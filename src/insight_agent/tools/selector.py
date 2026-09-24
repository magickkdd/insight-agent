"""URL 选择器：LLM 从搜索结果里挑最值得深读的网页（规格 §1.3）。

失败降级：结构化输出失败 / 选出的 URL 不在候选里 → 取前 max_docs 条。
"""

from pydantic import BaseModel, Field


class UrlSelection(BaseModel):
    urls: list[str] = Field(description="最值得深读原文的 URL 列表")


def select_urls(
    snippets: list[dict], question: str, *, max_docs: int, llm
) -> list[str]:
    if max_docs <= 0 or not snippets:
        return []
    valid = {s["url"] for s in snippets}
    lines = "\n".join(
        f"{i + 1}. {s['title']} — {s['snippet'][:150]}（{s['url']}）"
        for i, s in enumerate(snippets)
    )
    try:
        out = llm.with_structured_output(UrlSelection).invoke(
            f"研究子问题：{question}\n\n候选网页：\n{lines}\n\n"
            f"选出最值得深读原文的至多 {max_docs} 个 URL（优先权威来源、数据型内容）。"
        )
        urls = [u for u in out.urls if u in valid][:max_docs]
        return urls or [s["url"] for s in snippets[:max_docs]]
    except Exception:  # noqa: BLE001 - 选择失败退化为按序取前 N
        return [s["url"] for s in snippets[:max_docs]]
