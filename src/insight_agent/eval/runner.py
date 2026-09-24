"""Runner：跑用例、聚合指标、存 JSON + Markdown 报告。

每条用例流水线：
  构造被测对象（unit=单节点 / e2e=全图）→ invoke（挂 token 回调 + 计时）
  → 结构断言 → judge（可选）→ CaseResult
全部跑完：与上一次同层报告做回归对比，产出 Markdown 报告（可直接进 README）。
"""

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_openai import ChatOpenAI
from openai import OpenAI, RateLimitError

from insight_agent.config import Settings
from insight_agent.eval.dataset import EvalCase
from insight_agent.eval.judge import run_judge, run_judge_multi
from insight_agent.eval.metrics import summarize_tokens


@dataclass
class CaseResult:
    case_id: str
    tier: str
    passed: bool
    checks: list[dict] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)
    latency_s: float = 0.0
    error_kind: str | None = None  # rate_limit / exception / None
    error: str | None = None
    output_chars: int = 0
    hallucination_rate: float | None = None  # 模块 B 验证层产出（e2e）
    claim_score: float | None = None


@dataclass
class CaseOutput:
    """被测节点的输出文本（判分用），与指标分开传递。"""

    text: str


def _score(case: EvalCase, output: str, sub_questions: list[str], latency: float) -> list[dict]:
    checks: list[dict] = []
    e = case.expect

    if e.sub_questions_min is not None:
        ok = len(sub_questions) >= e.sub_questions_min
        checks.append({"name": "sub_questions_min", "passed": ok, "detail": f"提纲 {len(sub_questions)} 条 ≥ {e.sub_questions_min}"})
    if e.sub_questions_max is not None:
        ok = len(sub_questions) <= e.sub_questions_max
        checks.append({"name": "sub_questions_max", "passed": ok, "detail": f"提纲 {len(sub_questions)} 条 ≤ {e.sub_questions_max}"})
    if e.keywords_any:
        joined = " ".join(sub_questions)
        ok = any(k in joined for k in e.keywords_any)
        checks.append({"name": "keywords_any", "passed": ok, "detail": f"提纲需命中 {'/'.join(e.keywords_any)} 之一"})
    if e.contains_all:
        missing = [k for k in e.contains_all if k not in output]
        checks.append({"name": "contains_all", "passed": not missing, "detail": f"缺少 {missing}" if missing else "全部包含"})
    if e.min_links is not None:
        n = output.count("](http")
        checks.append({"name": "min_links", "passed": n >= e.min_links, "detail": f"来源链接 {n} 个 ≥ {e.min_links}"})
    if e.max_latency_s is not None:
        checks.append({"name": "max_latency_s", "passed": latency <= e.max_latency_s, "detail": f"时延 {latency:.0f}s ≤ {e.max_latency_s}s"})
    if e.key_points:
        # golden set 要点覆盖率："|"分隔同义写法，命中任一算覆盖（规格 §3.3）
        covered = sum(
            1 for point in e.key_points
            if any(alt in output for alt in point.split("|"))
        )
        ratio = covered / len(e.key_points)
        need = e.key_points_min if e.key_points_min is not None else 1.0
        checks.append({"name": "key_points_coverage", "passed": ratio >= need,
                       "detail": f"要点覆盖 {covered}/{len(e.key_points)} = {ratio:.0%}（需 ≥ {need:.0%}）"})
    return checks


def _invoke_case(case: EvalCase, settings, store_root: Path, handler) -> tuple[str, list[str], dict, dict]:
    """按层执行被测对象，返回 (输出文本, 提纲, token 总量, verification)。

    关键：每条用例构造专属 LLM 实例，回调挂进构造函数 ——
    实例回调自动传播到它的所有调用（含 with_structured_output
    和 researcher 子 agent），无需层层穿 config。
    """
    from insight_agent.graph.nodes import make_planner, make_writer
    from insight_agent.graph.build import build_research_graph
    from insight_agent.memory.notes import NotesStore

    llm = ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=0,
        callbacks=[handler],
    )

    if case.node == "planner":
        out = make_planner(llm)({"topic": case.topic, "existing_digest": case.existing_digest})
        return "", out["brief"], summarize_tokens(handler), {}
    if case.node == "writer":
        out = make_writer(llm)(
            {"topic": case.topic, "existing_notes": "", "compressed_findings": case.evidence}
        )
        return out["report"], [], summarize_tokens(handler), {}
    # e2e：全流水线
    graph = build_research_graph(llm, notes_store=NotesStore(store_root / "e2e_notes"), settings=settings)
    result = graph.invoke({"topic": case.topic}, config={"callbacks": [handler]})
    return (
        result["report"],
        result["brief"],
        summarize_tokens(handler),
        result.get("verification", {}),
    )


def run_case(case: EvalCase, settings, judge_client: OpenAI | None, judge_model: str, store_root: Path) -> CaseResult:
    t0 = time.perf_counter()
    handler = UsageMetadataCallbackHandler()
    try:
        output, sub_questions, tokens, verification = _invoke_case(case, settings, store_root, handler)
    except RateLimitError as e:
        return CaseResult(case.id, case.tier, False, [], {}, time.perf_counter() - t0, "rate_limit", f"API 限流：{str(e)[:100]}")
    except Exception as e:  # noqa: BLE001
        return CaseResult(case.id, case.tier, False, [], {}, time.perf_counter() - t0, "exception", f"{type(e).__name__}: {e}")

    latency = time.perf_counter() - t0
    checks = _score(case, output, sub_questions, latency)
    if case.judge and judge_client is not None:
        # e2e 层用多票制（规格 §3.2），unit 层单票省额度
        if case.tier == "e2e":
            ok, detail = run_judge_multi(judge_client, judge_model, case.judge, output)
        else:
            ok, detail = run_judge(judge_client, judge_model, case.judge, output)
        checks.append({"name": "judge", "passed": ok, "detail": detail})
    return CaseResult(
        case_id=case.id,
        tier=case.tier,
        passed=all(c["passed"] for c in checks),
        checks=checks,
        tokens=tokens,
        latency_s=latency,
        output_chars=len(output),
        hallucination_rate=verification.get("hallucination_rate"),
        claim_score=verification.get("score"),
    )
