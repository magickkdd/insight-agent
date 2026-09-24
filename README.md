# Insight Agent · 行业洞察研究助手

基于 **LangGraph 官方组件**构建的行业研究 Agent：输入一个主题，自动完成
**规划提纲 → 联网取证 → 证据压缩 → 结构化报告**，全程可观测、可评测、可回归。

> 求职作品集项目。姊妹项目 [agent-framework](https://github.com/magickkdd/agent-framework)
> 是从零手写的教学版框架（State 通道 / Tool Registry / 三层记忆 / 评测门禁），
> 本项目则是"理解原理之后，用成熟框架快速交付"的工程实践。

## 架构

```
topic
  ↓
recall      检索主题研究档案（长期记忆，重复研究只补增量）
  ↓
planner     LLM 结构化输出 → 增量研究提纲（无缺口则跳过研究）
  ↓ 路由
researcher  预制 create_agent + Tavily 联网取证（错误自愈重试）
  ↓
compress    超长证据 LLM 摘要压缩（上下文工程）
  ↓
writer      结构化报告（引用纪律：禁止丢链接、禁止编造）
  ↓
archive     新证据原子写回档案库（知识沉淀）
```

**Workflow 设计**：控制流由图拓扑定死（chaining + routing 模式），
LLM 只在节点内工作 —— 牺牲灵活性换可预测、可评测、可调试。

## 亮点

1. **标准版评测基准**（`eval/`）：30 条用例分 unit/e2e 两层；
   四维指标 = 成功率（结构断言 + LLM-as-judge）/ Token（官方 Usage 回调）/ 时延 / **回归**（与上次报告自动 diff，Markdown 报告直接进 PR）
2. **长期记忆**：研究档案按主题沉淀，覆盖度地图（digest）驱动增量规划，避免重复研究
3. **流式可观测**：FastAPI SSE 逐节点推送进度，浏览器实时可见
4. **错误哲学**：模型错误 → 信息回传自愈重试；环境错误（限流）→ 与结果分型，不污染评测

## 快速开始

```bash
cp .env.example .env          # 填入 LLM 与 Tavily 的 key
uv sync
uv run uvicorn insight_agent.api.app:app --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

```bash
uv run python -m insight_agent.eval --tier unit   # 快速回归（分钟级）
uv run python -m insight_agent.eval --tier e2e    # 全流水线基准
```

Docker：

```bash
docker build -t insight-agent .
docker run -p 8000:8000 --env-file .env insight-agent
```

## 评测报告（节选）

见 `eval/runs/report_*.md`。评测上线即抓到 3 个真实缺陷并修复：
Token 计量失效（回调传播断裂）、planner 提纲数量违规（带反馈重试）、
writer 引用丢失 URL（引用纪律写进提示词）—— 这就是评测体系的价值。

## 技术栈

LangGraph 1.2 · langchain 1.x agents · langchain-openai（任意 OpenAI 兼容端点）
· langchain-tavily · FastAPI SSE · pytest · Docker · uv
