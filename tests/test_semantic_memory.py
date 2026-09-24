"""Wave3 零成本单测：事实卡片库（KNN/淘汰）+ gap_analyzer 旁路。"""

from insight_agent.memory.fact_cards import FactCard, FactCardStore
from insight_agent.tools.embedder import KeywordEmbedder


def _store(tmp_path):
    return FactCardStore(tmp_path / "cards.db", embedder=KeywordEmbedder())


def test_fact_card_roundtrip_and_knn(tmp_path):
    store = _store(tmp_path)
    store.add(
        FactCard(claim="固态电池2026年小批量装车", source_url="https://e.com/1",
                 source_quote="原文片段", topic="固态电池", confidence=1.0)
    )
    store.add(
        FactCard(claim="人形机器人进入量产元年", source_url="https://e.com/2",
                 source_quote="原文片段", topic="机器人", confidence=1.0)
    )
    assert store.count() == 2
    hits = store.search("固态电池什么时候量产装车", top_k=1)
    assert len(hits) == 1
    assert "固态电池" in hits[0].claim  # 语义最近的事实排第一


def test_superseded_marks_duplicate_report(tmp_path):
    """同一事实被新来源重复报道：旧卡标记 superseded，检索只出最新来源。

    注：语义级近似去重（不同措辞的同一事实）依赖 embedding 档；
    keyword 档只能确定性覆盖"文本重合"的场景 —— 这是档位差异的诚实边界。
    """
    store = _store(tmp_path)
    claim = "某市场2025年规模为100亿元"
    old_id = store.add(FactCard(claim=claim, source_url="https://e.com/old",
                                source_quote="q", topic="t", confidence=1.0))
    new_id = store.add(FactCard(claim=claim, source_url="https://e.com/new",
                                source_quote="q", topic="t", confidence=1.0))
    superseded_ids = {
        r[0]
        for r in store._conn.execute("SELECT card_id FROM superseded").fetchall()
    }
    assert old_id in superseded_ids          # 旧卡留痕标记
    assert new_id not in superseded_ids
    hits = store.search(claim, top_k=5)
    assert [h.id for h in hits] == [new_id]  # 检索只出最新来源


def test_gap_analyzer_passthrough_non_deep(tmp_path, monkeypatch):
    import os

    from insight_agent.config import Settings
    from insight_agent.graph.nodes import make_gap_analyzer

    monkeypatch.setenv("RESEARCH_DEPTH", "standard")
    settings = Settings(
        llm_base_url="https://x", llm_api_key="k", llm_model="m",
        tavily_api_key="t", research_depth="standard",
    )
    node = make_gap_analyzer(None, settings)  # 非 deep 档不调 LLM
    out = node({"rounds": 1, "brief": ["问题"], "compressed_findings": "证据"})
    assert out == {"rounds": 1, "brief": []}  # 旁路：直接放行去 writer
