"""MCP Server 导出（UPGRADE_SPEC §5）：把研究能力提供给任何 MCP 客户端。

三种入口共用同一 graph 与记忆层：
  python -m insight_agent.mcp_server            # stdio（Claude Code / Claude Desktop）
  python -m insight_agent.mcp_server --http     # streamable-http（远程，端口 8001）

工具清单：
  research(topic, depth)   完整报告（含验证标注），分钟级长任务
  get_notes(topic)         该主题档案（笔记摘要 + 最近报告）
  list_archives()          全部已研究主题

并发保护：研究类工具全局信号量 = 1，防止多个客户端同时打爆免费额度。
"""

import argparse
import asyncio
import threading

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="insight-agent",
    description="行业洞察研究 Agent：规划→联网取证→验证→结构化报告",
)

_research_semaphore = threading.Semaphore(1)  # 不变量：研究并发 = 1（额度保护）


def _get_graph():
    from insight_agent.config import load_settings
    from insight_agent.graph.build import build_research_graph
    from insight_agent.graph.llm_factory import get_llm
    from insight_agent.memory.notes import NotesStore

    settings = load_settings()
    return build_research_graph(
        get_llm("main", settings), notes_store=NotesStore(), settings=settings
    )


@server.tool()
def list_archives() -> str:
    """列出全部已研究主题及其档案信息。"""
    import json
    from pathlib import Path

    from insight_agent.memory.notes import _slug

    root = Path("data/notes")
    if not root.exists():
        return "尚无研究档案"
    archives = []
    for p in sorted(root.glob("*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        archives.append({"topic": doc["topic"], "updates": len(doc.get("updates", []))})
    return json.dumps(archives, ensure_ascii=False, indent=2)


@server.tool()
def get_notes(topic: str) -> str:
    """获取某主题的研究档案（覆盖度目录 + 最近报告）。"""
    from insight_agent.memory.notes import NotesStore

    doc = NotesStore().load_notes(topic)
    if not doc:
        return f"主题「{topic}」尚无研究档案。可先调用 research 工具。"
    return doc[:4000]  # 上限截断：MCP 响应体不宜过大


@server.tool()
def research(topic: str, depth: str = "standard") -> str:
    """对指定主题执行完整研究，返回结构化 Markdown 报告（含验证标注）。长任务（约 1-3 分钟）。"""
    import os
    import time

    if depth not in ("fast", "standard", "deep"):
        return f"非法 depth: {depth}（fast/standard/deep）"
    os.environ["RESEARCH_DEPTH"] = depth

    acquired = _research_semaphore.acquire(timeout=1)
    if not acquired:
        return "已有研究任务在执行中，请稍后再试（并发上限 1，保护免费额度）。"
    try:
        from insight_agent.graph.nodes import DEPTH_MAX_DOCS  # noqa: F401 深读篇数随环境变量

        graph = _get_graph()
        t0 = time.time()
        result = graph.invoke({"topic": topic})
        elapsed = time.time() - t0
        verification = result.get("verification", {})
        return (
            f"{result['report']}\n\n---\n"
            f"[研究耗时 {elapsed:.0f}s | 提纲 {len(result['brief'])} 条 | "
            f"幻觉率 {verification.get('hallucination_rate')} | 可信度 {verification.get('score')}]"
        )
    finally:
        _research_semaphore.release()


@server.tool()
async def research_async(topic: str, depth: str = "standard") -> str:
    """异步版 research：在线程池里跑同步图，不阻塞事件循环。"""
    return await asyncio.to_thread(research, topic, depth)


def main() -> None:
    parser = argparse.ArgumentParser(description="Insight Agent MCP Server")
    parser.add_argument("--http", action="store_true", help="以 streamable-http 方式监听（默认 stdio）")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if args.http:
        server.run(transport="streamable-http", host="0.0.0.0", port=args.port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
