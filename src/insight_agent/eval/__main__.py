"""评测 CLI。

用法：
  uv run python -m insight_agent.eval                    # 全部
  uv run python -m insight_agent.eval --tier unit        # 快速回归（1-2 次 LLM/条）
  uv run python -m insight_agent.eval --tier e2e         # 全流水线（分钟级/条，额度大）
  uv run python -m insight_agent.eval --limit 4 --ids a,b

退出码：存在 FAIL → 1（可接 CI 门禁）；ERROR（限流等）→ 0。
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from langchain_openai import ChatOpenAI
from openai import OpenAI

from insight_agent.config import load_settings
from insight_agent.eval.dataset import load_cases
from insight_agent.eval.regression import compare, previous_report
from insight_agent.eval.runner import run_case


def main() -> None:
    parser = argparse.ArgumentParser(description="Insight Agent 评测基准")
    parser.add_argument("--cases", default="eval/cases.yaml")
    parser.add_argument("--tier", choices=["unit", "e2e"], default=None, help="只跑某一层")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--ids", default=None)
    args = parser.parse_args()

    s = load_settings()
    from insight_agent.graph.llm_factory import get_judge_config

    j_base, j_key, j_model = get_judge_config(s)
    judge_client = OpenAI(base_url=j_base, api_key=j_key)

    cases = load_cases(args.cases)
    if args.tier:
        cases = [c for c in cases if c.tier == args.tier]
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c.id in wanted]
    if args.limit:
        cases = cases[: args.limit]

    runs_dir = Path("eval/runs")
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    store_root = runs_dir / f"store_{stamp}"

    print(f"=== 评测开始：{len(cases)} 条（{'/'.join(sorted({c.tier for c in cases}))}）===")
    results = []
    for case in cases:
        print(f"[{case.id}] ", end="", flush=True)
        r = run_case(case, s, judge_client, j_model, store_root)
        results.append(r)
        if r.error_kind == "rate_limit":
            print("ERROR 限流，剩余中止")
            break
        status = "PASS" if r.passed else "FAIL"
        print(f"{status}  tokens={sum(r.tokens.values())}  {r.latency_s:.1f}s")

    # 回归对比（与最近一次报告）
    prev = previous_report(runs_dir, args.tier)
    regression = compare(results, prev or [])

    # 存 JSON
    json_path = runs_dir / f"report_{stamp}.json"
    json_path.write_text(json.dumps([asdict_rec(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8")

    # 存 Markdown
    md = render_markdown(results, regression, stamp)
    md_path = runs_dir / f"report_{stamp}.md"
    md_path.write_text(md, encoding="utf-8")

    # 控制台摘要
    ok = sum(r.passed for r in results)
    fails = sum(not r.passed and r.error_kind is None for r in results)
    errs = sum(r.error_kind is not None for r in results)
    print(f"\n=== 总报告：PASS {ok} / FAIL {fails} / ERROR {errs} ===")
    print(f"回归：{len(regression['regressions'])} 项退化 / {len(regression['fixed'])} 项修复 / {len(regression['warnings'])} 项成本警告")
    print(f"报告：{md_path}")
    for r in results:
        if not r.passed and r.error_kind is None:
            for c in r.checks:
                if not c["passed"]:
                    print(f"  ✗ {r.case_id} [{c['name']}] {c['detail']}")

    has_fail = fails > 0
    sys.exit(1 if has_fail else 0)


def asdict_rec(r):
    from dataclasses import asdict

    return asdict(r)


def render_markdown(results, regression: dict, stamp: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ok = sum(r.passed for r in results)
    total_tokens = sum(sum(r.tokens.values()) for r in results)
    avg_lat = sum(r.latency_s for r in results) / len(results) if results else 0

    lines = [
        f"# 评测报告 {stamp}",
        f"",
        f"- 生成时间：{now}",
        f"- 通过率：{ok}/{len(results)}",
        f"- 总 Token：{total_tokens}，平均时延：{avg_lat:.1f}s",
        f"- 回归对比：**{len(regression['regressions'])} 项退化** / {len(regression['fixed'])} 项修复 / {len(regression['warnings'])} 项成本警告",
        "",
    ]
    if regression["regressions"]:
        lines.append("## ⚠ 回归明细")
        lines += [f"- `{r['case_id']}`：{r['detail']}" for r in regression["regressions"]]
        lines.append("")
    lines += [
        "| 用例 | 层 | 状态 | Tokens | 时延(s) | 幻觉率 |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        status = "ERROR" if r.error_kind else ("PASS" if r.passed else "FAIL")
        hr = "—" if r.hallucination_rate is None else f"{r.hallucination_rate:.0%}"
        lines.append(f"| {r.case_id} | {r.tier} | {status} | {sum(r.tokens.values())} | {r.latency_s:.1f} | {hr} |")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
