"""评测用例 schema 与加载。

用例分两层（成本分层是评测体系的核心设计）：
  unit 层：单节点评测（planner / writer），每次调用 1-2 次 LLM，回归常用
  e2e  层：全流水线，分钟级+大额度，按需/夜间跑

期望（expect）支持的判定：
  结构断言（确定性，零成本）：
    sub_questions_min / sub_questions_max   planner 提纲条数
    keywords_any                            提纲里至少命中一个关键词
    contains_all                            报告必须包含的章节/字样
    min_links                               报告最少来源链接数
  judge（软性，花一次调用）：判分问题，PASS/FAIL
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_CASE_KEYS = {"id", "tier", "node", "topic", "existing_digest", "evidence", "expect", "judge"}
_TIER_KEYS = {"unit", "e2e"}
_NODE_KEYS = {"planner", "writer", None}
_EXPECT_KEYS = {
    "sub_questions_min",
    "sub_questions_max",
    "keywords_any",
    "contains_all",
    "min_links",
    "max_latency_s",
}


@dataclass
class Expectations:
    sub_questions_min: int | None = None
    sub_questions_max: int | None = None
    keywords_any: list[str] = field(default_factory=list)
    contains_all: list[str] = field(default_factory=list)
    min_links: int | None = None
    max_latency_s: float | None = None


@dataclass
class EvalCase:
    id: str
    tier: str
    node: str | None          # unit 层被测节点：planner / writer
    topic: str
    existing_digest: str = ""  # planner 记忆场景：给档案目录
    evidence: str = ""         # writer 单测：喂固定证据
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
                expect=Expectations(**expect_raw),
                judge=item.get("judge"),
            )
        )
        seen.add(cid)
    return cases
