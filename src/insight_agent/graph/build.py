"""组装带记忆的并行研究流水线。

拓扑（M7 两个模式的组合：routing + parallelization）：

  START → recall → planner ──┬─ Send×N → research_one(并行) → compress ─┬→ writer → archive → END
                             └────────（无增量时）──────────────────────┘

research_one 的 N 个实例由 LangGraph Send（map-reduce）并行拉起，
全部完成后才进入 compress（superstep 自动屏障）。
"""

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from insight_agent.config import Settings
from insight_agent.graph.nodes import (
    make_archive,
    make_compress,
    make_planner,
    make_recall,
    make_research_one,
    make_writer,
)
from insight_agent.graph.state import ResearchState
from insight_agent.memory.notes import NotesStore


def route_after_planner(state: dict):
    """有增量 → 按 Send 并行分发子问题；没有 → 直接用旧档案写报告。"""
    brief = state.get("brief", [])
    if not brief:
        return "writer"
    return [Send("research_one", {"question": q}) for q in brief]


def build_research_graph(
    llm: ChatOpenAI,
    notes_store: NotesStore | None = None,
    settings: Settings | None = None,
) -> CompiledStateGraph:
    from insight_agent.config import load_settings
    from insight_agent.graph.verify import make_verify_node
    from insight_agent.tools.embedder import get_embedder_cached

    store = notes_store or NotesStore()
    cfg = settings or load_settings()
    # 共享证据池：research_one 写入深读 chunk，verify 读取做逐句核对
    pool = EvidencePool()
    pool.bind_embedder(get_embedder_cached(cfg))

    verify_on = cfg.verify_enabled == "true" or (
        cfg.verify_enabled == "auto" and cfg.research_depth != "fast"
    )

    builder = StateGraph(ResearchState)
    builder.add_node("recall", make_recall(store))
    builder.add_node("planner", make_planner(llm))
    builder.add_node("research_one", make_research_one(llm, cfg, pool))
    builder.add_node("compress", make_compress(llm))
    builder.add_node("writer", make_writer(llm))
    builder.add_node("verify", make_verify_node(llm, verify_on, cfg.verify_max_claims, cfg.verify_concurrency, pool))
    builder.add_node("archive", make_archive(store))

    builder.add_edge(START, "recall")
    builder.add_edge("recall", "planner")
    builder.add_conditional_edges("planner", route_after_planner)
    builder.add_edge("research_one", "compress")
    builder.add_edge("compress", "writer")
    builder.add_edge("writer", "verify")
    builder.add_edge("verify", "archive")
    builder.add_edge("archive", END)
    return builder.compile()
