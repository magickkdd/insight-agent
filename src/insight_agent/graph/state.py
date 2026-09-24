"""研究流水线的 State：带记忆的两段式加工链。

字段按写入者分四组：
  recall    → existing_notes（历史档案，可能为空）
  planner   → brief（只含增量子问题，可能为空 = 不用再研究）
  researcher→ findings（本轮新证据）→ compress → compressed_findings
  writer    → report；archive → saved
"""

from typing import TypedDict


class ResearchState(TypedDict):
    topic: str
    existing_notes: str       # recall：笔记库里的历史证据（全文，给 writer）
    existing_digest: str      # recall：档案目录（标题行，给 planner 判断覆盖度）
    brief: list[str]          # planner：增量子问题清单（空 = 已充分覆盖）
    findings: str             # researcher：本轮新收集的证据
    compressed_findings: str  # compress：超长证据压缩后的版本
    report: str               # writer：最终报告
    saved: bool               # archive：是否写回笔记库
