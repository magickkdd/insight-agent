"""评测逻辑零成本单测：schema 校验 + 回归 diff（不调 LLM）。"""

import pytest

from insight_agent.eval.dataset import load_cases
from insight_agent.eval.regression import compare


def test_load_official_dataset():
    cases = load_cases("eval/cases.yaml")
    assert len(cases) >= 30
    tiers = {c.tier for c in cases}
    assert tiers == {"unit", "e2e"}


def test_rejects_typo_field(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("- id: a\n  topic: t\n  tier: unit\n  node: planner\n  expekt: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未知字段"):
        load_cases(p)


def test_rejects_unit_without_node(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("- id: a\n  topic: t\n  tier: unit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="node"):
        load_cases(p)


def test_regression_detects_pass_to_fail():
    prev = [
        {"case_id": "a", "passed": True, "error_kind": None, "tokens": {"total_tokens": 100}, "latency_s": 10},
        {"case_id": "b", "passed": False, "error_kind": None, "tokens": {}, "latency_s": 1},
    ]
    cur = [
        type("R", (), {"case_id": "a", "passed": False, "error_kind": None,
                       "tokens": {"total_tokens": 100}, "latency_s": 10,
                       "checks": [{"name": "x", "passed": False, "detail": "缺章节"}]})(),
        type("R", (), {"case_id": "b", "passed": True, "error_kind": None,
                       "tokens": {"total_tokens": 100}, "latency_s": 1,
                       "checks": []})(),
    ]
    result = compare(cur, prev)
    assert len(result["regressions"]) == 1
    assert result["regressions"][0]["case_id"] == "a"
    assert len(result["fixed"]) == 1 and result["fixed"][0]["case_id"] == "b"


def test_regression_ignores_errors():
    prev = [{"case_id": "a", "passed": True, "error_kind": None, "tokens": {}, "latency_s": 5}]
    cur = [type("R", (), {"case_id": "a", "passed": False, "error_kind": "rate_limit", "checks": []})()]
    result = compare(cur, prev)
    assert result["regressions"] == []  # 限流不算回归
