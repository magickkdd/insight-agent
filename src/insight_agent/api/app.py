"""FastAPI SSE 后端：研究流水线的流式 HTTP 服务。

核心机制：LangGraph 官方 astream(stream_mode="updates") 在每个节点
完成后产出增量 → 翻译成 SSE 事件推给浏览器。
演示价值：用户实时看到 检索档案→规划→搜索→撰写→归档 的推进过程，
而不是对着转圈的白屏干等 —— Agent 产品的可观测性门面。
"""

import json
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_openai import ChatOpenAI

from insight_agent.config import load_settings
from insight_agent.graph.build import build_research_graph
from insight_agent.graph.llm_factory import get_llm
from insight_agent.memory.chat_history import ChatHistoryStore
from insight_agent.memory.notes import NotesStore

settings = load_settings()
llm = get_llm("main", settings)
chat_store = ChatHistoryStore()
graph = build_research_graph(llm, notes_store=NotesStore(), settings=settings, chat_store=chat_store)

NODE_LABELS = {
    "gate": "判断研究意图",
    "smalltalk": "对话回复",
    "recall": "检索历史研究档案",
    "planner": "规划研究提纲",
    "research_one": "并行联网取证",
    "compress": "压缩证据笔记",
    "writer": "撰写结构化报告",
    "verify": "逐句核验可信度",
    "archive": "归档到笔记库",
}

app = FastAPI(title="Insight Agent API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 演示后端，生产环境应收敛为白名单
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/api/research/stream")
async def research_stream(
    topic: str = Query(min_length=2, max_length=200),
    session_id: str = Query(default="web"),
    depth: str = Query(default="fast"),
    focus: str = Query(default=""),
):
    """SSE：逐节点推送研究进度，最后推送完整报告。

    session_id 由前端 localStorage 维持 —— 同一会话的闲聊才有记忆。
    分级交付：概览 depth=fast（纯摘要，快）；深入 depth=standard + focus=<子问题>。
    """
    if depth not in ("fast", "standard", "deep"):
        depth = "fast"

    async def generate():
        yield _sse("start", {"topic": topic, "depth": depth, "focus": focus})
        report = None
        direct_reply = ""
        try:
            async for chunk in graph.astream(
                {"topic": topic, "session_id": session_id, "depth": depth, "focus": focus},
                stream_mode="updates",
            ):
                for node, delta in chunk.items():
                    payload = {"node": node, "label": NODE_LABELS.get(node, node)}
                    if node == "gate":
                        is_research = delta.get("is_research", True)
                        payload["is_research"] = is_research
                        payload["reason"] = delta.get("gate_reason", "")
                        payload["detail"] = ("研究请求" if is_research else "闲聊直答") + " · " + payload["reason"]
                    if node == "smalltalk":
                        direct_reply = delta.get("direct_reply", "")
                        payload["chars"] = len(direct_reply)
                        payload["detail"] = f"回复 {len(direct_reply)} 字"
                    if node == "recall":
                        notes = delta.get("existing_notes", "")
                        cards = delta.get("fact_cards", [])
                        detail = "命中 %d 字历史档案" % len(notes) if notes else "无历史档案，从头研究"
                        if cards:
                            detail += f" · 召回 {len(cards)} 条相关事实卡"
                        payload["detail"] = detail
                    if node == "planner":
                        payload["brief"] = delta.get("brief", [])
                    if node == "research_one":
                        payload["chars"] = sum(len(f) for f in delta.get("findings", []))
                    if node == "compress":
                        payload["detail"] = f"压缩后 {len(delta.get('compressed_findings', ''))} 字"
                    if node == "gap_analyzer":
                        more = delta.get("brief", [])
                        payload["detail"] = (
                            f"证据不足，追加 {len(more)} 个子问题再取证" if more else "证据充分，进入撰写"
                        )
                    if node == "writer":
                        report = delta.get("report", "")
                        payload["chars"] = len(report)
                        payload["detail"] = f"报告 {len(report)} 字"
                    if node == "verify":
                        v = delta.get("verification", {})
                        parts = []
                        if v.get("hallucination_rate") is not None:
                            parts.append(f"幻觉率 {v['hallucination_rate']:.1%}")
                        if v.get("score") is not None:
                            parts.append(f"可信度 {v['score']}")
                        if parts:
                            payload["detail"] = " · ".join(parts)
                    if node == "archive":
                        payload["detail"] = "已写入笔记库与报告目录"
                    yield _sse("stage", payload)
            yield _sse("report", {"markdown": report or direct_reply or ""})
        except Exception as e:  # noqa: BLE001 - 流里炸了也要把错误推给前端
            yield _sse("error", {"message": f"{type(e).__name__}: {e}"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# 静态前端（放在路由后面，避免吞掉 /api）
_web_dir = Path(__file__).parents[3] / "web"
if _web_dir.exists():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
