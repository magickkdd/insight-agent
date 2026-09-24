"""嵌入器三档降级（规格 §1.5）：API → 本地 fastembed → 关键词哈希。

启动时 get_embedder() 探活一次并固定，运行中不切换（保证同轮向量空间一致）。
"""

import hashlib
import math
from typing import Protocol

_DIM = 512  # 关键词档的哈希向量维度


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class ApiEmbedder:
    """OpenAI 兼容 /v1/embeddings 端点。"""

    name = "api"

    def __init__(self, base_url: str, api_key: str, model: str):
        from openai import OpenAI

        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self._model, input=texts)
        return [d.embedding for d in resp.data]


class KeywordEmbedder:
    """最终兜底：哈希技巧词频向量（CJK 二元组 + 拉丁词），确定性、零依赖。"""

    name = "keyword"

    def _terms(self, text: str) -> list[str]:
        terms: list[str] = []
        cur_latin: list[str] = []
        cur_cjk: list[str] = []
        for ch in text.lower():
            if ch.isascii() and ch.isalnum():
                cur_latin.append(ch)
                if cur_cjk:
                    terms.extend("".join(cur_cjk[i : i + 2]) for i in range(len(cur_cjk) - 1))
                    cur_cjk = []
            elif "\u4e00" <= ch <= "\u9fff":
                cur_cjk.append(ch)
                if cur_latin:
                    word = "".join(cur_latin)
                    if len(word) >= 2:
                        terms.append(word)
                    cur_latin = []
            else:
                if cur_latin:
                    word = "".join(cur_latin)
                    if len(word) >= 2:
                        terms.append(word)
                    cur_latin = []
                if cur_cjk:
                    terms.extend("".join(cur_cjk[i : i + 2]) for i in range(len(cur_cjk) - 1))
                    cur_cjk = []
        if cur_latin and len("".join(cur_latin)) >= 2:
            terms.append("".join(cur_latin))
        if cur_cjk:
            terms.extend("".join(cur_cjk[i : i + 2]) for i in range(len(cur_cjk) - 1))
        return terms

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs: list[list[float]] = []
        for text in texts:
            vec = [0.0] * _DIM
            for term in self._terms(text):
                h = int(hashlib.md5(term.encode()).hexdigest(), 16)
                vec[h % _DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vecs.append([v / norm for v in vec])
        return vecs


def _try_api(settings) -> Embedder | None:
    try:
        emb = ApiEmbedder(settings.llm_base_url, settings.llm_api_key, settings.embedding_model)
        emb.embed(["探活"])  # 启动探活
        return emb
    except Exception:  # noqa: BLE001
        return None


def _try_local() -> Embedder | None:
    try:
        from fastembed import TextEmbedding  # 可选依赖

        model = TextEmbedding("BAAI/bge-small-zh-v1.5")

        class _Local:
            name = "local"

            def embed(self, texts: list[str]) -> list[list[float]]:
                return [list(v) for v in model.embed(texts)]

        _Local.embed(["探活"])
        return _Local()
    except Exception:  # noqa: BLE001 - 未安装 / 模型下载失败
        return None


def get_embedder(settings) -> Embedder:
    """按配置探活选择：api → local → keyword。"""
    pref = settings.embedding_provider
    if pref in ("api", "auto"):
        if emb := _try_api(settings):
            return emb
    if pref in ("local", "auto"):
        if emb := _try_local():
            return emb
    return KeywordEmbedder()


_cached_embedder: Embedder | None = None


def get_embedder_cached(settings) -> Embedder:
    """进程级单例：探活一次，运行中不切换（保证同轮向量空间一致）。"""
    global _cached_embedder
    if _cached_embedder is None:
        _cached_embedder = get_embedder(settings)
    return _cached_embedder
