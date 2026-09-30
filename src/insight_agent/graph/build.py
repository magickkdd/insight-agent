"""组装带记忆的并行研究流水线。

拓扑（M7 两个模式的组合：routing + parallelization + redteam 修订闭环）：

  START → recall → planner ──┬─ Send×N → research_one(并行) → compress ─┬→ writer → verify → redteam ─┬─ pass ─→ archive → END
                             └────────（无增量时）──────────────────────┘                              ├─ revise 且轮数未满 → writer（带 issues 修订）
                                                                                                      └─ revise 且轮数耗尽 → archive（降级标注）

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
    make_gate,
    make_gap_analyzer,
    make_planner,
    make_recall,
    make_research_one,
    make_smalltalk,
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


def route_after_gate(state: dict):
    """入口分流：研究请求 → 流水线；闲聊 → 直答后结束。"""
    if state.get("is_research", True):
        return "recall"
    return "smalltalk"


def route_after_redteam(state: dict):
    """审查判定权在 redteam 节点（它持有轮数上限），条件边只读决策结果：
    revise=打回 writer 修订；degrade=轮数耗尽降级归档；review=放行。"""
    if (state.get("redteam") or {}).get("action") == "revise":
        return "writer"
    return "archive"


def build_research_graph(
    llm: ChatOpenAI,
    notes_store: NotesStore | None = None,
    settings: Settings | None = None,
    chat_store=None,
) -> CompiledStateGraph:
    """chat_store（ChatHistoryStore）只服务闲聊分支的对话记忆；
    传 None 时 gate/smalltalk 退化为无记忆行为（eval 等单轮场景够用）。"""
    from insight_agent.config import load_settings
    from insight_agent.graph.llm_factory import get_llm
    from insight_agent.graph.redteam import make_redteam_node
    from insight_agent.graph.verify import make_verify_node
    from insight_agent.memory.evidence_pool import EvidencePool
    from insight_agent.memory.fact_cards import FactCardStore
    from insight_agent.tools.embedder import get_embedder_cached

    store = notes_store or NotesStore()
    cfg = settings or load_settings()
    embedder = get_embedder_cached(cfg)
    # 共享证据池：research_one 写入深读 chunk，verify 读取做逐句核对
    pool = EvidencePool()
    pool.bind_embedder(embedder)
    # 语义记忆：verify 产出的 supported claim 自动入卡（B→C 联动）
    card_store = FactCardStore(embedder=embedder)

    # verify 开关原始值传给节点：auto 档在运行时按请求 depth 判定（概览 fast 跳过验证）
    verify_flag = cfg.verify_enabled

    # redteam 审查走强模型（REDTEAM_SPEC §3：对抗审查的质量下限比成本更敏感）——
    # cloud/hybrid 档复用主 LLM（hybrid 语义下 redteam 不像 judge 那样降本地）；
    # 仅 local 档借 judge 通道取本地模型（离线演示场景）
    redteam_llm = llm if cfg.redteam_profile in ("cloud", "hybrid") else get_llm("judge", cfg)

    builder = StateGraph(ResearchState)
    builder.add_node("gate", make_gate(llm, cfg, chat_store))
    builder.add_node("smalltalk", make_smalltalk(llm, chat_store, cfg))
    builder.add_node("recall", make_recall(store, card_store))
    builder.add_node("planner", make_planner(llm))
    builder.add_node("research_one", make_research_one(llm, cfg, pool))
    builder.add_node("compress", make_compress(llm))
    builder.add_node("gap_analyzer", make_gap_analyzer(llm, cfg))
    builder.add_node("writer", make_writer(llm))
    builder.add_node(
        "verify",
        make_verify_node(llm, verify_flag, cfg.verify_max_claims, cfg.verify_concurrency, pool, card_store),
    )
    builder.add_node("redteam", make_redteam_node(redteam_llm, cfg, pool))
    builder.add_node("archive", make_archive(store))

    builder.add_edge(START, "gate")
    builder.add_conditional_edges("gate", route_after_gate)
    builder.add_edge("smalltalk", END)
    builder.add_edge("recall", "planner")
    builder.add_conditional_edges("planner", route_after_planner)
    builder.add_edge("research_one", "compress")
    builder.add_edge("compress", "gap_analyzer")
    builder.add_conditional_edges("gap_analyzer", route_after_planner)
    builder.add_edge("writer", "verify")
    builder.add_edge("verify", "redteam")
    builder.add_conditional_edges("redteam", route_after_redteam)
    builder.add_edge("archive", END)
    return builder.compile()
