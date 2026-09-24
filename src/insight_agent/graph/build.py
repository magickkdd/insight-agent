"""组装带记忆的研究流水线。

拓扑（M7 routing 的应用：planner 之后按增量需求分流）：

  START → recall → planner ─┬→ researcher → compress ─┬→ writer → archive → END
                            └──────（无增量时）────────┘
"""

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from insight_agent.graph.nodes import (
    make_archive,
    make_compress,
    make_planner,
    make_recall,
    make_researcher,
    make_writer,
)
from insight_agent.graph.state import ResearchState
from insight_agent.memory.notes import NotesStore


def route_after_planner(state: dict) -> str:
    """有增量 → 去研究；没有 → 直接用旧档案写报告。"""
    return "researcher" if state.get("brief") else "writer"


def build_research_graph(
    llm: ChatOpenAI, notes_store: NotesStore | None = None
) -> CompiledStateGraph:
    store = notes_store or NotesStore()
    builder = StateGraph(ResearchState)
    builder.add_node("recall", make_recall(store))
    builder.add_node("planner", make_planner(llm))
    builder.add_node("researcher", make_researcher(llm))
    builder.add_node("compress", make_compress(llm))
    builder.add_node("writer", make_writer(llm))
    builder.add_node("archive", make_archive(store))

    builder.add_edge(START, "recall")
    builder.add_edge("recall", "planner")
    builder.add_conditional_edges("planner", route_after_planner)
    builder.add_edge("researcher", "compress")
    builder.add_edge("compress", "writer")
    builder.add_edge("writer", "archive")
    builder.add_edge("archive", END)
    return builder.compile()
