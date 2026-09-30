"""对抗审查层（REDTEAM_SPEC）：verify 回答"每句话有没有证据"，
redteam 回答"这份报告敢不敢直接交给客户"。

报告级、站在客户立场的独立审查 Agent，三个维度：
  evidence  证据真实性 —— 抽查 claim 引用与证据池是否对得上
  logic     逻辑一致性 —— 结论互斥 / 因果跳跃 / 与 verification.unsupported 自洽
  coverage  覆盖面     —— 报告章节对照 planner 提纲是否漏答子问题

verify 发现不了"两条 claim 各自有证据但互相矛盾"，这是本层存在的理由。
判定规则写死在节点里（不给模型自由裁量）：任一 blocker → revise；仅 minor → pass。
"""

import json
import random
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from insight_agent.config import Settings
from insight_agent.memory.evidence_pool import EvidencePool

SAMPLE_CLAIMS = 5  # 证据抽查条数上限（成本有界：单轮 = 1 次全文调用 + ≤5 条采样）

REDTEAM_PROMPT = """你是站在客户立场的产品审查员（红队）。下面这份研究报告即将交付，
你的职责是找出会让客户投诉的问题，宁可错杀不可放过。

审查三个维度：
1. evidence 证据真实性：对照"证据池抽查样本"核对报告引用——
   报告里的 URL 是否真的出现在证据里？数字是否与证据段落一致？
   报告引用了证据里不存在的来源或数字 → blocker。
2. logic 逻辑一致性：先把报告里针对同一主体（同一公司份额、同一市场规模/排名）的数值与排名断言
   两两配对，任何一对互斥（例如份额既 45% 第一、又不足 20% 第四）→ **即使两条各自都有出处，
   也必须出 logic issue**；quote 只能逐字复制其中一处原文，另一处写进 reason；
   再查因果断言是否跳过证据、报告说法与"未支撑陈述清单"是否自洽
   （把 unsupported 说成确定结论 → major）。
3. coverage 覆盖面：对照研究提纲逐条检查，报告是否漏答了某个子问题（major/minor）。

规则：
- quote 必须是报告原文片段（逐字复制，供定位），定位不了的质疑不要写
- 每条 issue 给出 severity：blocker（必须打回重写）/ major / minor
- 没有问题就返回空数组，不要为了表现而硬找

只输出 JSON：{"issues": [{"dimension": "evidence|logic|coverage", "severity": "blocker|major|minor", "quote": "报告原文片段", "reason": "一句话质疑理由"}]}"""

RECHECK_PROMPT = """你是上一轮打回了这份报告的审查员。报告已按你的意见修订，
现在只复查一件事：上一轮的每条意见是否真正解决了。

- 已解决的意见不要再列出
- 仍存在或改头换面重现的 → 原样列出（quote 用修订后报告的新原文）
- 修订引入的新 blocker 也要列出

只输出 JSON：{"issues": [{"dimension": "evidence|logic|coverage", "severity": "blocker|major|minor", "quote": "报告原文片段", "reason": "一句话质疑理由"}]}"""


class Issue(BaseModel):
    """单条审查意见。quote 必须能在报告里定位（节点侧校验，防内容注入）。"""

    dimension: Literal["evidence", "logic", "coverage"]
    severity: Literal["blocker", "major", "minor"]
    quote: str = Field(description="报告中被质疑的原文片段（逐字复制）")
    reason: str = Field(description="一句话质疑理由")


class RedTeamOut(BaseModel):
    issues: list[Issue] = Field(default_factory=list, description="审查意见列表，无问题则为空")


@dataclass
class RedTeamReport:
    verdict: Literal["pass", "revise"]
    issues: list[Issue] = field(default_factory=list)
    skipped: bool = False
    error: str | None = None
    # 节点的路由决策（as-built 增量字段）：revise=打回 writer / degrade=轮数耗尽降级归档 /
    # review=放行。判定权在节点（它持有 cfg），条件边只读 state，消除循环歧义。
    action: Literal["review", "revise", "degrade"] = "review"

    @property
    def blocker_count(self) -> int:
        return sum(i.severity == "blocker" for i in self.issues)

    def to_dict(self) -> dict:
        """含 issues 全量，供 eval / Langfuse trace 消费。"""
        return {
            "verdict": self.verdict,
            "action": self.action,
            "issues": [i.model_dump() for i in self.issues],
            "blocker_count": self.blocker_count,
            "skipped": self.skipped,
            "error": self.error,
        }


def _sample_evidence(report: str, verification: dict, pool: EvidencePool) -> str:
    """抽查素材：从 verification 的 claim 里采样 ≤5 条，配证据池实际段落。

    池里搜不到的 claim 照样注入（"证据池查无此段"本身就是审查线索）。
    """
    claims = [c.get("text", "") for c in (verification.get("claims") or []) if c.get("text")]
    if not claims:
        claims = [s + "。" for s in report.split("。") if len(s) > 15][:SAMPLE_CLAIMS]
    if len(claims) > SAMPLE_CLAIMS:
        claims = random.sample(claims, SAMPLE_CLAIMS)
    blocks = []
    for i, text in enumerate(claims, 1):
        passages = pool.search(text, top_k=2)
        ctx = "\n".join(f"来源：{c.url}\n段落：{c.text[:400]}" for c in passages) or "（证据池查无相关段落）"
        blocks.append(f"【抽查{i}】报告陈述：{text}\n证据池实际内容：\n{ctx}")
    return "\n\n".join(blocks)


def _locate(quote: str, report: str) -> bool:
    """quote 定位校验：先精确匹配，再退到去空白匹配（防模型转写漂移）。"""
    if not quote:
        return False
    if quote in report:
        return True
    return "".join(quote.split()) in "".join(report.split())


def _format_issues(issues: list[dict]) -> str:
    return "\n".join(
        f"- [{i['dimension']}/{i['severity']}] 「{i['quote']}」：{i['reason']}" for i in issues
    )


def _degrade_note(issues: list[Issue]) -> str:
    heads = "；".join(f"[{i.dimension}/{i.severity}] {i.reason}" for i in issues[:3])
    more = f"（等共 {len(issues)} 条）" if len(issues) > 3 else ""
    return f"> ⚠️ 对抗审查未通过，已达修订轮数上限，降级归档。审查意见：{heads}{more}\n\n"


def make_redteam_node(llm, cfg: Settings, pool: EvidencePool):
    """redteam 节点工厂（签名对齐 make_verify_node）。pool 用于抽查
    "报告引用 vs 证据池实际内容"是否对得上。

    enabled 支持 auto（仅 deep 档启用，分级交付：fast/standard 不加时延）/ true / false。
    修订轮数上限 redteam_max_rounds 是代码不变量：预算含首轮审查，
    故默认 1 表示"只审一次、不合格直接降级归档"，max_rounds=n 才允许 n-1 次修订；
    耗尽即降级归档，绝不无限循环。
    判定规则写死：任一 blocker → revise；仅 major/minor → pass（issues 照写留档）。
    """

    def _enabled_for(depth: str) -> bool:
        flag = cfg.redteam_enabled
        if flag == "true":
            return True
        if flag == "false":
            return False
        return depth == "deep"  # auto

    def redteam(state: dict) -> dict:
        report = state.get("report", "")
        rounds = state.get("redteam_rounds", 0)
        out: dict = {}

        if not _enabled_for(state.get("depth") or "standard"):
            out["redteam"] = RedTeamReport(verdict="pass", skipped=True).to_dict()
            return out

        # 第 2 轮起只复查上轮 issues 是否解决（REDTEAM_MAX_ROUNDS>1 时生效）
        prev_issues = []
        if rounds > 0:
            prev_issues = (state.get("redteam") or {}).get("issues", [])
            prompt = RECHECK_PROMPT
        else:
            prompt = REDTEAM_PROMPT

        try:
            sections = [
                f"当前日期基准下的报告全文：\n{report[:8000]}",
                f"研究提纲（覆盖面对照用）：\n{json.dumps(state.get('brief', []), ensure_ascii=False)}",
            ]
            digest = state.get("existing_digest", "")
            if digest:
                sections.append(f"已有档案目录（覆盖面对照用）：\n{digest[:1500]}")
            unsupported = [
                c.get("text", "")
                for c in (state.get("verification") or {}).get("claims", [])
                if c.get("verdict") == "unsupported"
            ]
            if unsupported:
                sections.append("verify 判定的未支撑陈述清单（逻辑自洽对照用）：\n" + "\n".join(unsupported[:10]))
            if prev_issues:
                sections.append("上一轮审查意见：\n" + _format_issues(prev_issues))
            else:
                sections.append("证据池抽查样本：\n" + _sample_evidence(report, state.get("verification") or {}, pool))

            result = llm.with_structured_output(RedTeamOut).invoke("\n\n".join(sections) + f"\n\n{prompt}")
            issues = [
                i for i in result.issues
                if _locate(i.quote, report)  # 定位失败 = 不可信意见，按畸形输出丢弃（防注入）
            ]
            issues = issues[: cfg.redteam_max_issues]
        except Exception as e:  # noqa: BLE001 - 审查失败不拦交付，记录后放行
            out["redteam"] = RedTeamReport(verdict="pass", error=f"{type(e).__name__}: {e}").to_dict()
            return out

        rd = RedTeamReport(
            verdict="revise" if any(i.severity == "blocker" for i in issues) else "pass",
            issues=issues,
        )

        # 轮数预算"含首轮审查"：允许触发的修订次数 = max_rounds - 1
        if rd.verdict == "revise" and rounds + 1 < cfg.redteam_max_rounds:
            # 触发修订：消耗一轮，issues 经 state 流转给 writer（修订提示词注入）
            rd.action = "revise"
            out["redteam"] = rd.to_dict()
            out["redteam_rounds"] = rounds + 1
        elif rd.verdict == "revise":
            # 轮数耗尽：降级归档，⚠️ 标注落盘 + verdict=revise 落 state（不粉饰成 pass）
            rd.action = "degrade"
            out["redteam"] = rd.to_dict()
            annotated = state.get("annotated_report") or report
            out["annotated_report"] = _degrade_note(issues) + annotated
        else:
            out["redteam"] = rd.to_dict()
        return out

    return redteam
