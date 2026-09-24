"""证据池：单次运行内的 chunk 索引与语义检索（规格 §1.2）。"""

import json
import math
from dataclasses import dataclass, field

from insight_agent.tools.chunker import Chunk


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


@dataclass
class EvidenceStats:
    docs: int = 0
    chunks: int = 0
    degraded_docs: int = 0


@dataclass
class EvidencePool:
    """深读产出的 chunk 池，提供按问题的语义检索。"""

    _chunks: list[Chunk] = field(default_factory=list)
    _embedder: object | None = None
    _vectors: list[list[float]] = field(default_factory=list)
    stats: EvidenceStats = field(default_factory=EvidenceStats)

    def bind_embedder(self, embedder) -> None:
        """确定 embedder 后，为已入池的 chunk 补算向量。"""
        self._embedder = embedder
        if self._chunks:
            self._vectors = embedder.embed([c.text for c in self._chunks])

    def add_chunks(self, chunks: list[Chunk]) -> None:
        self._chunks.extend(chunks)
        self.stats.docs += len({c.url for c in chunks})
        self.stats.chunks = len(self._chunks)
        if self._embedder is not None:
            new_vecs = self._embedder.embed([c.text for c in chunks])
            self._vectors.extend(new_vecs)

    def search(self, query: str, *, top_k: int = 4) -> list[Chunk]:
        """语义检索 top-k。无 embedder 时退化为包含匹配。"""
        if not self._chunks:
            return []
        if self._embedder is None:
            hits = [c for c in self._chunks if any(w in c.text for w in query.split())]
            return hits[:top_k] or self._chunks[:top_k]
        qv = self._embedder.embed([query])[0]
        scored = sorted(
            zip(self._chunks, self._vectors),
            key=lambda pair: _cosine(qv, pair[1]),
            reverse=True,
        )
        return [c for c, _ in scored[:top_k]]

    def dump(self) -> str:
        """调试用：池内容摘要。"""
        return json.dumps(
            [{"id": c.chunk_id, "url": c.url, "chars": len(c.text)} for c in self._chunks],
            ensure_ascii=False,
        )
