"""分块器：正文 → 500-800 字块（相邻重叠 ~50 字）。确定性纯函数（规格 §1.2）。

切分优先级：markdown 标题 → 空行段落 → 超长段滑窗。
"""

from dataclasses import dataclass

from insight_agent.tools.fetcher import url_hash


@dataclass
class Chunk:
    chunk_id: str
    url: str
    text: str
    embedding: list[float] | None = None


def _split_long(text: str, size: int, overlap: int) -> list[str]:
    out, start = [], 0
    while start < len(text):
        out.append(text[start : start + size])
        start += size - overlap
    return [s for s in out if len(s.strip()) >= 50]


def chunk_text(url: str, text: str, *, size: int = 700, overlap: int = 50) -> list[Chunk]:
    """把清洗后的正文切成 Chunk 列表。

    标题边界硬切分（不跨节合并，保证块的主题纯度）；
    节内段落合并到目标大小，超长段滑窗；全被过滤时保底保留最大块。
    """
    h = url_hash(url)
    # 第一层：标题切分
    sections: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#") and buf:
            sections.append("\n".join(buf))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        sections.append("\n".join(buf))

    # 第二层：节内段落打包（跨节不合并）
    raw: list[str] = []
    for sec in sections:
        cur = ""
        for para in sec.split("\n\n"):
            if len(cur) + len(para) <= size:
                cur = f"{cur}\n\n{para}".strip()
            else:
                if cur:
                    raw.append(cur)
                cur = para if len(para) <= size else ""
                if not cur:  # 超长段落滑窗
                    raw.extend(_split_long(para, size, overlap))
        if cur:
            raw.append(cur)

    chunks = [c for c in raw if len(c.strip()) >= 50] or (
        [max(raw, key=len)] if raw else []
    )
    return [Chunk(chunk_id=f"{h}:{i}", url=url, text=c.strip()) for i, c in enumerate(chunks)]
