"""多源搜索动态路由（UPGRADE_SPEC §4）：链式降级 + 熔断冷却。

SearchProvider 协议 → 三家实现 → SearchRouter：
  失败（异常/空结果）→ 熔断计数 +1 → 下一家；
  连续 breaker_threshold 次失败 → 该源冷却 cooldown_s 秒内跳过；
  语言路由：CJK query 且博查可用 → 置顶（国内直连稳定）。
"""

import re
import time
from dataclasses import dataclass, field
from typing import Protocol

_CJK = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass
class SearchBatch:
    results: list[SearchResult]
    provider_used: str
    fallback_chain: list[str] = field(default_factory=list)  # 尝试过且失败的源


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, max_results: int) -> list[SearchResult]: ...


class TavilyProvider:
    name = "tavily"

    def __init__(self, api_key: str):
        from langchain_tavily import TavilySearch

        self._tool = TavilySearch(max_results=8, tavily_api_key=api_key)

    def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        raw = self._tool.invoke({"query": query})
        items = raw.get("results", []) if isinstance(raw, dict) else []
        return [
            SearchResult(r.get("title", ""), r.get("url", ""), r.get("content", ""))
            for r in items
            if r.get("url")
        ][:max_results]


class DuckDuckGoProvider:
    name = "duckduckgo"

    def __init__(self):
        from ddgs import DDGS  # ddgs 包（原 duckduckgo_search 更名）

        self._ddgs = DDGS()

    def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        rows = self._ddgs.text(query, max_results=max_results) or []
        return [SearchResult(r.get("title", ""), r.get("href", ""), r.get("body", "")) for r in rows if r.get("href")]


class BochaProvider:
    name = "bocha"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        import httpx

        resp = httpx.post(
            "https://api.bochaai.com/v1/web-search",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"query": query, "count": max_results},
            timeout=15.0,
        )
        resp.raise_for_status()
        pages = resp.json().get("data", {}).get("webPages", {}).get("value", [])
        return [SearchResult(p.get("name", ""), p.get("url", ""), p.get("snippet", "")) for p in pages][:max_results]


@dataclass
class _Breaker:
    fails: int = 0
    open_until: float = 0.0


class SearchRouter:
    def __init__(
        self,
        providers: list[SearchProvider],
        *,
        breaker_threshold: int = 3,
        cooldown_s: float = 60.0,
    ):
        self._providers = providers
        self._threshold = breaker_threshold
        self._cooldown = cooldown_s
        self._breakers: dict[str, _Breaker] = {p.name: _Breaker() for p in providers}

    def _usable(self, name: str) -> bool:
        b = self._breakers[name]
        return b.open_until <= time.monotonic()

    def search(self, query: str, *, max_results: int = 8) -> SearchBatch:
        # 语言路由：CJK query 且博查在列 → 置顶
        order = list(self._providers)
        if _CJK.search(query):
            order.sort(key=lambda p: p.name != "bocha")
        chain: list[str] = []
        for provider in order:
            if not self._usable(provider.name):
                continue
            try:
                results = provider.search(query, max_results=max_results)
                if results:
                    self._breakers[provider.name].fails = 0
                    return SearchBatch(results, provider.name, chain)
                raise RuntimeError("空结果")
            except Exception:  # noqa: BLE001 - 失败降级下一家
                b = self._breakers[provider.name]
                b.fails += 1
                chain.append(provider.name)
                if b.fails >= self._threshold:
                    b.open_until = time.monotonic() + self._cooldown
                    b.fails = 0
        return SearchBatch([], "none", chain)
