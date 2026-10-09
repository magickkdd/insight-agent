"""§SCHEDULING_SPEC 判据：定时调度要让"哪一次、哪个原因、烧了多少"事后可查。

这些用例全部离线，不花钱也不联网 —— `RunLedger` 的 IO 只碰 `tmp_path`，
`classify_exception` 只造异常不真调 API。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from insight_agent.exits import ExitKind, classify_exception, should_retry
from insight_agent.runs import (
    SCHEMA_VERSION,
    RunLedger,
    RunRecord,
    new_run_id,
    redact,
)


# ----------------------------------------------------------------- S2 退出码语义


def test_zero_and_two_keep_their_meaning():
    """`0` 是成功、`2` 是用法错是 POSIX 惯例，脚本里已有的判断不能被破坏。"""
    assert int(ExitKind.COMPLETED) == 0
    assert int(ExitKind.USAGE_ERROR) == 2


def test_only_external_failures_are_retryable():
    """重试的是"外部依赖坏了"，不是"自己写错了"。

    限流与上游不可用退避后有机会；参数错、内部 bug、验证层异常重试一万次
    结果一样，把它们算进 RETRYABLE 会让定时任务陷入死循环。
    """
    assert should_retry(ExitKind.RATE_LIMITED)
    assert should_retry(ExitKind.UPSTREAM_UNAVAILABLE)
    for kind in (
        ExitKind.COMPLETED,
        ExitKind.USAGE_ERROR,
        ExitKind.INTERNAL_ERROR,
        ExitKind.VERIFY_FAILED,
    ):
        assert not should_retry(kind), f"{kind.name} 不该重试"


def test_rate_limit_and_5xx_map_to_distinct_codes():
    """限流（3）与上游不可用（4）要分开：前者额度窗口，后者可能是服务端挂了。"""
    import httpx
    from openai import APIConnectionError, APIStatusError, RateLimitError

    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(429, request=request)
    assert classify_exception(RateLimitError("slow down", response=response, body=None)) is ExitKind.RATE_LIMITED

    response_503 = httpx.Response(503, request=request)
    assert (
        classify_exception(APIStatusError("boom", response=response_503, body=None))
        is ExitKind.UPSTREAM_UNAVAILABLE
    )
    assert (
        classify_exception(APIConnectionError(request=request))
        is ExitKind.UPSTREAM_UNAVAILABLE
    )


def test_a_real_502_through_langchain_is_upstream_unavailable():
    """5xx 要走**真实链路**验，不能只喂构造的 `APIStatusError`。

    langchain 会把 SDK 的异常包一层：`OpenAIAPIError` 继承自
    `InternalServerError`（langchain_openai/chat_models/base.py），而后者是
    `APIStatusError` 的子类 —— 包装过一层之后 `isinstance` 仍认得到、
    `status_code` 也还在。曾经怀疑这条链会断（写进 SPEC §11 又被实测推翻），
    留这个用例是为了让它以后真断时立刻响。

    用 `httpx.MockTransport` 让 SDK 收到真的 502 空 body，不联网、不花钱。
    """
    import httpx
    from langchain_openai import ChatOpenAI

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="")

    llm = ChatOpenAI(
        model="m",
        api_key="k",
        base_url="https://mock.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(BaseException) as caught:
        llm.invoke("hi")
    exc = caught.value

    # 前提断言：确认异常确实是"包装过的 5xx"，而不是别的东西
    assert "APIStatusError" in [c.__name__ for c in type(exc).__mro__]
    assert getattr(exc, "status_code", None) == 502

    assert classify_exception(exc) is ExitKind.UPSTREAM_UNAVAILABLE
    assert should_retry(classify_exception(exc)), "5xx 是外部依赖问题，该退避重试"


def test_an_unknown_failure_is_ours_not_a_rate_limit():
    """判不出来就归内部异常 —— 猜成限流会让脚本无限重试一个永远失败的任务。"""
    assert classify_exception(ValueError("something odd")) is ExitKind.INTERNAL_ERROR
    assert classify_exception(KeyboardInterrupt()) is ExitKind.INTERRUPTED


# ----------------------------------------------------------------- S3 台账字段都有写入点


def test_every_record_field_has_a_writer():
    """台账里的每个键都要有写入点，否则它不是"没发生"而是"没人记"。

    同一个套路来自 mini-claude-code 的 `test_metrics_have_producers`。
    嵌套 dict（`verification` / `redteam` / `artifacts`）的键算写入时，
    要看整段赋值语句而不是 `self.record.<key>` —— 那些键是随整块赋进去的。
    """
    source = Path(__file__).resolve().parents[1] / "src" / "insight_agent" / "runs.py"
    text = source.read_text(encoding="utf-8")
    body = text[text.index("def to_dict") :]
    keys = re.findall(r'"(\w+)":', body)
    assert keys, "to_dict 什么键都没有？"

    ledger_src = text[text.index("class RunLedger") :]
    dataclass_src = text[text.index("class RunRecord") : text.index("class RunLedger")]
    # 有默认值的键不需要额外写入点：默认本身就是"有值"
    defaults_ok = {"schema_version", "cost_note", "cost_usd", "error_kind",
                   "error_message", "elapsed_s", "ended_at"}
    missing = [
        key
        for key in keys
        if key not in defaults_ok
        and f'self.record.{key}' not in ledger_src       # 顶层字段
        and f"{key}:" not in dataclass_src              # dataclass 声明
        and f'"{key}":' not in ledger_src               # 嵌套 dict 的字面量键
    ]
    assert not missing, f"这些键没有任何写入点：{missing}"


# ----------------------------------------------------------------- S1 台账的账加得起来


def _run_once(tmp_path: Path, **kwargs) -> tuple[RunLedger, dict]:
    ledger = RunLedger(topic="httpx 超时配置最佳实践", depth="standard", log_dir=tmp_path, **kwargs)
    ledger.log("graph.invoke 开始")
    ledger.set_tokens({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    ledger.set_verification(
        {"hallucination_rate": 0.25, "coverage_rate": 0.5, "score": 75.0, "skipped": False, "claims": [{"text": "x"}]}
    )
    ledger.set_redteam({"verdict": "pass", "action": "archive", "issues": []})
    ledger.set_artifacts(report="data/reports/x.md", annotated=None, trace=None)
    ledger.finish(ExitKind.COMPLETED)
    return ledger, json.loads(ledger.ledger_path.read_text(encoding="utf-8"))


def test_a_finished_run_leaves_a_readable_ledger(tmp_path: Path):
    """S1：一次运行 = 一个 json + 一个 log，两个文件名都带同一个 run_id。"""
    ledger, data = _run_once(tmp_path)

    assert data["run_id"] == ledger.record.run_id
    assert data["schema_version"] == SCHEMA_VERSION
    for key in ("started_at", "ended_at", "elapsed_s", "termination", "exit_code"):
        assert key in data, f"台账少了 {key}"
    assert data["termination"] == "completed"
    assert data["exit_code"] == 0

    assert ledger.log_path.exists(), "日志没落盘 —— 定时跑失败时只剩终端回滚"
    assert ledger.log_path.stem == ledger.record.run_id


def test_claims_body_stays_out_of_the_ledger(tmp_path: Path):
    """claims 是逐条论断原文，台账是给脚本读的 —— 正文不该进台账。"""
    _, data = _run_once(tmp_path)
    assert "claims" not in data["verification"]
    assert data["verification"]["hallucination_rate"] == 0.25
    assert data["verification"]["score"] == 75.0


# ----------------------------------------------------------------- S6 异常路径也要写台账


def test_a_crash_still_writes_a_complete_ledger(tmp_path: Path):
    """定时任务最需要的信息恰恰出现在失败时，只在成功时记等于失败那次没记录。"""
    ledger = RunLedger(topic="会崩的主题", depth="fast", log_dir=tmp_path)
    with pytest.raises(RuntimeError, match="graph 挂了"):
        try:
            raise RuntimeError("graph 挂了")
        except RuntimeError as exc:
            ledger.finish(ExitKind.INTERNAL_ERROR, error=exc)
            raise

    data = json.loads(ledger.ledger_path.read_text(encoding="utf-8"))
    assert data["termination"] == "internal_error"
    assert data["exit_code"] == 5
    assert data["ended_at"], "异常路径也得有结束时间，否则不知道跑了多久"
    assert data["error_kind"] == "RuntimeError"
    assert "graph 挂了" in data["error_message"]


def test_finish_is_idempotent(tmp_path: Path):
    """重复收口只生效一次 —— 忘了幂等的话日志会被第二次收口覆盖。"""
    ledger = RunLedger(topic="t", depth="fast", log_dir=tmp_path)
    ledger.finish(ExitKind.COMPLETED)
    first = ledger.ledger_path.read_text(encoding="utf-8")
    ledger.finish(ExitKind.INTERNAL_ERROR)
    assert ledger.ledger_path.read_text(encoding="utf-8") == first


# ----------------------------------------------------------------- S4/S5 成本


def test_tokens_come_from_the_same_shape_the_evaluator_uses(tmp_path: Path):
    """口径与 `eval.metrics.summarize_tokens` 同源，判据 S4。

    用真实 handler 喂两条 usage，跨模型合并后的 total 必须是两条之和 ——
    台账若自己另算一套，评测与真实运行的成本就会对不上。
    """
    from insight_agent.eval.metrics import summarize_tokens
    from langchain_core.callbacks import UsageMetadataCallbackHandler

    handler = UsageMetadataCallbackHandler()
    handler.usage_metadata = {
        "m1": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        "m2": {"input_tokens": 30, "output_tokens": 5, "total_tokens": 35},
    }
    expected = summarize_tokens(handler)
    assert expected["total_tokens"] == 155, "前置条件：合并口径本身"

    ledger = RunLedger(topic="t", depth="fast", log_dir=tmp_path)
    ledger.set_tokens(expected)
    ledger.finish(ExitKind.COMPLETED)
    data = json.loads(ledger.ledger_path.read_text(encoding="utf-8"))
    assert data["tokens"] == expected
    assert data["tokens"]["total_tokens"] == 155


def test_no_price_means_no_money(tmp_path: Path):
    """没配单价就不出现金额 —— 写死单价会产出"看着精确、实际没人敢用"的成本数字。"""
    _, data = _run_once(tmp_path, price_per_mtok=0.0)
    assert data["cost_usd"] is None
    assert "未配置" in data["cost_note"]


def test_a_configured_price_produces_a_number(tmp_path: Path):
    _, data = _run_once(tmp_path, price_per_mtok=2.0)
    # 台账里 total_tokens=120（_run_once 写的），120/1e6 * 2.0
    assert data["cost_usd"] == pytest.approx(0.00024)
    assert data["cost_note"] == ""


# ----------------------------------------------------------------- S7 脱敏


def test_secrets_never_reach_the_ledger_or_the_log(tmp_path: Path):
    """`.env` 里的 key 不该出现在任何定时跑出来的文件里。"""
    ledger = RunLedger(topic="泄漏测试", depth="fast", log_dir=tmp_path)
    ledger.log("调用端点用 key=sk-abcdefghijklmnop 完成了")
    ledger.log("Authorization: Bearer sk-abcdefghijklmnop")
    ledger.finish(ExitKind.INTERNAL_ERROR, error=RuntimeError("失败原因 sk-abcdefghijklmnop"))
    blob = ledger.ledger_path.read_text(encoding="utf-8") + ledger.log_path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in blob
    assert "***" in blob


@pytest.mark.parametrize(
    "raw",
    [
        "sk-abcdefghijklmnop",
        "pk-live-1234567890ab",
        "TAVILY_API_KEY=tvly-abcdefghijkl",
        "authorization: Bearer xxxxxxxxxxxx",
    ],
)
def test_redact_covers_the_shapes_we_actually_hold(raw: str):
    assert "abcdefghijklmnop" not in redact(raw)
    assert "tvly-abcdefghijkl" not in redact(raw)
    assert "xxxxxxxxxxxx" not in redact(raw)


# ----------------------------------------------------------------- S9 run_id 不撞


def test_run_id_never_collides(tmp_path: Path):
    """同一主题连跑两次要两个台账，否则后一次会盖掉前一次的记录。"""
    first = new_run_id("同一主题")
    second = new_run_id("同一主题")
    assert first != second

    ledgers = [RunLedger(topic="同一主题", depth="fast", log_dir=tmp_path) for _ in range(2)]
    for ledger in ledgers:
        ledger.finish(ExitKind.COMPLETED)
    assert len({ledger.record.run_id for ledger in ledgers}) == 2
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_run_id_is_readable_by_a_human(tmp_path: Path):
    """这个文件名是要被人 ls 出来看的：得能看出哪天跑的哪的主题。

    slug 截到 8 个字符 —— 截断是刻意的（主题可能很长），但要留够认得出：
    `rate limit retry` → `rate-lim`。
    """
    run_id = new_run_id("rate limit retry")
    assert run_id.startswith("20")           # 年份
    assert "rate-lim" in run_id
    assert re.fullmatch(r"20\d{6}T\d{6}-[\w-]+-[0-9a-f]{6}", run_id)


def test_a_chinese_topic_still_yields_a_usable_run_id():
    """主题是中文时不能把转义后的中文塞进文件名（Windows 上脆且丑）。"""
    run_id = new_run_id("低空经济的政策进展")
    assert re.fullmatch(r"20\d{6}T\d{6}-topic[0-9a-f]{5}-[0-9a-f]{6}", run_id)


def test_a_crash_before_measurement_says_so(tmp_path: Path):
    """崩在 invoke 里就够不到计量 —— 台账要能让人分清"没烧钱"和"没走到计量那步"。"""
    ledger = RunLedger(topic="t", depth="fast", log_dir=tmp_path)
    ledger.finish(ExitKind.UPSTREAM_UNAVAILABLE, error=RuntimeError("502"))
    data = json.loads(ledger.ledger_path.read_text(encoding="utf-8"))
    assert data["cost_usd"] is None
    assert data["cost_note"], "cost_note 空着 = 读的人分不清是哪种情况"


def test_a_default_record_has_the_documented_defaults():
    """默认值要与 SPEC §4.1 一致，不能悄悄变。"""
    record = RunRecord(run_id="r", topic="t", depth="fast")
    data = record.to_dict()
    assert data["termination"] == "completed"
    assert data["exit_code"] == 0
    assert data["cost_usd"] is None
    assert data["tokens"] == {}
    assert data["error_kind"] is None