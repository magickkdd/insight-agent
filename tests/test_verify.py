"""模块 B 零 LLM 单测：幻觉率计算 / 标注 / 提取失败降级。"""

from insight_agent.graph.verify import Claim, VerificationReport, _annotate


def _vr(claims: list[tuple[str, str]]) -> VerificationReport:
    return VerificationReport(claims=[Claim(t, v, [], "") for t, v in claims])


def test_hallucination_rate_and_score():
    vr = _vr([("a", "supported"), ("b", "supported"), ("c", "unsupported"), ("d", "partial")])
    assert vr.hallucination_rate == 0.25  # 1/4
    assert vr.coverage_rate == 0.0
    assert vr.score() == 75.0


def test_all_not_checkable_means_no_score():
    vr = _vr([("a", "not_checkable"), ("b", "not_checkable")])
    assert vr.hallucination_rate is None
    assert vr.coverage_rate == 1.0


def test_annotate_marks_only_flagged_claims():
    report = "第一句。第二句。第三句。"
    claims = [
        Claim("第一句。", "supported"),
        Claim("第二句。", "unsupported"),
        Claim("第三句。", "partial"),
    ]
    out = _annotate(report, claims)
    assert "第一句。" in out and "⚠️" not in out.split("第二句")[0]
    assert "第二句。 ⚠️[未找到证据支撑]" in out
    assert "第三句。 ⚠️[部分支撑]" in out


def test_skipped_verification():
    vr = VerificationReport(skipped=True)
    d = vr.to_dict()
    assert d["skipped"] is True and d["hallucination_rate"] is None
