"""ChatHistoryStore 单测：落盘持久化、滚动窗口、危险 session_id 清洗、坏文件兜底。"""

from insight_agent.memory.chat_history import ChatHistoryStore


def test_roundtrip(tmp_path):
    store = ChatHistoryStore(tmp_path)
    store.append("s1", "我叫小明", "你好小明！")
    store.append("s1", "我叫什么", "你叫小明。")
    turns = store.load("s1")
    assert len(turns) == 2
    assert turns[0] == {"user": "我叫小明", "assistant": "你好小明！"}
    assert turns[1]["user"] == "我叫什么"


def test_session_isolation(tmp_path):
    store = ChatHistoryStore(tmp_path)
    store.append("a", "你好", "嗨")
    store.append("b", "再见", "拜拜")
    assert len(store.load("a")) == 1
    assert store.load("b")[0]["user"] == "再见"


def test_rolling_window(tmp_path):
    store = ChatHistoryStore(tmp_path, max_turns=2)
    for i in range(3):
        store.append("s", f"第{i}句", f"答{i}")
    turns = store.load("s")
    assert len(turns) == 2  # 只留最近 2 轮
    assert turns[0]["user"] == "第1句"


def test_dangerous_session_id_sanitized(tmp_path):
    store = ChatHistoryStore(tmp_path)
    store.append("../../evil", "hi", "hello")  # 路径穿越尝试
    assert list(tmp_path.rglob("*.json")) != []  # 文件落在 store 根内
    assert not (tmp_path.parent / "evil.json").exists()


def test_corrupt_file_returns_empty(tmp_path):
    store = ChatHistoryStore(tmp_path)
    (tmp_path / "broken.json").write_text("{ 不是json", encoding="utf-8")
    assert store.load("broken") == []
