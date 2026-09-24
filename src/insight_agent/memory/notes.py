"""研究笔记库：按主题持久化的长期记忆。

一条档案 = {topic, notes(累积证据), updates(更新历史)}。
写入是"合并"：新证据追加为"增量研究"章节，旧内容永远保留。

诚实边界：主题匹配用规范化 slug 精确匹配（换个措辞就是新档案）。
向量/模糊检索是升级路径，等评测证明需要再做。
写入用"临时文件+原子替换"，崩溃不会留下半截档案。
"""

import json
import re
import time
from pathlib import Path


def _slug(topic: str) -> str:
    """主题 → 文件名安全且稳定的 slug。"""
    t = re.sub(r"\s+", "", topic.lower())
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
    return t[:48] or "untitled"


class NotesStore:
    def __init__(self, root: str | Path = "data/notes"):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, topic: str) -> Path:
        return self._root / f"{_slug(topic)}.json"

    def load_notes(self, topic: str) -> str:
        """取某主题的全部历史证据。无档案返回空串。"""
        p = self._path(topic)
        if not p.exists():
            return ""
        return json.loads(p.read_text(encoding="utf-8"))["notes"]

    def load_digest(self, topic: str) -> str:
        """档案的"目录"：全部 markdown 标题行。

        给 planner 看的覆盖度地图 —— 确定性提取、零成本，
        比截断原文可靠得多（截断会让规划器误判覆盖缺口）。
        """
        notes = self.load_notes(topic)
        if not notes:
            return ""
        headings = [ln.strip() for ln in notes.splitlines() if ln.strip().startswith("#")]
        return "\n".join(headings) if headings else notes[:500]

    def merge(self, topic: str, new_findings: str) -> None:
        """把本轮证据合并进档案（原子写入）。"""
        p = self._path(topic)
        doc = (
            json.loads(p.read_text(encoding="utf-8"))
            if p.exists()
            else {"topic": topic, "created_at": time.strftime("%Y-%m-%d %H:%M"), "updates": [], "notes": ""}
        )
        if doc["notes"]:
            doc["notes"] += f"\n\n## 增量研究（{time.strftime('%Y-%m-%d %H:%M')}）\n\n{new_findings}"
        else:
            doc["notes"] = new_findings
        doc["updates"].append({"at": time.strftime("%Y-%m-%d %H:%M"), "chars": len(new_findings)})

        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)  # 原子替换：要么完整旧档，要么完整新档
