# Insight Agent 升级规格书（UPGRADE SPEC）

> 版本：v1.0 · 状态：待评审
> 原则：每个模块做全做透，不提供简化版糊弄；每个模块必须可验收、可测试、可讲透（面试）。
> 所有模块独立成篇：目标 → 数据结构 → 接口 → 流程 → 错误处理 → 配置 → 测试 → 验收标准 → 工作量。

---

## 0. 总览

### 0.1 背景诊断（为什么改）

当前系统存在三对结构性缺失：

| 现状 | 缺失 | 后果 |
|---|---|---|
| 只有生成，没有验证 | 报告无任何环节核对"该句有证据吗" | 幻觉无人把关 |
| 只有存储，没有检索 | 笔记按主题 slug 精确匹配，换措辞即新档案 | 记忆是仓库不是大脑 |
| 只有片段，没有原文 | researcher 只看 Tavily 摘要，从未打开网页 | 引用是装饰不是证据 |

### 0.2 模块清单与 JD 映射

| 编号 | 模块 | 命中的 JD 原文 | 波次 |
|---|---|---|---|
| A | 证据深读层（完整 RAG） | "熟悉 RAG，知识库"（JD5）；"上下文管理"（JD2） | 1 |
| B | 验证层（Claim Verifier） | "分析 Agent 失败案例，深耕……可靠性"（JD2） | 1 |
| F | 评测 2.0（幻觉率/多票/成本曲线） | "搭建评测、基准与回归测试体系，量化成功率、Token、时延、稳定性"（JD2 逐字） | 1 |
| G | 多源搜索动态路由 | "动态工具路由"（JD2 逐字） | 2 |
| M | MCP Server 导出 | "MCP"（JD1/JD2） | 2 |
| L | CLI 入口 | "工具调用、cli、skills 的 harness 工程"（JD1 逐字） | 2 |
| O | 本地模型档位（Ollama/vLLM） | "本地安装大模型推理平台 Ollama, vLLM"（JD5 逐字） | 2 |
| D | 可观测性（Langfuse trace） | "异常场景定位与修复"（JD4） | 2 |
| C | 语义记忆（事实卡片 + 向量检索） | "长短期记忆"（JD1/4）、"知识库、智能体记忆系统"（JD5） | 3 |
| E | 迭代深研循环 | "长周期 Agent"（JD2） | 3 |

### 0.3 全局设计原则（贯穿所有模块）

1. **参数化知识负责"怎么想"，外部证据负责"是什么"** —— 事实性陈述必须可溯源
2. **降级而非崩溃** —— 每个外部依赖（网页/搜索/向量库/LLM）都有降级链，降级事件显式记录
3. **代码拥有不变量，LLM 拥有判断** —— 轮数上限、并发上限、预算上限全部代码兜底
4. **可观测优先** —— 每个新模块的输入输出必须可被 trace 和评测捕获
5. **成本分档** —— 全局三档 `fast / standard / deep`，各模块按档位缩放行为

---

## 1. 模块 A：证据深读层（Grounded Evidence Pipeline）

### 1.1 目标

把证据从"搜索摘要片段"升级为"网页全文中的相关段落"，引用落到段落级。
这是模块 B（验证）的前提：没有真证据池，claim 核对无从谈起。

### 1.2 数据结构

```python
# src/insight_agent/tools/fetcher.py
@dataclass
class SourceDoc:
    url: str
    title: str
    status: Literal["fetched", "failed_403", "failed_timeout",
                    "failed_paywall", "failed_decode", "degraded_snippet"]
    text: str | None        # trafilatura 清洗后正文，失败为 None
    fetched_at: str

@dataclass
class Chunk:
    chunk_id: str           # f"{url_hash}:{seq}"
    url: str
    text: str               # 500-800 字，相邻块重叠 ~50 字
    embedding: list[float] | None   # 懒计算
```

```python
# src/insight_agent/memory/evidence_pool.py（单次运行内有效）
class EvidencePool:
    def add_chunks(self, chunks: list[Chunk]) -> None: ...
    def search(self, query: str, top_k: int = 4) -> list[Chunk]:
        """语义检索：query embedding ↔ chunk embedding 余弦相似度。"""
    def stats(self) -> dict:
        """{docs, chunks, degraded_docs} —— 供 trace 与评测采集。"""
```

### 1.3 接口签名

```python
# tools/fetcher.py
def fetch_readable(url: str, *, timeout: float = 15.0) -> SourceDoc:
    """httpx 抓取 + trafilatura 抽正文。永不抛异常，失败进 status。"""

# tools/chunker.py
def chunk_text(url: str, text: str, *, size: int = 700, overlap: int = 50) -> list[Chunk]:
    """优先按 markdown 标题/空行分段，超长段再滑窗切。确定性纯函数。"""

# tools/selector.py（LLM 选择器）
def select_urls(snippets: list[SearchResult], question: str, *,
                max_docs: int, llm) -> list[str]:
    """LLM 根据 snippet 相关性挑出值得深读的 URL。结构化输出，失败降级为取前 max_docs 条。"""
```

### 1.4 research_one 节点改造（流程）

```
1. TavilySearch(query) → top-8 结果（title + url + snippet）
2. select_urls() → 按 depth 档位选 N 篇（fast=0 / standard=2 / deep=5）
3. 并行 fetch_readable（asyncio.gather，单篇 timeout 15s）
4. 成功篇 chunk_text → EvidencePool.add_chunks
5. EvidencePool.search(question, top_k=4) → 相关段落
6. 生成证据笔记：LLM 基于【相关段落原文】作答，
   引用格式升级为段落级：[来源N](url "quote=前80字")
7. 降级规则：某 URL 深读失败 → 该 URL 退回用 snippet，证据条目标注 ⚠degraded
```

### 1.5 Embedding 策略（含降级）

| 优先级 | 方案 | 说明 |
|---|---|---|
| 1 | OpenAI 兼容 embeddings 端点 | 与 LLM 同一 provider，`/v1/embeddings` |
| 2 | 本地 fastembed（ONNX，免网络） | 端点不支持或超时自动切换，首次下载模型 ~90MB |
| 3 | 关键词重叠打分（BM25 简化版） | 最终兜底，纯 Python 零依赖 |

三档实现封装为 `tools/embedder.py: Embedder` 协议 + `get_embedder(settings)` 工厂，
启动时探活一次并固定，运行中不再切换（保证同轮内向量空间一致）。

### 1.6 错误处理与配置

| 场景 | 处理 |
|---|---|
| 403/付费墙 | status=failed_*，退回 snippet 模式，degraded 标记 |
| 抓取超时 | 重试 1 次（换 UA），仍败则降级 |
| 正文过短（<200 字） | 视为抓取失败（多为反爬空壳页） |
| robots.txt 拒绝 | 尊重，跳过该 URL |
| 域名黑名单 | settings 可配置 `FETCH_BLOCK_DOMAINS` |

配置（.env / Settings 扩展）：

```
RESEARCH_DEPTH=standard        # fast | standard | deep
FETCH_TIMEOUT=15
FETCH_MAX_CONCURRENCY=4        # 并行抓取上限
EMBEDDING_PROVIDER=auto        # api | local | keyword | auto
```

### 1.7 测试方案

- chunker：确定性单测（标题切分 / 滑窗 / 短文本边界），零网络
- fetcher：对 3 个真实 URL 冒烟 + mock 403/超时单测
- EvidencePool：假 embedding（确定性向量）测检索排序
- research_one：mock fetcher 全失败 → 验证 degraded 路径产出仍完整

### 1.8 验收标准

- [ ] standard 档下，一条子问题的证据笔记中 ≥60% 的证据来自网页正文段落（非搜索摘要）
- [ ] 每条证据可回溯：URL + 段落原文可人工打开核对
- [ ] 全部网页抓取失败时，流水线不崩，产出 degraded 标记的 snippet 版证据
- [ ] 评测报告新增字段：deep_read_coverage（深读覆盖率）

### 1.9 工作量

fetcher+chunker 0.5d · embedder 三档 0.5d · EvidencePool 0.5d · selector+节点改造 0.5d · 测试 0.5d ≈ **2.5d**

---

## 2. 模块 B：验证层（Claim Verifier）

### 2.1 目标

报告生成后不再"直接交付"，而是逐句核对：每条可核对的陈述是否有证据支撑。
产出三件套：**幻觉率数字、逐句标注版报告、可信度评分**。

### 2.2 数据结构

```python
@dataclass
class Claim:
    text: str                 # 报告中的一句原子陈述
    verdict: Literal["supported", "partial", "unsupported", "not_checkable"]
    evidence: list[dict]      # [{url, quote}] 支撑它的证据
    reason: str               # LLM 判定理由（一句话）

@dataclass
class VerificationReport:
    claims: list[Claim]
    hallucination_rate: float # unsupported / (supported + partial + unsupported)
    coverage_rate: float      # not_checkable 占比（越低越好）
    score: float              # 0-100 综合可信度 = 100 - 100*hallucination_rate - 50*coverage_rate*0.2
```

### 2.3 流程（新增 verify 节点，插在 writer 与 archive 之间）

```
writer 输出 report
  ↓
① claim 提取：LLM 结构化输出，把报告拆成原子陈述列表
   （规则：一个数字/一个因果断言/一个排名 = 一条 claim；格式性语句跳过）
  ↓
② 逐条核对（并行，每条 claim）：
   EvidencePool.search(claim, top_k=3) → 候选段落
   → LLM 判定：段落原文 vs claim 是否相符（supported/partial/unsupported）
   → 找不到任何相关段落 → not_checkable
  ↓
③ 汇总 VerificationReport
  ↓
④ 生成标注版报告：unsupported 句尾追加 ⚠️[未找到证据支撑]，partial 追加 ⚠️[部分支撑]
  ↓
⑤ 双版本输出：clean 版（用户看）+ annotated 版（存档/评测用）
```

### 2.4 配置与降级

```
VERIFY_ENABLED=standard/deep 档默认 true，fast 档默认 false
VERIFY_MAX_CLAIMS=30         # 防超长报告核对失控（不变量）
VERIFY_CONCURRENCY=4         # claim 核对并行上限
```

降级链：LLM 判定连续失败 → 该 claim 记 not_checkable；verify 节点整体异常 →
报告照常交付， VerificationReport.error 记录，评测采集 error 而非崩图。

### 2.5 测试方案

- claim 提取：固定报告文本 → 断言拆出的 claim 数与内容（mock LLM）
- 判定：构造"有支撑/无支撑/矛盾"三类样本对，mock LLM 返回，验证四态归类
- 幻觉率计算：纯函数单测（手工构造 VerificationReport）
- 并行核对：4 条 claim mock，验证并发与异常隔离

### 2.6 验收标准

- [ ] 端到端跑 standard 档：报告自动产出 VerificationReport，幻觉率有数字
- [ ] 人工抽查 10 条 supported claim：≥8 条能在证据里找到对应原文
- [ ] 人工抽查 unsupported claim：确属报告无证据支撑（验证判定器不是摆设）
- [ ] annotated 版报告中 ⚠️ 标记位置准确
- [ ] fast 档 verify 关闭时，流水线耗时回落到现有水平

### 2.7 工作量

提取+判定 1d · 汇总与标注版 0.5d · 节点集成与并行 0.5d · 测试 0.5d ≈ **2.5d**

---

## 3. 模块 F：评测 2.0

### 3.1 目标

评测从"能跑"升级为"能量化可信度、能对比成本-质量、能多票防 judge 抽风"。

### 3.2 新增指标

| 指标 | 来源 | 采集方式 |
|---|---|---|
| claim_hallucination_rate | 模块 B | e2e 用例自动读取 VerificationReport |
| claim_coverage_rate | 模块 B | 同上 |
| deep_read_coverage | 模块 A | EvidencePool.stats() |
| judge_consensus | 多票制 | 同 rubric × 3 次（temperature 0/0.3/0.7），2/3 多数决；一致率入报告 |
| cost_quality_curve | 档位对比 | 同一 e2e 用例 × fast/standard/deep 三档，tokens vs 幻觉率散点 |

### 3.3 用例集扩展

- 现有 30 条保持；e2e 8 条全部追加 `verify: true`（自动采集幻觉率）
- 新增 **golden set 5 条**：人工写好"标准答案要点"（3-5 个 key_points），
  判定 = 报告覆盖了几个要点（结构化抽取 + 关键词/语义双匹配），产出 coverage 分

### 3.4 报告升级

Markdown 报告新增：
- 幻觉率列（e2e 明细表）
- 三档对比表（tokens / 时延 / 幻觉率 / 覆盖率，一张表看清成本-质量权衡）
- judge 一致率（<2/3 的 judge 结果标"低置信"）

### 3.5 验收标准

- [ ] e2e 报告每条含幻觉率数字
- [ ] 三档对比表可复现（同一用例三档各跑一次的产物）
- [ ] golden set 5 条入集并有 coverage 分
- [ ] 全部指标可被回归模块 diff（新增字段纳入对比）

### 3.6 工作量

指标采集 0.5d · 多票 0.5d · golden set 编写 0.5d · 三档对比与报告 0.5d ≈ **2d**

---

## 4. 模块 G：多源搜索动态路由

### 4.1 设计

```python
class SearchProvider(Protocol):
    name: str
    def search(self, query: str, *, max_results: int) -> list[SearchResult]: ...

# 实现
TavilyProvider      # 现有
DuckDuckGoProvider  # ddgs 包（国内需代理，作为海外 query 备选）
BochaProvider       # api.bochaai.com，国内直连（预留，注册 key 即启用）

class SearchRouter:
    """链式降级 + 熔断。"""
    def __init__(self, providers: list[SearchProvider], *,
                 cooldown_s: int = 60, breaker_threshold: int = 3): ...
    def search(self, query: str, *, max_results: int) -> SearchResultBatch:
        """按序尝试：失败（异常/空结果）→ 熔断计数+1 → 下一家。
        熔断：连续 3 次失败则该 provider 冷却 60s 内跳过。
        返回 batch 携带 metadata：{provider_used, fallback_chain}。"""
```

语言路由：query 含 CJK 且 Bocha 可用 → 置顶 Bocha；纯英文 → Tavily 置顶。
降级事件写入 findings 尾注：`⚠ 搜索源降级：tavily → duckduckgo`。

### 4.2 验收标准

- [ ] mock 三家 provider，验证熔断、冷却、顺序降级
- [ ] 真实环境：拔掉 Tavily key → 流水线自动走 DuckDuckGo 完成
- [ ] SearchResultBatch.metadata 进 trace

### 4.3 工作量

**0.5d**

---

## 5. 模块 M：MCP Server 导出

### 5.1 目标

把 Insight Agent 的研究能力封装为 MCP Server，供 Claude Desktop / Claude Code / 任何 MCP 客户端调用 —— 从"会用 MCP"升级为"提供 MCP"。

### 5.2 工具清单

| 工具 | 入参 | 出参 |
|---|---|---|
| `research` | topic: str, depth: fast/standard/deep | 完整 Markdown 报告（含验证标注） |
| `get_notes` | topic: str | 该主题档案（notes 摘要 + last_report） |
| `list_archives` | — | 全部已研究主题列表 |

### 5.3 实现要点

- SDK：官方 `mcp` 包 server 侧（已装 2.2.0，实现前照例查证安装版 API）
- 传输：stdio（本地 Claude Code 接入）+ `--http` 开关（streamable-http，远程接入）
- 阻塞问题：research 是长任务（分钟级）—— MCP 工具内 async 执行，
  提供进度通知（notifications/progress）；客户端超时配置写入 README
- 入口：`python -m insight_agent.mcp_server`（stdio）/ `--http --port 8001`
- 并发：同一 NotesStore 的文件锁沿用；研究并发上限全局信号量 1（防额度打爆）

### 5.4 验收标准

- [ ] Claude Desktop 配置后能成功调用 `research` 拿到报告
- [ ] `list_archives` 返回与 data/notes 一致
- [ ] InMemoryTransport 集成测试覆盖三工具

### 5.5 工作量

**1d**（SDK 查证 0.25 + server 实现 0.5 + 测试 0.25）

---

## 6. 模块 L：CLI 入口

### 6.1 设计

`typer` 实现三条命令：

```bash
uv run python -m insight_agent.cli research "主题" \
    --depth standard --out report.md --no-verify   # 单次研究
uv run python -m insight_agent.cli serve --port 8000    # 起 Web 服务（等价 uvicorn 命令）
uv run python -m insight_agent.cli eval --tier unit     # 转发到评测 CLI
```

- `research` 支持 `--json`（输出 JSON：report + verification + metrics），
  供脚本/其他程序调用 —— 这是"Agent 作为可编程组件"的接口形态
- 退出码语义与评测 CLI 一致（FAIL→1）

### 6.2 验收标准

- [ ] 三条命令可用，`--help` 完整
- [ ] `research --json` 产出结构合法（jsonschema 校验）

### 6.3 工作量

**0.5d**

---

## 7. 模块 O：本地模型档位（Ollama / vLLM）

### 7.1 设计

Settings 引入 profile 概念，不同角色可指向不同端点：

```
# .env
LLM_PROFILE=cloud            # cloud | local | hybrid
CLOUD_BASE_URL=...           # agnes
LOCAL_BASE_URL=http://localhost:11434/v1   # Ollama 的 OpenAI 兼容端点
LOCAL_MODEL=qwen3:8b
```

| profile | planner/researcher | writer/judge | 用途 |
|---|---|---|---|
| cloud | 云端 | 云端 | 现状 |
| local | 本地 | 本地 | 全离线演示（JD5 场景） |
| hybrid | 云端 | 本地 | 主力生成用强模型，judge 用本地省额度 |

工厂：`get_llm(role: str, settings) -> ChatOpenAI`，全部节点改走工厂（消除散落的 ChatOpenAI 构造）。
结构化输出兼容性：Ollama 对 function-calling 支持参差 → planner 在 local 档
自动切 JSON-mode + schema 校验重试（复用 planner 现有重试骨架）。

### 7.2 验收标准

- [ ] Ollama 拉起 qwen3:8b 后，local 档全流水线跑通
- [ ] hybrid 档 judge 使用本地模型（trace 可见）
- [ ] 所有节点不再直接构造 ChatOpenAI

### 7.3 工作量

**0.5d**（+ 用户侧安装 Ollama/模型的时间）

---

## 8. 模块 D：可观测性（Langfuse）

### 8.1 设计

- 自托管：docker-compose 追加 langfuse + postgres 服务（官方 compose 改端口）
- 接入：`langfuse.langchain.CallbackHandler` 挂进 LLM 构造（与 Usage 回调并列），
  全链路 trace 自动上云（本机）
- 采集内容：每节点输入输出、每次 LLM 调用的 prompt/completion/tokens/时延、
  工具调用参数、验证层逐 claim 判定
- 隐私开关：`LANGFUSE_MASK_CONTENT=false` 时只记元数据不记正文
- 评测 CLI 联动：`--trace` 时每次 run 生成独立 session，报告里附 trace 链接

### 8.2 验收标准

- [ ] 一次研究产生完整 trace：6 节点 + N 子 agent + 逐 claim 核对全部可见
- [ ] trace 中可直接跳看某次 writer 调用的完整 prompt
- [ ] 遮罩开关生效

### 8.3 工作量

**0.5d**（+ compose 编排 0.25d）

---

## 9. 模块 C：语义记忆（事实卡片 + 向量检索）

### 9.1 目标

把"整坨 markdown 档案"进化为"结构化事实卡片 + 向量检索"，
recall 从"精确匹配主题"升级为"语义相关的记忆都能被找回"。

### 9.2 数据结构

```python
@dataclass
class FactCard:
    id: str                  # uuid
    claim: str               # 一条原子事实（来自验证层 supported claim）
    source_url: str
    source_quote: str        # 证据原文片段
    topics: list[str]        # 主题标签（研究主题 + planner 提纲关键词）
    confidence: float        # 验证层 supported=1.0 / partial=0.6
    embedding: list[float]
    created_at: str
```

### 9.3 存储与检索

- 向量库：SQLite + sqlite-vec（零服务、单文件、已依赖 sqlite-vec 包），
  表 `fact_cards(id, payload_json, embedding vec)`；KNN 检索 top-k
- 写入闭环：**verify 节点产出的 supported/partial claim → 自动转 FactCard 入库**
  （验证层是记忆层的质检关：只有核对过的事实才配进长期记忆 —— 这是 B→C 的架构级联动）
- 读取：recall 节点双通道 = ①主题 slug 精确档案（现有，保留）②向量检索全局 FactCard top-10
  （跨主题记忆）。双通道结果都进 planner 提示（目录 + 相关事实卡）
- 冲突处理：新卡片与旧卡片 claim 语义相似 >0.92 且来源更新 → 旧卡标记 superseded（不删除，留痕）

### 9.4 迁移与兼容

- NotesStore 保留（原始证据全文仍是 writer 的原料），FactCardStore 是其上的索引层
- 提供迁移脚本：从现有 data/notes/*.json 的 reports 里抽取 claim 入卡（LLM 批量提取，一次性）

### 9.5 验收标准

- [ ] 跨主题记忆验证：先研究"A 框架格局"，再问"某厂商动态"，recall 能召回 A 研究中该厂商的事实卡
- [ ] 相似冲突事实触发 superseded 标记
- [ ] 迁移脚本对现有 4 份档案跑通
- [ ] 检索 P95 < 50ms（本地向量）

### 9.6 工作量

FactCardStore 1d · 写入闭环 0.5d · recall 双通道 0.5d · 迁移脚本 0.5d ≈ **2.5d**

---

## 10. 模块 E：迭代深研循环

### 10.1 设计

- 新节点 `gap_analyzer`（compress 之后、writer 之前）：
  输入 = 提纲 + 全部证据 + digest；输出 = 结构化判定
  `{sufficient: bool, missing_aspects: list[str]}`
- `sufficient=false` 且 `rounds < MAX_RESEARCH_ROUNDS` →
  把 missing_aspects 作为新子问题再次 Send fan-out（复用 research_one），
  rounds+1 → 回到 compress
- `MAX_RESEARCH_ROUNDS = 2`（deep 档 3）：不变量，代码拥有，防止长周期失控
- 仅 `depth=deep` 启用（standard/fast 直通 sufficient=true）

### 10.2 验收标准

- [ ] deep 档下构造证据薄弱的主题：观察到第二轮补充研究发生且 rounds 正确计数
- [ ] rounds 达上限后强制进入 writer（死循环不可能发生）
- [ ] trace 中两轮研究清晰可辨

### 10.3 工作量

**1d**

---

## 11. 最终拓扑（全部模块就位后）

```
                         ┌──────────── Langfuse trace（全链路）────────────┐
topic ──► recall ──► planner ──► Send×N ──► research_one ──► compress ──► gap_analyzer
          ▲│                  （SearchRouter 多源路由，             │  ▲                │
          ││                   Fetcher 深读 + EvidencePool 检索）   │  └── 不足 & rounds<上限 ─┘
          ││                                                      │
          ││                                            verify（claim 逐条核对）
          ││                                                      │
          │└──── digest + FactCard 语义召回 ◄── FactCardStore ◄────┘（supported claim 入卡）
          ▼
        writer ──► archive（双版本报告 + data/reports/*.md + NotesStore）
```

入口矩阵：Web(SSE) · CLI(research/serve/eval) · MCP Server(research/get_notes)，
三者共用同一 graph 与记忆层。

---

## 12. 实施顺序与验收门禁

| 波次 | 模块 | 前置依赖 | 验收门禁（未过不开下一波） |
|---|---|---|---|
| 1 | A → B → F | 无 | 幻觉率报告产出且人工抽查合格 |
| 2 | G → M → L → O → D | 波次 1 | 拔 Tavily 仍可完成研究；Claude 调通 MCP；local 档跑通 |
| 3 | C → E | A/B（事实卡来源） | 跨主题记忆召回验证通过 |

- 每个模块完成即提交 git 并打 tag（`v0.x-module-a` …），commit message 关联本规格编号
- 全程 `uv run pytest` 保持全绿；评测 unit 层作为每次提交的回归门禁
- 额度策略：开发期用 fast 档验证逻辑，每模块验收时跑一次 standard 档

---

## 13. 风险登记簿

| 风险 | 概率 | 缓解 |
|---|---|---|
| 反爬导致深读覆盖率低 | 高 | 降级链 + degraded 显式标注（1.6） |
| 免费额度不足以支撑验收 | 高 | fast 档开发 + 档位验收分批 + local 档兜底 |
| Ollama 结构化输出兼容差 | 中 | JSON-mode 回退 + 重试骨架复用（7.1） |
| 并行 fetch 触发本机限流 | 中 | FETCH_MAX_CONCURRENCY 信号量 |
| 验证层判定本身不稳定 | 中 | 判定 prompt 附原文对照 + not_checkable 兜底 + 评测持续观测 |
| MCP 长任务客户端超时 | 中 | 进度通知 + README 超时配置说明（5.3） |
