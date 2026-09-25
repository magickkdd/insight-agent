# Insight Agent · 行业洞察研究助手

基于 **LangGraph 官方组件**构建的可验证研究 Agent：输入一个主题，自动完成
**规划 → 多源搜索 → 深读网页 → 并行取证 → 逐句核验 → 结构化报告**，
带长期语义记忆、成本档位与标准版评测基准。

> 求职作品集项目。姊妹项目 [agent-framework](https://github.com/magickkdd/agent-framework)
> 是从零手写的教学版框架（State 通道 / Tool Registry / 三层记忆 / 评测门禁），
> 本项目是"理解原理之后，用成熟框架快速交付"的工程实践。

## 架构（八节点 + 两个横切层）

```
topic
  ↓
recall        双通道记忆：主题档案（精确） + 事实卡片（sqlite-vec 语义召回）
  ↓
planner       结构化输出 → 增量研究提纲（对照档案目录/事实卡去重，无缺口则跳过）
  ↓ Send ×N（map-reduce 并行）
research_one  多源搜索路由 → LLM 选 URL → 并行深读网页 → 语义检索段落 → 证据笔记
  ↓
compress      超长证据 LLM 摘要压缩（上下文工程）
  ↓
gap_analyzer  证据充分度判定：不足且轮数未满 → 再派一轮（deep 档，MAX_ROUNDS 不变量）
  ↓
writer        结构化报告（引用纪律：禁止丢链接、禁止编造）
  ↓
verify        逐句核验：claim 与证据段比对 → 幻觉率 / 可信度评分 / ⚠️ 标注版
  ↓
archive       双版本报告落盘 + 证据入档案库 + supported claim 转事实卡片
```

横切层：
- **搜索路由**：Tavily / 博查 / DuckDuckGo 链式降级，熔断（连续 3 败冷却 60s）+ CJK 语言置顶
- **LLM 工厂**：cloud / local（Ollama）/ hybrid（judge 走本地）三档 profile，Langfuse trace 自动挂载

### 入口矩阵（同一 graph、同一记忆层）

| 入口 | 命令 | 场景 |
|---|---|---|
| Web（SSE 可观测） | `serve` 命令 | 演示：六阶段实时推进 + 报告渲染 |
| CLI | `research "主题" --depth standard --json` | 脚本/程序化调用 |
| MCP Server | `python -m insight_agent.mcp_server` | Claude Code / 任何 MCP 客户端调用 |

## 亮点

1. **可验证的引用**：证据来自网页正文段落（非搜索摘要），报告经 claim 级核验，
   输出幻觉率数字与 ⚠️ 标注版 —— "Agent 能量化自己有多可信"
2. **验证层是记忆层的质检关**：只有核验通过（supported/partial）的事实才转成
   FactCard 进长期语义记忆（sqlite-vec 向量检索，跨主题召回，重复报道自动 superseded）
3. **标准版评测基准**（`eval/`）：35 条用例分 unit/e2e 两层；
   四维指标 = 成功率（结构断言 + LLM 多票 judge）/ Token（官方 Usage 回调）/ 时延 / **回归**（自动 diff 上次报告）
4. **多智能体并行**：planner + N 个并行研究工人 + writer，LangGraph Send map-reduce
5. **降级哲学**：搜索三家熔断、抓取六态、嵌入三档、验证失败不拦交付 —— 每个外部依赖都有退路且显式标注

## 快速开始

```bash
cp .env.example .env          # 填入 LLM 与 Tavily 的 key
uv sync
uv run python -m insight_agent.cli serve --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

单次研究（可编程接口）：

```bash
uv run python -m insight_agent.cli research "低空经济的政策进展" --depth standard --json
```

MCP 接入（Claude Code 配置示例）：

```json
{"mcpServers": {"insight-agent": {"command": "uv", "args": ["run", "--project",
  "/path/to/insight-agent", "python", "-m", "insight_agent.mcp_server"]}}}
```

## 成本档位

| 档位 | 深读篇数/子问题 | 验证层 | 迭代深研 | 适用 |
|---|---|---|---|---|
| fast | 0（只用摘要） | 关 | 关 | 快速迭代、额度紧张 |
| standard | 2 | 开 | 关 | 日常使用 |
| deep | 5 | 开 | 开（MAX_ROUNDS=3） | 高质量交付 |

`RESEARCH_DEPTH=deep uv run ...` 或 `--depth deep` 切换。

## 评测基准

```bash
uv run python -m insight_agent.eval --tier unit   # 快速回归（分钟级）
uv run python -m insight_agent.eval --tier e2e    # 全流水线基准（含幻觉率）
```

35 条用例（12 planner + 10 writer + 13 e2e/golden set）。
报告含**幻觉率**列、judge 多票一致率、与上次运行的回归 diff。
评测上线期间实测抓到并修复 4 个缺陷：Token 计量失效、planner 提纲数违规、
writer 引用丢 URL、trafilatura 2.x API 变更 —— 详见 `eval/runs/report_*.md`。

## Docker

```bash
docker build -t insight-agent .
docker run -p 8000:8000 --env-file .env insight-agent
docker compose up -d langfuse-db langfuse   # 可选：本地 Langfuse trace
```

## 技术栈

LangGraph 1.2（Send map-reduce / conditional edges）· langchain 1.x agents ·
langchain-openai（任意 OpenAI 兼容端点 + Ollama）· langchain-tavily · ddgs ·
trafilatura · sqlite-vec · FastAPI SSE · Langfuse · typer · mcp 2.x · pytest · Docker · uv
