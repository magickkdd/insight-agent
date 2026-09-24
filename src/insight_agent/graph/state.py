"""研究流水线的 State：带记忆的两段式加工链。

findings 是并行聚合字段：planner 拆出的 N 个子问题由 N 个
research_one 实例并行研究（LangGraph Send map-reduce），用
operator.add 把各段证据合并成列表。
"""

import operator
from typing import Annotated, TypedDict


class ResearchState(TypedDict):
    topic: str
    existing_notes: str       # recall：笔记库里的历史证据（全文，给 writer）
    existing_digest: str      # recall：档案目录（标题行，给 planner 判断覆盖度）
    brief: list[str]          # planner：增量子问题清单（空 = 已充分覆盖）
    findings: Annotated[list[str], operator.add]  # 各并行研究员的证据（自动汇聚）
    compressed_findings: str  # compress：超长证据压缩后的版本
    report: str               # writer：最终报告
    saved: bool               # archive：是否写回笔记库
