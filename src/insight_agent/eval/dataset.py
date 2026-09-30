"""评测用例 schema 与加载。

用例分两层（成本分层是评测体系的核心设计）：
  unit 层：单节点评测（planner / writer / redteam），每次调用 1-2 次 LLM，回归常用
  e2e  层：全流水线，分钟级+大额度，按需/夜间跑

期望（expect）支持的判定：
  结构断言（确定性，零成本）：
    sub_questions_min / sub_questions_max   planner 提纲条数
    keywords_any                            提纲里至少命中一个关键词
    contains_all                            报告必须包含的章节/字样
    min_links                               报告最少来源链接数
    redteam_verdict                         对抗审查判定必须等于该值（pass/revise）
    redteam_dimensions                      审查 issues 必须命中这些维度（evidence/logic/coverage）
    redteam_quote_hits                      每个关键词必须出现在某条 issue 的 quote 里（T2 定位注入内容）
    redteam_quote_any                       至少一个关键词出现在某条 issue 的 quote 里
                                            （矛盾对审查只摘互斥双方之一，逐字 quote 无法两边都锚死）
    —— redteam_* 作用于 case 级 redteam_votes（默认 1）次审查的 issues 并集：
       审查判定本身有跨轮抖动（REDTEAM_SPEC §9.2），单票断言等于在测骰子。
  judge（软性，花一次调用）：判分问题，PASS/FAIL
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_CASE_KEYS = {"id", "tier", "node", "topic", "existing_digest", "evidence", "expect", "judge",
              "report", "pool_evidence", "redteam_votes"}
_TIER_KEYS = {"unit", "e2e"}
_NODE_KEYS = {"planner", "writer", "redteam", None}
_EXPECT_KEYS = {
    "sub_questions_min",
    "sub_questions_max",
    "keywords_any",
    "contains_all",
    "min_links",
    "max_latency_s",
    "key_points",
    "key_points_min",
    "redteam_verdict",
    "redteam_dimensions",
    "redteam_quote_hits",
    "redteam_quote_any",
}


@dataclass
class Expectations:
    sub_questions_min: int | None = None
    sub_questions_max: int | None = None
    keywords_any: list[str] = field(default_factory=list)
    contains_all: list[str] = field(default_factory=list)
    min_links: int | None = None
    max_latency_s: float | None = None
    key_points: list[str] = field(default_factory=list)  # golden set：标准答案要点，"|"分隔同义写法
    key_points_min: float | None = None                  # 要点覆盖率下限（0-1）
    redteam_verdict: str | None = None                   # 对抗审查判定（pass/revise）
    redteam_dimensions: list[str] = field(default_factory=list)  # issues 必须命中的维度
    redteam_quote_hits: list[str] = field(default_factory=list)  # 必须出现在 issue quote 里的关键词
    redteam_quote_any: list[str] = field(default_factory=list)   # 至少一个出现在 issue quote 里的关键词


@dataclass
class EvalCase:
    id: str
    tier: str
    node: str | None          # unit 层被测节点：planner / writer / redteam
    topic: str
    existing_digest: str = ""  # planner 记忆场景：给档案目录
    evidence: str = ""         # writer 单测：喂固定证据
    report: str = ""           # redteam 单测：被审报告全文
    pool_evidence: str = ""    # redteam 单测：预置证据池（文档间用 --- 分隔，首行为 URL）
    redteam_votes: int = 1     # 审查判定有跨轮抖动（SPEC §9.2）：同一输入跑 N 次取 issues 并集再判
    expect: Expectations = field(default_factory=Expectations)
    judge: str | None = None

    def has_expect(self) -> bool:
        e = self.expect
        return bool(
            e.sub_questions_min is not None
            or e.sub_questions_max is not None
            or e.keywords_any
            or e.contains_all
            or e.min_links is not None
            or e.max_latency_s is not None
            or e.redteam_verdict
            or e.redteam_dimensions
            or e.redteam_quote_hits
            or e.redteam_quote_any
            or self.judge
        )


def load_cases(path: str | Path) -> list[EvalCase]:
    raw: list[dict[str, Any]] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    cases: list[EvalCase] = []
    seen: set[str] = set()

    for i, item in enumerate(raw):
        where = f"{path} 第 {i + 1} 条"
        unknown = set(item) - _CASE_KEYS
        if unknown:
            raise ValueError(f"{where}：未知字段 {unknown}")
        cid, tier = item.get("id"), item.get("tier")
        if not cid or not item.get("topic"):
            raise ValueError(f"{where}：id 和 topic 必填")
        if cid in seen:
            raise ValueError(f"{where}：id 重复（{cid}）")
        if tier not in _TIER_KEYS:
            raise ValueError(f"{where}：tier 必须是 {'/'.join(sorted(_TIER_KEYS))}")
        node = item.get("node")
        if node not in _NODE_KEYS:
            raise ValueError(f"{where}：node 必须是 planner/writer（e2e 留空）")
        if tier == "unit" and node is None:
            raise ValueError(f"{where}：unit 层必须指定 node")
        expect_raw = item.get("expect") or {}
        bad = set(expect_raw) - _EXPECT_KEYS
        if bad:
            raise ValueError(f"{where}：expect 拼错 {bad}")

        cases.append(
            EvalCase(
                id=cid,
                tier=tier,
                node=node,
                topic=item["topic"],
                existing_digest=item.get("existing_digest", ""),
                evidence=item.get("evidence", ""),
                report=item.get("report", ""),
                pool_evidence=item.get("pool_evidence", ""),
                redteam_votes=int(item.get("redteam_votes", 1)),
                expect=Expectations(**expect_raw),
                judge=item.get("judge"),
            )
        )
        seen.add(cid)
    return cases
