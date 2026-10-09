"""§SCHEDULING_SPEC S2/S8/S10：`cli research` 的退出码语义与定时形态端到端。

**不花钱**：所有用例都用替身 graph，返回值是现成的，不调任何真实端点。
`build_research_graph` 与 `get_llm` 在这里被替换掉，所以这一层测的
只有「台账 + 退出码 + --json 的接线」，不是图的逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from insight_agent.config import Settings
from insight_agent.exits import ExitKind


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_base_url="https://mock.invalid/v1",
        llm_api_key="mock-key",
        llm_model="mock-model",
        tavily_api_key="t",
        log_dir=str(tmp_path / "runs"),
    )


class _FakeGraph:
    """返回固定结果的替身图。`config` 必须收下，否则测不到回调挂载。"""

    def __init__(self, *, error: str | None = None, blow_up: BaseException | None = None) -> None:
        self._error = error
        self._blow_up = blow_up
        self.seen_config: dict[str, Any] | None = None

    def invoke(self, payload: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        self.seen_config = config
        if self._blow_up is not None:
            raise self._blow_up
        return {
            "report": "# 报告\n\n内容",
            "brief": ["子问题一"],
            "verification": {
                "hallucination_rate": 0.0,
                "coverage_rate": 1.0,
                "score": 100.0,
                "skipped": False,
                "error": self._error,
            },
            "redteam": {"verdict": "pass", "action": "archive", "issues": []},
        }


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, graph: _FakeGraph, argv: list[str]):
    """跑一次真实的 CLI 入口，返回 (退出码, stdout, 台账字典)。"""
    monkeypatch.setenv("LLM_BASE_URL", "https://mock.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "mock-key")
    monkeypatch.setenv("LLM_MODEL_ID", "mock-model")
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setenv("RUN_LOG_DIR", str(tmp_path / "runs"))

    # `cli.research` 里的 import 都是**函数内**的（延迟加载），所以要打在
    # 模块上而不是 cli 命名空间上。`build_research_graph` 收 llm 作为参数，
    # `build.py` 自己并不导入 get_llm —— 替身只需顶掉图与工厂两个名字。
    monkeypatch.setattr("insight_agent.config.load_settings", lambda *a, **k: _settings(tmp_path))
    monkeypatch.setattr("insight_agent.graph.llm_factory.get_llm", lambda *a, **k: object())
    monkeypatch.setattr("insight_agent.graph.build.build_research_graph", lambda *a, **k: graph)

    from typer.testing import CliRunner

    from insight_agent import cli

    result = CliRunner().invoke(cli.app, argv)
    ledgers = list((tmp_path / "runs").glob("*.json"))
    data = json.loads(ledgers[0].read_text(encoding="utf-8")) if ledgers else {}
    return result, data


# ----------------------------------------------------------------- S2 七种情形


def test_success_exits_zero_and_writes_a_ledger(tmp_path, monkeypatch):
    """判据 S2 第 1 档 + S1：正常完成 = 0，台账齐全。"""
    result, data = _run(tmp_path, monkeypatch, _FakeGraph(), ["research", "研究主题", "--json"])
    assert result.exit_code == 0, result.output
    assert data["termination"] == "completed"
    assert data["exit_code"] == 0
    assert data["started_at"] and data["ended_at"]


def test_usage_error_keeps_exit_two(tmp_path, monkeypatch):
    """判据 S2：非法 depth 仍然是 2 —— 脚本里已有的用法错判断不能破。"""
    result, _ = _run(tmp_path, monkeypatch, _FakeGraph(), ["research", "研究主题", "--depth", "turbo"])
    assert result.exit_code == int(ExitKind.USAGE_ERROR) == 2


def test_verification_error_is_its_own_code(tmp_path, monkeypatch):
    """判据 S2：验证层异常是 1，与限流（3）分开 —— 这是这次改动的核心目的。"""
    graph = _FakeGraph(error="核验层炸了")
    result, data = _run(tmp_path, monkeypatch, graph, ["research", "研究主题"])
    assert result.exit_code == int(ExitKind.VERIFY_FAILED) == 1
    assert data["termination"] == "verify_failed"


def test_a_rate_limit_is_retryable_and_says_so(tmp_path, monkeypatch):
    """判据 S2：限流 = 3，调用方据此退避重试；台账里能看出是哪一类。"""
    from openai import RateLimitError
    import httpx

    request = httpx.Request("POST", "https://mock.invalid/v1/chat/completions")
    response = httpx.Response(429, request=request)
    graph = _FakeGraph(blow_up=RateLimitError("额度用尽", response=response, body=None))

    result, data = _run(tmp_path, monkeypatch, graph, ["research", "研究主题"])
    assert result.exit_code == int(ExitKind.RATE_LIMITED) == 3
    assert data["termination"] == "rate_limited"
    assert data["error_kind"] == "RateLimitError"


def test_an_unreachable_endpoint_is_a_different_code(tmp_path, monkeypatch):
    """判据 S2：上游不可用 = 4，与限流分开（额度窗口 vs 服务端挂了）。"""
    import httpx
    from openai import APIConnectionError

    graph = _FakeGraph(blow_up=APIConnectionError(request=httpx.Request("POST", "https://mock.invalid/v1/x")))
    result, data = _run(tmp_path, monkeypatch, graph, ["research", "研究主题"])
    assert result.exit_code == int(ExitKind.UPSTREAM_UNAVAILABLE) == 4
    assert data["termination"] == "upstream_unavailable"


def test_our_own_bug_is_five_and_not_retryable(tmp_path, monkeypatch):
    """判据 S2：内部异常 = 5，且不重试 —— 重试一个必然失败的调用是浪费额度。"""
    graph = _FakeGraph(blow_up=ValueError("我自己的 bug"))
    result, data = _run(tmp_path, monkeypatch, graph, ["research", "研究主题"])
    assert result.exit_code == int(ExitKind.INTERNAL_ERROR) == 5
    assert data["termination"] == "internal_error"


def test_a_crash_still_leaves_a_log(tmp_path, monkeypatch):
    """判据 S6：失败那次也要有 log，否则定时跑挂了只剩终端回滚。"""
    graph = _FakeGraph(blow_up=ValueError("炸"))
    _run(tmp_path, monkeypatch, graph, ["research", "研究主题"])
    logs = list((tmp_path / "runs").glob("*.log"))
    assert len(logs) == 1
    assert "炸" in logs[0].read_text(encoding="utf-8")


# ----------------------------------------------------------------- S8 --json 可被脚本消费


def test_json_output_is_script_consumable(tmp_path, monkeypatch):
    """判据 S8：定时脚本靠 stdout 的 JSON 取 run_id，不该去解析人看的报告。"""
    import json as _json

    result, _ = _run(tmp_path, monkeypatch, _FakeGraph(), ["research", "研究主题", "--json"])
    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    for key in ("run_id", "termination", "tokens", "elapsed_s", "artifacts"):
        assert key in payload, f"--json 少了 {key}"
    assert payload["termination"] == "completed"
    assert payload["run_id"]


def test_the_callback_really_reaches_the_graph(tmp_path, monkeypatch):
    """计量回调必须挂进 invoke 的 config。

    挂成 `llm.bind(callbacks=...)` 的话，langchain-core 1.x 会在
    `with_structured_output` 的代理层把回调整个吞掉、token 静默计成 0 ——
    那是这个 Stage 要修的同一个坑，不能在接线时重新踩一遍。
    """
    graph = _FakeGraph()
    _run(tmp_path, monkeypatch, graph, ["research", "研究主题"])
    assert graph.seen_config is not None, "config 没传进 invoke"
    assert graph.seen_config.get("callbacks"), "计量回调没挂上"


# ----------------------------------------------------------------- S10 定时形态


def test_two_runs_of_the_same_topic_keep_two_ledgers(tmp_path, monkeypatch):
    """判据 S9/S10：定时连跑两次要两份台账，后一次不能盖掉前一次。"""
    _run(tmp_path, monkeypatch, _FakeGraph(), ["research", "同一个主题", "--json"])
    _run(tmp_path, monkeypatch, _FakeGraph(), ["research", "同一个主题", "--json"])
    ledgers = sorted((tmp_path / "runs").glob("*.json"))
    assert len(ledgers) == 2
    assert json.loads(ledgers[0].read_text(encoding="utf-8"))["run_id"] != json.loads(
        ledgers[1].read_text(encoding="utf-8")
    )["run_id"]


def test_ledger_and_log_share_the_run_id(tmp_path, monkeypatch):
    """判据 S1：一次运行一对文件，文件名是同一个 run_id。"""
    _run(tmp_path, monkeypatch, _FakeGraph(), ["research", "研究主题"])
    ledgers = list((tmp_path / "runs").glob("*.json"))
    logs = list((tmp_path / "runs").glob("*.log"))
    assert len(ledgers) == len(logs) == 1
    assert ledgers[0].stem == logs[0].stem


def test_no_secret_reaches_the_scheduled_artifacts(tmp_path, monkeypatch):
    """判据 S7：定时跑出来的文件里不该有 key —— 它会一直躺在磁盘上。"""
    graph = _FakeGraph(blow_up=ValueError("失败 sk-abcdefghijklmnop"))
    _run(tmp_path, monkeypatch, graph, ["research", "研究主题"])
    blob = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "runs").iterdir())
    assert "sk-abcdefghijklmnop" not in blob