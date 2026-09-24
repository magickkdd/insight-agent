"""模块 A 零网络单测：分块 / 证据池检索 / 关键词嵌入器 / 抓取降级。"""

import httpx
import pytest

from insight_agent.tools.chunker import chunk_text
from insight_agent.tools.embedder import KeywordEmbedder
from insight_agent.tools.fetcher import fetch_readable
from insight_agent.memory.evidence_pool import EvidencePool


def _sample_text() -> str:
    sec1 = "".join(f"国内框架市场格局报告第{i}条：一超多强态势持续。" for i in range(30))
    sec2 = "".join(f"混合架构技术路线第{i}条：本地与云端协同优化。" for i in range(40))
    sec3 = "".join(f"风险挑战第{i}条：数据安全与合规是首要挑战。" for i in range(10))
    return f"# 行业格局\n\n{sec1}\n\n## 技术路线\n\n{sec2}\n\n## 风险\n\n{sec3}"


def test_chunker_splits_by_heading_and_caps_size():
    chunks = chunk_text("https://example.com/a", _sample_text())
    assert len(chunks) >= 3  # 三个标题至少切成三块
    assert all(c.chunk_id.startswith("sha") is False for c in chunks)
    assert all(len(c.text) <= 900 for c in chunks)  # size+余量
    assert chunks[0].url == "https://example.com/a"


def test_chunker_short_text_single_chunk():
    chunks = chunk_text("https://example.com/b", "只有一句话。")
    assert len(chunks) == 1


def test_keyword_embedder_deterministic_and_normalized():
    emb = KeywordEmbedder()
    v1 = emb.embed(["人工智能框架"])[0]
    v2 = emb.embed(["人工智能框架"])[0]
    assert v1 == v2
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-6  # 归一化


def test_evidence_pool_ranks_relevant_chunks_first():
    pool = EvidencePool()
    pool.bind_embedder(KeywordEmbedder())
    pool.add_chunks(chunk_text("https://e.com/1", "价格战 比亚迪 降价 销量 均价。" * 10))
    pool.add_chunks(chunk_text("https://e.com/2", "咖啡连锁 闭店 下沉市场 单店模型。" * 10))
    pool.add_chunks(chunk_text("https://e.com/3", "价格战 的新能源汽车 均价 数据。" * 10))
    hits = pool.search("新能源汽车价格战 均价", top_k=2)
    urls = [h.url for h in hits]
    assert "https://e.com/2" not in urls  # 不相关块被排在后面
    assert "https://e.com/1" in urls


def test_fetch_readable_403_degrades_without_raise(monkeypatch):
    def fake_get(*args, **kwargs):
        req = httpx.Request("GET", "https://x.com/a")
        raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))

    monkeypatch.setattr("insight_agent.tools.fetcher.httpx.get", fake_get)
    doc = fetch_readable("https://x.com/a")
    assert doc.status == "failed_403"
    assert doc.text is None


def test_fetch_readable_success(monkeypatch):
    paras = "".join(
        f"<p>第{i}段正文：本段包含不同的数据与事实描述{i * 7}，用于验证抽取。</p>"
        for i in range(40)
    )
    html = f"<html><head><title>T</title></head><body><article>{paras}</article></body></html>"
    monkeypatch.setattr(
        "insight_agent.tools.fetcher.httpx.get",
        lambda *a, **k: httpx.Response(200, text=html, request=httpx.Request("GET", "https://x.com/b")),
    )
    doc = fetch_readable("https://x.com/b")
    assert doc.status == "fetched"
    assert doc.text and len(doc.text) >= 200
