"""回归对比：拿本次结果和上一次同层报告逐条 diff。

判定口径：
  PASS→FAIL        回归（红线）
  FAIL→PASS        修复（喜报）
  PASS→PASS        对比 token / 时延，涨幅超阈值记警告
ERROR 不参与对比（环境的锅不算回归）。
"""
import json
from dataclasses import asdict
from pathlib import Path

TOKEN_DELTA_WARN = 0.30  # token 涨 30% 记警告
LATENCY_DELTA_WARN = 0.30


def compare(current: list, previous: list[dict]) -> dict:
    prev = {r["case_id"]: r for r in previous if r.get("error_kind") is None}
    regressions, fixed, warns = [], [], []

    cur_ok = [r for r in current if r.error_kind is None]
    for r in cur_ok:
        p = prev.get(r.case_id)
        if p is None:
            continue
        was_pass = p["passed"]
        now_pass = r.passed
        if was_pass and not now_pass:
            fails = [c["detail"] for c in r.checks if not c["passed"]]
            regressions.append({"case_id": r.case_id, "detail": "；".join(fails) or "判分失败"})
        elif not was_pass and now_pass:
            fixed.append({"case_id": r.case_id})

        pt = sum(p.get("tokens", {}).values()) or None
        nt = sum(r.tokens.values()) or None
        if was_pass and now_pass and pt and nt:
            delta = (nt - pt) / pt
            if delta > TOKEN_DELTA_WARN:
                warns.append({"case_id": r.case_id, "metric": "tokens", "delta_pct": round(delta * 100)})
        if was_pass and now_pass and p.get("latency_s") and r.latency_s:
            delta = (r.latency_s - p["latency_s"]) / p["latency_s"]
            if delta > LATENCY_DELTA_WARN:
                warns.append({"case_id": r.case_id, "metric": "latency", "delta_pct": round(delta * 100)})

    return {"regressions": regressions, "fixed": fixed, "warnings": warns}


def previous_report(runs_dir: Path, tier: str | None) -> dict | None:
    """取目录里最近的报告 JSON（排除本次将写入的名字）。"""
    if not runs_dir.exists():
        return None
    reports = sorted(runs_dir.glob("report_*.json"))
    if not reports:
        return None
    data = json.loads(reports[-1].read_text(encoding="utf-8"))
    if tier:
        data = [r for r in data if r.get("tier") == tier]
    return data or None
