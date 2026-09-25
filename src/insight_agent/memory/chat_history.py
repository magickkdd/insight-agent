"""闲聊分支的短期对话记忆（working memory 产品化）。

研究流水线是单次任务（产出已落盘档案/事实卡），无需对话记忆；
但闲聊分支天然多轮 —— 没记忆就是 M3 失忆实验的产品复现：
上一轮"我叫小明"，下一轮问"我叫什么"就答不上来。

ChatHistoryStore 按 session_id 存最近 max_turns 轮到
data/chat_history/{sid}.json，磁盘持久化，重启不丢。
写入用"临时文件+原子替换"（与 NotesStore 同款纪律）。
"""

import json
import re
from pathlib import Path

_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


class ChatHistoryStore:
    def __init__(self, root: str | Path = "data/chat_history", max_turns: int = 10):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self.max_turns = max_turns

    def _path(self, session_id: str) -> Path:
        sid = _SAFE.sub("_", session_id)[:64] or "default"
        return self._root / f"{sid}.json"

    def load(self, session_id: str) -> list[dict]:
        """取某会话的全部历史轮次（[{user, assistant}, ...]）。无/损坏返回空。"""
        p = self._path(session_id)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []  # 坏会话文件当空处理：闲聊不值得 fail-fast

    def append(self, session_id: str, user_msg: str, assistant_msg: str) -> None:
        """追加一轮对话，超出窗口的旧轮次滚动丢弃。"""
        turns = self.load(session_id)
        turns.append({"user": user_msg, "assistant": assistant_msg})
        p = self._path(session_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(turns[-self.max_turns:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)  # 原子写入，崩溃不留半截文件
