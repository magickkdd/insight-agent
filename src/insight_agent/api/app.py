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
from insight_agent.memory.notes import NotesStore

settings = load_settings()
llm = ChatOpenAI(
    model=settings.llm_model,
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    temperature=0,
)
graph = build_research_graph(llm, notes_store=NotesStore())

NODE_LABELS = {
    "recall": "检索历史研究档案",
    "planner": "规划研究提纲",
    "researcher": "联网搜索取证",
    "compress": "压缩证据笔记",
    "writer": "撰写结构化报告",
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
async def research_stream(topic: str = Query(min_length=2, max_length=200)):
    """SSE：逐节点推送研究进度，最后推送完整报告。"""

    async def generate():
        yield _sse("start", {"topic": topic})
        report = None
        try:
            async for chunk in graph.astream({"topic": topic}, stream_mode="updates"):
                for node, delta in chunk.items():
                    payload = {"node": node, "label": NODE_LABELS.get(node, node)}
                    if node == "planner":
                        payload["brief"] = delta.get("brief", [])
                    if node == "researcher":
                        payload["chars"] = len(delta.get("findings", ""))
                    if node == "writer":
                        report = delta.get("report", "")
                        payload["chars"] = len(report)
                    yield _sse("stage", payload)
            yield _sse("report", {"markdown": report or ""})
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
