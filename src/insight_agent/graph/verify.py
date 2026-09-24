"""验证层（UPGRADE_SPEC §2）：claim 提取 → 逐条核对 → 幻觉率与标注版报告。"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

EXTRACT_PROMPT = """把下面的报告拆成原子陈述（claim）列表。

规则：
- 一个数字、一个因果断言、一个排名、一个事实断言 = 一条 claim
- 问候语、格式标题、免责声明不算 claim
- 保持原文措辞，不要改写

只输出 JSON：{"claims": ["陈述1", "陈述2"]}"""

VERDICT_PROMPT = """判断"陈述"是否被"证据段落"支撑。

- supported：段落原文直接支撑该陈述
- partial：段落只支撑陈述的一部分，或相关但更弱
- unsupported：段落与陈述矛盾，或完全无关
- not_checkable：证据段落与此陈述无关（无法核对）

只输出 JSON：{"verdict": "supported|partial|unsupported|not_checkable", "reason": "一句话", "quote": "支撑该判定的段落原文片段"}"""


class ClaimsOut(BaseModel):
    claims: list[str] = Field(description="原子陈述列表")


class VerdictOut(BaseModel):
    verdict: str
    reason: str = ""
    quote: str = ""


@dataclass
class Claim:
    text: str
    verdict: Literal["supported", "partial", "unsupported", "not_checkable"]
    evidence: list[dict] = field(default_factory=list)  # [{url}]
    reason: str = ""
    quote: str = ""


@dataclass
class VerificationReport:
    claims: list[Claim] = field(default_factory=list)
    skipped: bool = False
    error: str | None = None

    @property
    def hallucination_rate(self) -> float | None:
        scored = [c for c in self.claims if c.verdict in ("supported", "partial", "unsupported")]
        if not scored:
            return None
        return sum(c.verdict == "unsupported" for c in scored) / len(scored)

    @property
    def coverage_rate(self) -> float | None:
        if not self.claims:
            return None
        return sum(c.verdict == "not_checkable" for c in self.claims) / len(self.claims)

    def score(self) -> float | None:
        """综合可信度：幻觉率是主罚项，not_checkable 占比轻罚。"""
        hr = self.hallucination_rate
        if hr is None:
            return None
        cov = self.coverage_rate or 0.0
        return round(100 - 100 * hr - 10 * cov, 1)

    def to_dict(self) -> dict:
        return {
            "claims": [
                {"text": c.text, "verdict": c.verdict, "evidence": c.evidence, "reason": c.reason}
                for c in self.claims
            ],
            "hallucination_rate": self.hallucination_rate,
            "coverage_rate": self.coverage_rate,
            "score": self.score(),
            "skipped": self.skipped,
            "error": self.error,
        }


def _extract_claims(llm, report: str, max_claims: int) -> list[str]:
    out = llm.with_structured_output(ClaimsOut).invoke(f"{EXTRACT_PROMPT}\n\n报告：\n{report[:6000]}")
    return out.claims[:max_claims]


def _annotate(report: str, claims: list[Claim]) -> str:
    """在报告原文里给 unsupported/partial 的句子加标记（每条只标一处）。"""
    marks = {"unsupported": " ⚠️[未找到证据支撑]", "partial": " ⚠️[部分支撑]"}
    for c in claims:
        if c.verdict in marks and c.text in report:
            report = report.replace(c.text, f"{c.text}{marks[c.verdict]}", 1)
    return report


def make_verify_node(llm, verify_enabled: bool, max_claims: int, concurrency: int, pool, card_store=None):
    """verify 节点工厂。pool 为跨节点共享的 EvidencePool（research_one 写入，verify 读取）。

    card_store 非空时：supported/partial 的 claim 自动转为 FactCard 入长期记忆
    （B→C 架构联动：验证层是记忆层的质检关，规格 §9.3）。
    """

    def verify(state: dict) -> dict:
        report = state.get("report", "")
        if not verify_enabled:
            vr = VerificationReport(skipped=True)
            return {"verification": vr.to_dict(), "annotated_report": report}

        try:
            claim_texts = _extract_claims(llm, report, max_claims)
        except Exception as e:  # noqa: BLE001 - 验证失败不拦交付，记录后放行
            vr = VerificationReport(error=f"{type(e).__name__}: {e}")
            return {"verification": vr.to_dict(), "annotated_report": report}

        def check(text: str) -> Claim:
            try:
                passages = pool.search(text, top_k=3)
                if not passages:
                    return Claim(text, "not_checkable", [], "证据池无相关段落")
                context = "\n\n---\n\n".join(f"来源：{c.url}\n段落：{c.text}" for c in passages)
                v = llm.with_structured_output(VerdictOut).invoke(
                    f"陈述：{text}\n\n证据段落：\n{context}"
                )
                verdict = v.verdict if v.verdict in ("supported", "partial", "unsupported", "not_checkable") else "not_checkable"
                return Claim(
                    text, verdict,
                    [{"url": p.url} for p in passages[:2]],
                    v.reason, quote=v.quote,
                )
            except Exception as e:  # noqa: BLE001 - 单条失败不拖垮整批
                return Claim(text, "not_checkable", [], f"判定异常：{type(e).__name__}")

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            claims = list(ex.map(check, claim_texts))

        vr = VerificationReport(claims=claims)

        # B→C 联动：核对通过的事实进长期记忆（事实卡片）
        if card_store is not None:
            from insight_agent.memory.fact_cards import FactCard

            for c in claims:
                if c.verdict in ("supported", "partial") and c.evidence:
                    card_store.add(
                        FactCard(
                            claim=c.text,
                            source_url=c.evidence[0].get("url", ""),
                            source_quote=c.quote,
                            topic=state.get("topic", ""),
                            confidence=1.0 if c.verdict == "supported" else 0.6,
                        )
                    )

        return {"verification": vr.to_dict(), "annotated_report": _annotate(report, claims)}

    return verify
