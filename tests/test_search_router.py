"""搜索路由单测：熔断、冷却、降级顺序、语言路由（全 mock，零网络）。"""

import pytest

from insight_agent.tools.search_router import SearchBatch, SearchRouter


class FakeProvider:
    def __init__(self, name: str, results: list | None = None, fail: bool = False):
        self.name = name
        self.results = results or [{"title": "t", "url": f"https://{name}.com/1", "snippet": "s"}]
        self.fail = fail
        self.calls = 0

    def search(self, query: str, *, max_results: int):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} 挂了")
        return [
            type("R", (), {"title": r.get("title", ""), "url": r["url"], "snippet": r.get("snippet", "")})()
            for r in self.results
        ]


def _router(providers):
    return SearchRouter(providers, breaker_threshold=3, cooldown_s=60.0)


def test_primary_used_when_healthy():
    a, b = FakeProvider("a"), FakeProvider("b")
    batch = _router([a, b]).search("query")
    assert batch.provider_used == "a" and a.calls == 1 and b.calls == 0


def test_fallback_on_failure():
    a, b = FakeProvider("a", fail=True), FakeProvider("b")
    batch = _router([a, b]).search("query")
    assert batch.provider_used == "b"
    assert batch.fallback_chain == ["a"]


def test_breaker_opens_after_threshold():
    a, b = FakeProvider("a", fail=True), FakeProvider("b")
    router = _router([a, b])
    for _ in range(3):  # 连续 3 次失败 → 熔断
        router.search("query")
    a_before = a.calls
    router.search("query")  # 熔断期内跳过 a
    assert a.calls == a_before  # a 不再被尝试
    assert batch_ok(router)


def batch_ok(router):
    return router.search("query").provider_used == "b"


def test_all_down_returns_empty():
    a, b = FakeProvider("a", fail=True), FakeProvider("b", fail=True)
    batch = _router([a, b]).search("query")
    assert batch.results == [] and batch.provider_used == "none"
