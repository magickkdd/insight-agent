"""记忆层单测（零 API 成本）：笔记库读写 + 压缩旁路。"""

from insight_agent.graph.nodes import make_compress
from insight_agent.memory.notes import NotesStore


def test_notes_store_roundtrip(tmp_path):
    store = NotesStore(tmp_path)
    assert store.load_notes("测试主题") == ""  # 无档案

    store.merge("测试主题", "证据A：某事实 [来源](url1)")
    store.merge("测试主题", "证据B：另一事实 [来源](url2)")

    notes = store.load_notes("测试主题")
    assert "证据A" in notes and "证据B" in notes
    assert "增量研究" in notes  # 第二次合并追加为增量章节


def test_slug_stable_and_distinct(tmp_path):
    from insight_agent.memory.notes import _slug

    assert _slug("AI Agent 格局 2026") == _slug("AI  Agent   格局2026")  # 空白/大小写不敏感
    assert _slug("主题一") != _slug("主题二")


def test_compress_passthrough_without_llm():
    """短证据直接旁路，不触发 LLM 调用 —— llm 传 None 也不该报错。"""
    node = make_compress(None)
    out = node({"findings": ["短证据A", "短证据B"]})
    assert out["compressed_findings"] == "短证据A\n\n短证据B"
