"""Insight Agent：行业洞察研究 Agent（求职作品集项目）。

LangGraph 官方组件快速开发 + 标准版评测基准。
架构：
  src/insight_agent/
    graph/    LangGraph 编排（规划→搜索→阅读→综合→报告）
    tools/    搜索（Tavily + DuckDuckGo）与网页抓取
    memory/   研究笔记沉淀与上下文压缩
    api/      FastAPI SSE 后端
  eval/       评测基准（成功率 / Token / 时延 / 回归）
  web/        演示前端
"""
