"""语义记忆：FactCard 事实卡片 + sqlite-vec 向量检索（UPGRADE_SPEC §9）。

写入闭环：verify 节点判定的 supported/partial claim 自动转卡片入库
（验证层是记忆层的质检关）。冲突处理：语义高度相似的旧卡标记
superseded（不删除，留痕），保留演进历史。
"""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import sqlite_vec

_SIMILAR_L2_THRESHOLD = 0.4  # 归一化向量下，L2 < 0.4 ≈ 余弦相似度 > 0.92


@dataclass
class FactCard:
    claim: str
    source_url: str
    source_quote: str
    topic: str
    confidence: float          # supported=1.0 / partial=0.6
    created_at: str = ""
    id: str = ""
    superseded: bool = False


class FactCardStore:
    """sqlite-vec 后端的事实卡片库（单文件、零服务）。"""

    def __init__(self, path: str | Path = "data/fact_cards.db", embedder=None):
        self._embedder = embedder
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._dim = self._resolve_dim()
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS fact_cards USING vec0("
            f"id TEXT PRIMARY KEY, embedding float[{self._dim}], payload TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS superseded "
            "(card_id TEXT PRIMARY KEY, reason TEXT, at TEXT)"
        )
        self._conn.commit()

    def _resolve_dim(self) -> int:
        if self._embedder is None:
            return 512  # keyword 档默认维度
        probe = self._embedder.embed(["维度探针"])
        return len(probe[0])

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if self._embedder is None:
            raise RuntimeError("FactCardStore 未配置嵌入器")
        return self._embedder.embed(texts)

    def add(self, card: FactCard) -> str:
        """入库一张卡片；与高置信旧卡语义高度相似时标记旧卡 superseded。"""
        card.id = card.id or uuid.uuid4().hex
        card.created_at = card.created_at or time.strftime("%Y-%m-%d %H:%M")
        vec = self._embed([card.claim])[0]

        # 冲突检测：KNN 找最近的未淘汰卡片
        rows = self._conn.execute(
            "SELECT id, distance, payload FROM fact_cards WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT 1",
            (json.dumps(vec),),
        ).fetchall()
        if rows:
            old_id, distance, payload = rows[0]
            old = json.loads(payload)
            if (
                distance < _SIMILAR_L2_THRESHOLD
                and not old.get("superseded")
                and card.confidence >= old.get("confidence", 0)
                and old["source_url"] != card.source_url
            ):
                self._conn.execute(
                    "INSERT OR REPLACE INTO superseded VALUES (?, ?, ?)",
                    (old_id, f"被更新事实取代：{card.claim[:60]}", card.created_at),
                )
                old["superseded"] = True
                self._conn.execute("DELETE FROM fact_cards WHERE id = ?", (old_id,))
                self._conn.execute(
                    "INSERT INTO fact_cards VALUES (?, ?, ?)",
                    (old_id, json.dumps(old.get("embedding", vec)), json.dumps(old, ensure_ascii=False)),
                )

        payload = {
            "claim": card.claim,
            "source_url": card.source_url,
            "source_quote": card.source_quote,
            "topic": card.topic,
            "confidence": card.confidence,
            "created_at": card.created_at,
            "superseded": card.superseded,
            "embedding": vec,
        }
        self._conn.execute(
            "INSERT INTO fact_cards VALUES (?, ?, ?)",
            (card.id, json.dumps(vec), json.dumps(payload, ensure_ascii=False)),
        )
        self._conn.commit()
        return card.id

    def search(self, query: str, *, top_k: int = 5) -> list[FactCard]:
        """语义检索（KNN），过滤已淘汰卡片。"""
        if self._embedder is None:
            return []
        vec = self._embed([query])[0]
        rows = self._conn.execute(
            "SELECT id, distance, payload FROM fact_cards WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (json.dumps(vec), top_k * 3),
        ).fetchall()
        out: list[FactCard] = []
        superseded_ids = {r[0] for r in self._conn.execute("SELECT card_id FROM superseded")}
        for _id, _distance, payload in rows:
            data = json.loads(payload)
            if _id in superseded_ids or data.get("superseded"):
                continue
            out.append(
                FactCard(
                    claim=data["claim"],
                    source_url=data["source_url"],
                    source_quote=data.get("source_quote", ""),
                    topic=data.get("topic", ""),
                    confidence=data.get("confidence", 0),
                    created_at=data.get("created_at", ""),
                    id=_id,
                )
            )
            if len(out) >= top_k:
                break
        return out

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM fact_cards").fetchone()[0]
