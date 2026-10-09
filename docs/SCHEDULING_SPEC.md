# SCHEDULING_SPEC · 定时调度、日志与运行台账

版本：v1.0-as-built · 日期：2026-10-09 · 状态：**已实现**（§11 as-built）
预算：3–4 净工时 · 对齐 JD：「测试与上线：定时调度、系统日志管理、报错拦截与 API 成本监控」

---

## 0. 一句话定位

给 `insight-agent` 补上**能被定时调起的运行形态**：一次运行有唯一 id、有落盘日志、有明确的退出码语义、有成本账，出问题能看出是"哪一次、哪个节点、烧了多少"。

**不是**做一个调度平台。cron / 计划任务 / CI 定时器是现成的，重复造轮子的收益是零。真正缺的是**被调起来之后那些运维接口**——现在没有。

---

## 1. 问题定位：现在为什么不能被放心地定时调起

盘上现状（逐条核实过）：

| 事实 | 出处 | 后果 |
|---|---|---|
| `cli research` 的退出码只有 0 / 1 / 2 | `cli.py:36`（`Exit(2)`）、`cli.py:69`（`Exit(1)`），全文件仅这两处 | 调用方分不出"成功""验证层异常""用法错"，更分不出"外部 API 限流"这种**该重试**的情况 |
| 退出码 1 覆盖了验证层异常 | `cli.py:68-69` `if verification.get("error")` | 一次限流和一次核验失败长得一样 |
| 运行过程不落盘任何日志 | `cli.py` 全文无 `logging` | 定时跑三次，失败了只能看终端回滚的输出，不知道哪一次 |
| Token 计量已经能拿到，但 `research` 命令不用它 | `eval/metrics.py::summarize_tokens` vs `cli.py:50-56`（`--json` 只放 `latency_s`） | 成本账只在评测里记，真实运行的成本看不到 |
| 报告写到哪儿由 `--out` 决定，不写就只打印 | `cli.py:64-67` | 定时跑不传 `--out` 的话，产物只在 stdout，事后无从查 |

评测层其实已经把这件事做对了一半：`eval/runner.py` 有 `error_kind` 字段区分 `rate_limit` / `exception`，有 `tokens` / `latency_s`，有 `CaseResult` 结构体。**要做的不是发明，是把评测层那套搬到真实运行路径上**——这跟 S9（评测层交付 B1）是同一个思路：先让指标有产地。

---

## 2. 范围与不做

### 2.1 做

1. **运行台账**：每次 `research` 生成 `run_id`，把 topic / depth / 起止时间 / 终止原因 / token / 落盘路径写进 `data/runs/<run_id>.json`
2. **结构化日志**：同一目录写 `run_id.log`，运行期关键事件按行落盘
3. **退出码语义细化**：把"用法错""配置缺""验证层异常""限流""内部异常"分开
4. **成本记录**：复用 `summarize_tokens`，把真实运行的 token 写进台账（能换算费用就给单价，换算不了就只记 token 并写明原因）
5. **`--json` 补字段**：`run_id`、`termination`、`tokens`、`elapsed_s`、`artifacts`

### 2.2 不做（且每条都写清为什么）

| 不做 | 为什么 |
|---|---|
| 内置 cron / 调度器循环 | cron、Windows 计划任务、GitHub Actions 都是现成的。自带一个循环等于让用户同时维护两套调度 |
| 飞书 / 邮件通知 | 需要 webhook 与凭证，是**部署配置**不是运行时能力。真要做应当是独立 Stage（M3），先留 `--notify-webhook` 的接口位置不实现 |
| 并发调度（同时跑多个主题） | `mcp_server.py` 已有 `_research_semaphore = 1` 的额度保护先例。批量并行要先解决额度与 SQLite 写竞争，本 Stage 不碰 |
| 分布式 / 多机 | 单机单人使用场景，加了就是装饰 |
| Web 端触发定时任务 | 那是产品功能，不是工程能力。JD 要的是"上线后负责定时调度"，指工程侧 |

---

## 3. 决策记录

### D1 · 退出码用扩展值还是只用 0/1/2？

**扩展值**，但**保持 0 = 成功**不变，且所有非 0 都能在台账里查到原因。

理由：调用方（cron 脚本、CI）最需要的是"该不该重试"。只用 0/1/2 时限流和 bug 同码，脚本只能全当失败。POSIX 惯例是 2 表示用法错、1 表示一般失败，把它们细分有明确收益。

| 退出码 | 含义 | 调用方该做什么 |
|---|---|---|
| 0 | 完成 | 无 |
| 1 | 验证层异常（幻觉率不可算等） | 看台账，不重试 |
| 2 | 用法 / 参数错 | 修命令，不重试 |
| 3 | 限流（429 / 上游拒绝） | **退避后重试** |
| 4 | 上游不可用（5xx / 连接失败） | **退避后重试** |
| 5 | 内部异常 | 上报，别重试 |

红线：`2` 保持"用法错"的传统含义不变，`0` 保持成功——脚本里已有的 `if [ $? -eq 0 ]` 不会破。

### D2 · 台账落在 `data/runs/`，不是日志文件里？

**两个都写，但职责分开**：`runs/<run_id>.json` 是**结构化台账**（给脚本读、要能被 `jq` 查），`runs/<run_id>.log` 是**人读的时间线**（给排查看）。

理由：把结构化数据塞进日志行，脚本要写正则解析；把日志塞进 JSON，人读要 jq。两者读者不同。`data/` 已在 `.gitignore` 里，不进版本库。

### D3 · run_id 怎么生成？

`<UTC 时间戳>-<主题 slug 8 位>-<6 位随机>`，例如 `20261009T081422-hxxquota-a3f91c`。

理由：纯时间戳在同秒并发下会撞；纯随机没法从文件名看出跑的是什么。两者拼一起在排查时最省事——`ls` 一眼看到哪天跑的哪的主题。**不用 UUID**：那玩意儿对人不可读，而这个 ID 是要被人看的。

### D4 · token 换算成钱吗？

**只记 token，不记钱**，除非 `Settings` 里配了 `price_per_mtok`。

理由：不同模型、不同厂商、不同时间的单价差异极大，代码里写死单价会产出一个"看起来精确、实际没人敢用"的成本数字。`mini-claude-code` 那边是 `price_per_mtokens: float = 0.0` 默认关闭——沿用同一个口径：没配就留 0，报告里显示"未配置单价"。

### D5 · 用 `logging` 还是自己拼？

**标准库 `logging`**，输出到 `runs/<run_id>.log`，格式 `HH:MM:SS.mmm LEVEL message`。

理由：标准库、零依赖、能被别的日志系统接。不自拼是因为自拼的最终形态一定是半个 logging。

### D6 · 拦截异常还是让它抛？

**在 `cli.research` 里 try/except 收口**，把异常映射成 D1 的退出码，台账里记 `error_kind` + 异常摘要，**不打印完整 traceback 到 stdout**（traceback 进 log 文件）。

理由：`eval/runner.py` 已经有 `error_kind` 这个概念（`rate_limit` / `exception`），沿用它而不是新造一套词。

---

## 4. 数据结构

### 4.1 台账 `data/runs/<run_id>.json`

```json
{
  "schema_version": "1.0",
  "run_id": "20261009T081422-hxxquota-a3f91c",
  "topic": "httpx 超时配置最佳实践",
  "depth": "standard",
  "started_at": "2026-10-09T08:14:22Z",
  "ended_at": "2026-10-09T08:16:31Z",
  "elapsed_s": 129.4,
  "termination": "completed",
  "exit_code": 0,
  "error_kind": null,
  "error_message": null,
  "tokens": {"input": 18161, "output": 2675, "total": 20836},
  "cost_usd": null,
  "cost_note": "未配置 LLM_PRICE_PER_MTOK，只记 token",
  "verification": {
    "hallucination_rate": null,
    "coverage_rate": null,
    "score": null,
    "skipped": false
  },
  "redteam": {"verdict": null, "action": null, "issues": 0},
  "artifacts": {
    "report": "data/reports/20261009_081422.md",
    "annotated": null,
    "trace": null
  }
}
```

**每个字段都要有写入点**——`mini-claude-code` 那条 `test_metrics_have_producers` 是同一个套路，本 Stage 照抄这个约束：台账里出现的键必须指得到一处代码里的写入。

### 4.2 退出原因 `termination` 枚举

| 值 | 含义 | 对应退出码 |
|---|---|---|
| `completed` | 正常完成 | 0 |
| `verify_failed` | 跑完了但验证层异常 | 1 |
| `usage_error` | 参数 / 配置问题 | 2 |
| `rate_limited` | 上游 429 或额度耗尽 | 3 |
| `upstream_unavailable` | 上游 5xx / 连接失败 | 4 |
| `internal_error` | 自己的 bug | 5 |
| `interrupted` | Ctrl-C / SIGTERM | 130 |

---

## 5. 实现落点

| 文件 | 改什么 | 新增/修改 |
|---|---|---|
| `src/insight_agent/runs.py` | **新增**。`RunRecord` 数据类、`new_run_id()`、`RunLedger` 上下文管理器（进入时写 started、退出时写 ended + termination + 退出码） | 新增 |
| `src/insight_agent/exits.py` | **新增**。退出码常量与 `ExitKind` 枚举，两者一一对应 | 新增 |
| `src/insight_agent/cli.py` | `research` 包裹 `RunLedger` + `logging` + try/except 收口；`--json` 补 5 个字段；退出码改用 `exits.py` | 修改 |
| `src/insight_agent/config.py` | 加 `price_per_mtok: float = 0.0`、`log_dir: str = "data/runs"` | 修改 |
| `.env.example` | 补两个新项的说明 | 修改 |
| `tests/test_runs.py` | **新增**。见 §6 | 新增 |
| `README.md` | 补「定时调度怎么接」一节，给 cron / 计划任务 / CI 三种写法 | 修改 |

**不新增依赖。**

---

## 6. 判据（全部离线可跑，不花钱）

假 LLM 用 `tests/` 里已有的模式：不需要真端点就能跑完整个 `research` 流程。

| # | 判据 | 通过标准 |
|---|---|---|
| S1 | 台账的账加得起来 | 跑完一次 `research`（fake），`runs/<run_id>.json` 存在，四个时间/状态字段齐全，且 `run_id` 与日志文件名一致 |
| S2 | 退出码一语义一出口 | 构造 7 种情形（正常/验证异常/参数错/429/5xx/内部异常/中断），逐一断言退出码与台账 `termination` 对应（见 §8 脚本） |
| S3 | 台账键都有写入点 | 遍历 `RunRecord` 的所有字段，每个都能在 `runs.py` / `cli.py` 里 grep 到至少一处赋值 —— 与 `test_metrics_have_producers` 同构 |
| S4 | token 口径与评测层一致 | fake 跑一次的台账 `tokens.total` 与 `eval.metrics.summarize_tokens` 的输出一致（同一个 handler） |
| S5 | 没配单价就不出现钱 | `cost_usd is None` 且 `cost_note` 说明原因；配了 `LLM_PRICE_PER_MTOK` 时才有值，且 = total/1e6 × 单价 |
| S6 | 异常不留半个文件 | 故意在 `graph.invoke` 抛异常，断言台账仍完整（`ended_at` 有值、`termination=internal_error`），不是只剩半截 JSON |
| S7 | 日志不泄密 | 日志与台账都不含 `sk-` / `TAVILY_API_KEY=` / `authorization`（复用 `mini-claude-code` 那边 `_SECRET_LIKE` 的正则思路，本地实现，不跨仓依赖） |
| S8 | `--json` 可被脚本消费 | 退出码 0 时 stdout 是合法 JSON 且含 5 个新字段，能被 `jq -r .run_id` 取出来 |
| S9 | 幂等与并发 | 同一主题连跑两次，`run_id` 不同；`runs/` 下文件数 = 2（`test_run_id_never_collides`） |
| S10 | 定时场景端到端 | 写一个 `scripts/scheduled_run.sh`（等价于 cron 那一行），用假 key 跑通，且它的退出码分支与 S2 一致 |

**T10 的脚本必须真的能被 crontab 引用**——规格里写得出、跑不通，等于没写。

---

## 7. 实施顺序

按"每步都能独立验证"切，不留半成品状态：

1. `exits.py` + `runs.py` 的纯数据层（无 IO）→ S3 过
2. `cli.py` 接 `RunLedger` + 结构化日志 → S1、S6、S9 过
3. try/except 收口 + 退出码 → S2 过
4. token / cost 接进台账 → S4、S5 过
5. `--json` 补字段 → S8 过
6. 脱敏 + `scripts/scheduled_run.sh` → S7、S10 过
7. README 与 `.env.example`

---

## 8. 验收命令

```powershell
# 单元测试（离线，不花钱）
uv run pytest tests/test_runs.py -q

# 全量回归：不能因为新增台账就漏一项
uv run pytest -q

# 定时形态端到端（假 key，只验接线与退出码，不验真实产出）
$env:LLM_BASE_URL="http://127.0.0.1:9/v1"   # 故意指向死端口，走 5 号退出码
bash scripts/scheduled_run.sh "冒烟主题"
echo "exit=$?"                            # 期望 4（上游不可用）

# 台账长什么样
Get-Content data/runs/*.json | ConvertFrom-Json | Select-Object run_id,termination,exit_code,tokens
```

---

## 9. 已知边界（这一节不证明什么）

- **不做并发调度**。要并行得先解决额度与 SQLite 写竞争，`data/fact_cards.db` 是单文件 SQLite，并发写入会锁。
- **台账不加密**。`data/` 在 `.gitignore` 里，但落到磁盘上是明文；真上生产得考虑盘加密或只落元数据不落正文。
- **`error_message` 只留摘要**（前 N 字符），完整 traceback 在 log 文件里。完整信息可能含 URL 参数里的 query token，不进台账。
- **限流判定靠异常类型与状态码**，不同 SDK 抛的异常类不一样，这一层在各 SDK 上都要各自试过才算数——本 Stage 只覆盖 `openai` 系。
- **不做时区处理**，`started_at` 一律 UTC，本地时间看 log 文件里的时间戳。
- **退出码 3/4 的区分依赖上游真的抛 429/5xx**。免费额度耗尽时某些厂商返回的是 200 + body 里带错误信息，这种情况会被归到 `completed`——这不是能靠本地代码解决的，只能问厂商。

---

## 10. 与 JD 的对应

| JD 原文 | 本 Stage 对应 |
|---|---|
| 定时调度 | §2.2 明确不做调度器、只做被调起来的接口；`scripts/scheduled_run.sh` 给出可直接 crontab 的形态 |
| 系统日志管理 | §4.2 运行台账 + §5 结构化日志，`data/runs/` 一次运行一个文件对 |
| 报错拦截 | §6 S2 七种退出情形逐一有码；`error_kind` 沿用评测层已有的词 |
| API 成本监控 | §3 D4 + §6 S4/S5，token 口径与评测层同源，成本不编造 |

---

## 11. as-built（2026-10-09 实测回填）

**测试**：`uv run pytest -q` → **92 passed**（实施前 58，新增 34 项：`tests/test_runs.py` 22 项 + `tests/test_cli_scheduling.py` 12 项）。全部离线，不花钱。

**端到端**（§8 的验收命令实跑）：临时 `.env` 指向死端口 `http://127.0.0.1:9/v1`，跑
`bash scripts/scheduled_run.sh "冒烟主题" --depth fast` → 退出码 **4**，分诊分支打出
「外部依赖不可用，建议退避后重试」，台账落盘且 `termination=upstream_unavailable`、
`error_kind=OpenAIAPIError`。中文主题的 `run_id` 退化成 `topic72a3d` 而不是转义后的中文。

### 实施中发现并修掉的三个真问题

1. **`Bearer xxxxxxxxxxxx` 整个漏出。** `_SECRET_LIKE` 要求前缀后面紧跟 `-` 或 `_`，
   而 `Bearer` 后面是空格，token 原样留在日志里。这是**真的脱敏漏洞** ——
   测试是照着 `_ASSIGN_LIKE` 写的，同一个盲区。补 `_BEARER` 正则，并且**顺序**很关键：
   先吃 `Bearer xxx` 再吃 `KEY=值`，反过来做的话 `authorization: Bearer xxxx` 会先被
   砍成 `authorization=***`，`Bearer` 留在输出里，看着像脱敏过了其实等于没脱。
2. **`TAVILY_API_KEY=xxx` 整个漏出。** `_ASSIGN_LIKE` 用 `\b...\b` 锚词首，而 `_` 是
   词字符，`\b` 在 `_` 后不成立 —— 而 `TAVILY_API_KEY` 正是 `.env` 里的真实写法。
   改成 `[A-Za-z0-9_]*` 前缀通吃。
3. **`cost_note` 空串歧义。** 运行崩在 `invoke` 里时够不到 `set_tokens`，台账上
   `cost_usd: null` + `cost_note: ""` 读起来和「没烧钱」一模一样。默认值改成
   「未计量（运行未走到 token 计量）」，并补一条用例钉住。

### 与规格的两处偏差

- **§4.1 的 `verification` 字段**：规格写的是 `hallucination_rate / claim_score /
  flags`，实现按 `graph/verify.py::VerificationReport.to_dict()` 的真实键改成
  `hallucination_rate / coverage_rate / score / skipped`。规格写错了，以代码为准。
- **§6 S10**：规格说脚本用「假 key 跑通」，实跑是让它**真的失败**（指向死端口走
  退出码 4）。这样才验到分诊分支 —— 假 key 若配了真端点会真的花钱，而要验的
  恰恰是「失败时怎么办」。

### 一条被自己推翻的怀疑（留着，因为它验过东西）

起草本文时曾记下一条 TODO：「真实厂商把 5xx 包成通用 `OpenAIAPIError`，
`classify_exception` 靠 `isinstance` 认不到，会归到 `internal_error(5)` 而不是
`upstream_unavailable(4)`」。

**这条是误判，推翻。** 用 `httpx.MockTransport` 让 SDK 收到真的 502 空 body，
再走完整链路 `ChatOpenAI.invoke`，实测：

```
异常类: OpenAIAPIError
MRO: OpenAIAPIError → InternalServerError → APIStatusError → APIError → OpenAIError → ModelAPIError
status_code: 502
classify_exception → UPSTREAM_UNAVAILABLE   ← 正确
```

`OpenAIAPIError` 继承自 `InternalServerError`（langchain_openai `chat_models/base.py:668`），
而 `InternalServerError` 是 `APIStatusError` 的子类，所以 `isinstance` 认得到、
`status_code` 也还在。之前那次端到端实测的台账记的也正是
`termination=upstream_unavailable / exit_code=4`，与本文 §11 首段一致 ——
是这条 TODO 本身写错了，不是代码错了。

**保留这条的价值**：它证明 §9 那句「限流判定靠异常类型与状态码，不同 SDK 抛的
异常类不一样，这一层在各 SDK 上都要各自试过才算数」不是空话 —— 当时确实
以为有个漏洞，去验了，验完是没有。判据 S2 里那条 5xx 用例从此改成走真实
langchain 链路（`test_a_real_502_through_langchain_is_upstream_unavailable`），
不再用构造的 `APIStatusError`。