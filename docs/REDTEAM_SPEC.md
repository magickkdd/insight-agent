# REDTEAM_SPEC · Insight Agent 对抗审查节点（Red-Team）

版本：v1.0-draft1 · 日期：2026-09-29 · 状态：**待执行（本文是计划契约，所有 as-built 数字留空，禁止预填）**
预算：约 14 净工时（2 天）· 对齐 JD：晶远芯「多智能体（Multi-Agent）系统」、正和御风「Agent 能力测评 / Bad case 分析」、九方智投「金融超级智能体多轮质检」
关系：本文只写增量。UPGRADE_SPEC.md 未被本文推翻的条款全部继续有效；graph 拓扑、State 字段、记忆联动规则以当前 `src/insight_agent/graph/` 代码为准。

---

## 0. 一句话定位

**verify 回答"每句话有没有证据"；redteam 回答"这份报告敢不敢直接交给客户"。**

verify 是句子级、机械的（claim ↔ 证据逐句比对）；redteam 是报告级、对抗的：一个独立审查 Agent 站在客户立场，质疑证据真实性、逻辑一致性与覆盖面，不合格就打回去。verify 不可能发现"两条 claim 各自有证据但互相矛盾"——这是 redteam 存在的理由。

## 1. 拓扑变更

```
现状：  writer → verify → archive → END
目标：  writer → verify → redteam ─┬─ pass ────────────────────→ archive → END
                                   ├─ fail 且轮数未满 → writer(带 issues 修订)
                                   └─ fail 且轮数耗尽 → archive(降级标注) → END
```

- 新节点 `redteam` 插在 verify 与 archive 之间；archive 不动。
- fail 修订走 **writer 复用**（issues 注入提示词），不走 re-research——v1 控制成本；`redteam_mode=re-research`（复用 `route_after_planner` 的 Send 机制把 issues 转成新子问题）留作 v2 开关，本文不实现。
- 与 gap_analyzer 的迭代深研循环**相互独立**：`rounds`（research 轮数）与新增 `redteam_rounds` 分开计数，各自有上限，避免两个循环互相放大成本。

## 2. State 与数据结构

`ResearchState` 新增两个字段：

```python
redteam: dict          # RedTeamReport.to_dict()（pass 后也落，供 trace/评测消费）
redteam_rounds: int    # redteam 已触发的修订轮数（默认 0）
```

新数据结构（放 `graph/redteam.py`，风格对齐 `verify.py` 的 Claim/VerificationReport）：

```python
class Issue(BaseModel):
    dimension: Literal["evidence", "logic", "coverage"]   # 证据真实性 / 逻辑一致性 / 覆盖面
    severity: Literal["blocker", "major", "minor"]
    quote: str          # 报告中被质疑的原文片段（必须能在 report 里找到，供定位）
    reason: str         # 一句话质疑理由

class RedTeamReport:
    verdict: Literal["pass", "revise"]
    issues: list[Issue]
    skipped: bool = False
    error: str | None = None

    @property
    def blocker_count(self) -> int: ...
    def to_dict(self) -> dict: ...   # 含 issues 全量，供 eval/Langfuse 消费
```

## 3. 节点接口与配置

```python
def make_redteam_node(llm, cfg, pool) -> node
# 签名对齐 make_verify_node：pool 用于抽查"报告引用 vs 证据池实际内容"是否对得上
```

Settings / 环境变量（对齐 config.py 现有约定）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `REDTEAM_ENABLED` | `auto` | auto = 仅 deep 档启用（分级交付哲学：fast/standard 不加时延）；true/false 强制 |
| `REDTEAM_MAX_ROUNDS` | `1` | 修订轮数上限（含首轮审查；>1 时第 2 轮只复查上轮 issues 是否解决） |
| `REDTEAM_PROFILE` | `hybrid` | 复用 LLM 工厂：审查走强模型（对抗审查的质量下限比成本更敏感） |
| `REDTEAM_MAX_ISSUES` | `10` | 单轮 issues 截断上限（控成本 + 防 prompt 爆炸） |

审查提示词三个维度（输出 JSON，风格对齐 VERDICT_PROMPT）：

1. **evidence 证据真实性**：抽查 ≤5 条 claim 的引用——URL 是否真实出现在证据里、数字是否与证据段落一致（pool 随机采样注入提示词）；
2. **logic 逻辑一致性**：结论之间是否矛盾、因果断言是否有证据跳跃、报告与 verification.unsupported 集合是否自洽；
3. **coverage 覆盖面**：报告章节 vs planner 提纲（existing_digest + brief）是否漏答子问题。

判定规则（写死在节点里，不给模型自由裁量）：存在任一 `blocker` → `revise`；仅 minor → `pass`（issues 照写，供 archive 留档）。

## 4. writer 修订链路

`make_writer(llm)` → `make_writer(llm, revision_notes: str | None = None)`：
- issues 非空时，把逐条 issue（dimension/quote/reason）作为"上一轮审查意见"注入 writer 提示词，要求**只修订被质疑的段落、保留引用格式**；
- 修订后的报告重新走 verify → redteam（第 2 轮）；
- `REDTEAM_MAX_ROUNDS=1` 时 fail 直接降级归档：报告保留 ⚠️ 标注 + `redteam.verdict=revise` 落 state——**不粉饰成 pass**（对齐全项目"失败不粉饰"原则）。

## 5. 判据（四层，全部不信任模型自述）

| # | 层 | 判据 | 实现 |
|---|---|---|---|
| T1 | 单元 | fake LLM 脚本驱动 ≥6 例：全绿 pass / 假引用被 flag / 相互矛盾结论被 flag / 覆盖缺失被 flag / 轮数上限终止不死循环 / JSON 畸形走容错不崩 | `tests/test_redteam.py`（对齐 test_verify.py 风格） |
| T2 | e2e | 构造 golden fixture：证据池预置一条**无出处的编造数字** + 一对**相互矛盾的结论**，redteam 必须 `verdict=revise` 且 issues 命中这两类（dimension + quote 命中注入内容，不看模型说了什么） | `eval/cases.yaml` 新增 e2e 用例 |
| T3 | 回归 | 现有 35 用例全绿不回退；幻觉率不升高；deep 档端到端时延增量记录实测值并设上限（目标 ≤25%，超了要么优化要么如实记录并讨论） | `eval/runner.py` 现有回归 diff |
| T4 | 迭代有效性 | 修订轮后二轮审查：同 issue 不再出现，或 verdict=pass；测不到（样本不够）就如实记「未量」，不许编 | e2e 跑 3 次取一致 |

## 6. 非目标（v1 明确不做）

- 不做 re-research 模式（`redteam_mode` 留枚举位）；
- 不改 verify 的 FactCard 入卡规则（redteam 结果 v1 不参与记忆准入，避免耦合；v2 再议）；
- 不做 redteam 自身的对抗训练/微调；
- fast/standard 档不加 redteam（时延敏感，分级交付原则不动摇）。

## 7. 风险清单

| 风险 | 缓解 |
|---|---|
| redteam ↔ writer 无限循环 | `redteam_rounds` 硬上限；耗尽即降级归档 |
| 本地小模型当审查者判据质量不足 | `REDTEAM_PROFILE=cloud/hybrid`（对齐"审查走强模型"的 hybrid 语义） |
| deep 档成本上升 | 单轮 = 1 次全文调用 + pool 采样 ≤5 条，成本有界；issues 截断 |
| LangGraph 条件边循环 | 复用 gap_analyzer 已验证的 route 模式，不发明新写法 |
| 审查提示词被报告内容注入 | quote 字段必须能在 report 中定位，节点侧校验，定位失败按畸形 JSON 容错 |

## 8. 交付物清单

- [x] `src/insight_agent/graph/redteam.py`（Issue/RedTeamReport/make_redteam_node）
- [x] `graph/state.py` +2 字段；`graph/build.py` 插节点 + 条件边；`graph/nodes.py` writer 支持 revision_notes
- [x] `config.py` +4 环境变量；`.env.example` 同步
- [x] `tests/test_redteam.py`（T1）
- [x] `eval/cases.yaml` +T2 golden 用例；T3 回归通过；T4 记录
- [x] `docs/REDTEAM_SPEC.md` 本文补 as-built 段（实测时延增量、T4 结果、与预算的偏差）
- [x] README 亮点区 +1 条；简历项目二第 1 条升级（Multi-Agent 编排 → 增"对抗审查与修订闭环"）

---

## 9. as-built（2026-09-30 实测）

> 本节只写实测值与偏差；测不到的维度显式标「未量」，不做估算式回填。
> 取证件：`eval/runs/report_20260930_*.md/.json` 共 21 份（`003320` ~ `013503`：unit 四批 + e2e 三次 + T2 稳定性复测）。

### 9.1 四层判据结果

| 层 | 结论 | 实测 |
|---|---|---|
| T1 单元 | 通过 | `tests/test_redteam.py` **18 例全绿**（接续前 17 例 + 1 例 max_rounds 语义）；全量 `uv run pytest -q` **58 passed**（再 +1 例 verify 重复入卡） |
| T2 golden | 通过（多票口径） | `redteam-golden-fabrication-001` 真实 LLM，case 级 3 票取 issues 并集：`verdict=revise`，evidence+logic 双维、`4.8` 逐字 quote 与矛盾对任一侧 quote 全命中；提示词收紧后连跑 4 次 **4/4 PASS**，单条 13.0—47.5s / 5,424—8,860 tokens（=3 次审查调用）。收紧前：单票 1/3、三票并集仍 1/3，原因与修法见 §9.2 |
| T3 回归 | 不回退 | unit 层 23 条全跑完：21 条首跑即绿；`writer-coffee-report-003` judge 单票 FAIL → 复跑转绿（判分抖动，非结构断言）；`writer-no-fabrication-006` 连 2 次 judge FAIL（报告写出证据里没有的数字，历史无该用例基线，writer unit 提示词逐字未变 → 与本文无关，见 §9.6）；回归 diff **0 项退化 / 0 项成本警告**。受免费额度 429 限制，unit 层分 4 批跑完（评测 CLI 遇限流即中止剩余，属既有行为） |
| T3 时延 | 达标（目标 ≤25%） | 端到端 A/B **不可归因**（见下），改按节点墙钟实测：真实 deep 报告 4,020 字、同输入连跑 3 次 = **3.9 / 4.4 / 5.3s，均值 4.5s**，约 4.3k tokens/次；折算到两次 e2e 总时延 A=704.2s → **+0.6%**，B=118.7s → **+3.8%** |
| T4 迭代有效性 | e2e 未量 / fixture 实测 4/4 成立 | 见 §9.3 |

### 9.3 T4 迭代有效性

e2e 三次真实样本（同题 `e2e-llm-memory-004`，`RESEARCH_DEPTH=deep`）：

| 次 | 配置 | 总时延 | 总 tokens | redteam |
|---|---|---|---|---|
| A | `REDTEAM_ENABLED=false` | 704.2s | 94,451 | skipped |
| B | `true` + `MAX_ROUNDS=1` | 118.7s | 28,942 | pass · 0 blocker |
| C | `true` + `MAX_ROUNDS=2` | 566.2s | 83,264 | pass · 0 blocker |

**e2e 层记「未量」**：3 次都没出 blocker，修订链路一次也没被触发，按 §5 的约定不编结论。

补测（与 T2 同一 golden 用例，受控 fixture + 真实 LLM 走完整闭环，提示词收紧后的 2 个独立样本）：

| 环节 | 样本 1 | 样本 2 |
|---|---|---|
| 首轮审查 | 5.4s · revise · 4 blocker（6 条意见） | 3.0s · revise · 2 blocker（2 条意见） |
| writer 修订 | 6.9s · 1,783 字 | 8.9s · 2,324 字 |
| 二轮复查（RECHECK 模式） | 0.4s · pass · 0 blocker | 1.5s · pass · 0 blocker |
| 同 issue 复现 | 0 / 6 | 0 / 2 |
| 首轮被质疑段落在修订稿中定位不到 | 6 / 6 | 2 / 2 |
| 单链路 tokens（3 次调用合计） | 4,992 | 4,780 |

判据全读结构化字段（`verdict` / `blocker_count` / quote 能否在新报告重新定位），不看模型自述：
首轮打回的每条 quote 在修订稿里都不再存在，二轮复查 `action=review` 放行。
提示词收紧前另有 2 个样本同样是 `revise → 修订 → 二轮 pass`（6.2/4.3/0.4s 与 3.4/3.9/2.9s），
链路结论未变。→ **迭代有效性在 fixture 级实测成立（4/4）**，端到端层面仍未量。

A/B 对照失真的原因写死在这里以免后人重复踩：B 那次 4 个子问题有 3 个 `⚠degraded：搜索源全部失败`，
研究阶段成本从 704s 掉到 119s，差值比 redteam 本身大两个数量级。deep 档端到端时延由联网取证阶段主导，
单跑两次做 A/B 归因不成立，故用节点级实测 + 两次总时延给上界。
最坏路径（`MAX_ROUNDS=2` 且首轮判 revise）多 2 次审查 + 1 次 writer，按 §9.3 实测单链路 8—14s 计，
折算到 A=704.2s 为 +1.1%—2.0%，到 B=118.7s 为 +6.7%—11.8%，仍在 ≤25% 目标内。

### 9.2 审查判定存在跨轮抖动（实测，直接推翻 T4 的取样口径）

temperature=0、同一报告、同一提示词、同一证据池，连跑 3 次：

```
第1次：5.3s  verdict=revise  blockers=2
第2次：3.9s  verdict=pass    blockers=0
第3次：4.4s  verdict=revise  blockers=1
```

SPEC §5 给 T4 的口径是「e2e 跑 3 次取一致」，但审查判定本身就不是确定量，一致性样本无法构造。
T4 因此改按**触发链路是否按判据走通**取证（revise → writer 带 issues 修订 → 二次 verify → 二次审查
→ 放行或降级），单次链路即可判定"修订闭环是否工作"，跨轮一致性记「未量」。
压一致率的 v2 候选：审查多票投票制、或要求 blocker 必须附证据池反例段落。

抖动同样打在 T2 上，实测数据（同一 golden 用例，真模型）：

| 配置 | 结果 |
|---|---|
| 单票 | 3 跑 1 绿（2 次缺 logic 维度） |
| 3 票取 issues 并集 | 3 跑 1 绿（同上） |
| 3 票 + logic 提示词收紧 + 矛盾对改用 quote_any | **4 跑 4 绿** |

漏检的机制是审查者把"同一主体两个互斥份额"统一归成了 evidence（引用不实）而不是 logic，
于是两处改动后才立住：
1. **提示词**：logic 维度从"结论是否互相矛盾"改成可执行步骤——先按同一主体把数值/排名断言两两配对，
   互斥即出 logic issue，**即使两条各自都有出处**；并规定 quote 只能逐字复制互斥双方之一，另一处写进 reason。
2. **期望口径**：`redteam_quote_hits` 只锚死无出处的编造数字 `4.8`，矛盾对改判 `redteam_quote_any`
   （`45%`/`20%`/`第一`/`第四` 任一被逐字引用即算命中）。原口径把 `45%` 写成硬性 quote 命中，
   实际是在考"审查者抄哪一侧"，与它有没有发现矛盾无关。

### 9.4 与 SPEC 的偏差（as-built 增量）

1. **`RedTeamReport.action`（review | revise | degrade）**：SPEC 草图之外新增。
   轮数耗尽时 verdict 必须保持 `revise`（不粉饰），而 LangGraph 条件边只读 state——仅凭
   verdict+rounds 无法区分"刚打回"与"已耗尽"，会造成循环歧义。判定权收在持有 cfg 的节点，
   `route_after_redteam` 只读 action，终止性可证。
2. **`REDTEAM_MAX_ROUNDS` 语义修正**（本次接续修）：原实现 `rounds < max_rounds`，默认 1 会触发
   一次修订，与 §3 表格"含首轮审查"、§4"`REDTEAM_MAX_ROUNDS=1` 时 fail 直接降级归档"两处矛盾。
   改为 `rounds + 1 < max_rounds`，即允许修订次数 = max_rounds − 1；默认 1 = **只审不修**。
3. **T2 落地形态**：SPEC 写"cases.yaml 新增 e2e 用例"，但 e2e 是真实联网全流水线，无法确定性预置
   "编造数字 + 矛盾对"。故扩 schema 为 `tier: unit, node: redteam` 的受控 fixture：case 新增
   `report` / `pool_evidence`（`---` 分文档、首行 URL），expect 新增 `redteam_verdict` /
   `redteam_dimensions` / `redteam_quote_hits`，runner 端做结构化断言，不信任模型自述。
4. **auto 档判定读 `state.depth`，缺省按 standard**（与 verify 同款约定，非本文引入）：
   eval 的 e2e 不在初始 state 注入 depth，所以 `RESEARCH_DEPTH=deep` 不会自动打开 redteam，
   必须显式 `REDTEAM_ENABLED=true`——§9.1 的两次实测即此口径。
5. **修订回环不重复入卡**（本次接续修的缺陷）：writer 复用使 verify 二次进入，同一 claim 同一 URL
   会再生成一张 FactCard 污染召回（`FactCardStore.add` 的去重仅在 `source_url` 不同时触发）。
   verify 准入条件加 `not state.get("redteam_rounds")`，只在首轮入卡；§6"不改 verify 入卡规则"
   指的是不引入 redteam 结果准入，此处仅阻止重复写入。
6. **eval token 计量修复**：`_invoke_case` 原用 `llm.bind(callbacks=[handler])` 挂计量回调，
   langchain-core 1.6 下纯 invoke 路径报
   `generate_prompt() got multiple values for keyword argument 'callbacks'`，
   structured 路径则因 `RunnableBinding.__getattr__` 代理回原实例而静默丢回调 → 用例 tokens 全 0。
   新增 `llm_factory.with_llm_callbacks()`（实例级回调副本），两条路径均恢复取值；
   §9.1 的所有 token 数字均为修复后实测。README 记载的"Token 计量失效"属回归复发。
7. **评测端为对抗审查加了两个机制**（SPEC 未画，§9.2 逼出来的）：case 级 `redteam_votes`
   （同一输入跑 N 次，issues 取并集、任一轮 revise 即算 revise）与 expect 键 `redteam_quote_any`
   （矛盾对只锚"任一侧被逐字引用"）。单票断言是在测骰子，N 票并集才把"这一层能不能抓到"
   和"这一票掷得好不好"分开。

### 9.5 预算偏差

SPEC 预算约 14 净工时。交接文档只记"主体已完成、剩 T3/T4/文档"，前段实际工时未记录，**无从核对完成率**；
本次接续（语义修正 + API 接入 + token 计量修复 + T3/T4 实测 + T2 稳定性复测 + 文档）墙钟约 1 小时 15 分，
其中三次 e2e 与四批 unit 的等待占大半——真正的工程时间远小于评测等待时间，这是本类"判据不信任模型自述"
项目的固有成本。

### 9.6 遗留（不阻塞交付）

- e2e 层 verify 两次都无可判分 claim（报告头部「幻觉率：—」），而 `CaseResult` 不落
  `verification.error/skipped`，无法区分"抽 claim 为空"与"判定异常"→ 需先补取证字段再单独排查
  （redteam 未改 verify 的任何输入）。
- `writer-no-fabrication-006` judge 连 2 次 FAIL：撰写端仍会写出证据里没有的数字。
- 前端 revise 回环复用同一张 stage 卡片（writer 卡片被第二次结果覆盖），修订过程的时间线呈现待优化。
- 简历 PDF 为 09-29 旧版，docx 已更新，需重新导出。
- `redteam_mode=re-research` 仍只占枚举位，未实现（§6 非目标）。
