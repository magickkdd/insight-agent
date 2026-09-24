"""研究流水线的节点集（六个，各司其职）。

  recall   查档案 → existing_notes
  planner  看档案找缺口 → 增量提纲（已充分覆盖则返回空清单）
  researcher 预制 agent + 搜索，只研究增量
  compress 超长证据摘要压缩（M3 窗口管理的产品化）
  writer   旧档案 + 新证据 → 报告
  archive  新证据写回笔记库
"""

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, Field

from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch

from insight_agent.config import Settings
from insight_agent.memory.evidence_pool import EvidencePool
from insight_agent.memory.notes import NotesStore
from insight_agent.tools.chunker import chunk_text
from insight_agent.tools.embedder import get_embedder_cached
from insight_agent.tools.fetcher import _domain, fetch_readable
from insight_agent.tools.selector import select_urls

RESEARCHER_SYSTEM = """你是严谨的行业研究员。基于提供的网页正文段落或搜索摘要撰写证据笔记。

纪律：
- 只记录有出处的事实，每条证据标注 [来源N](url)
- 证据用要点列出；材料没覆盖的方面明确标注"未找到可靠来源"，不要编造
- 相互矛盾的来源并列呈现，不擅自取舍
"""

WRITER_SYSTEM = """你是行业分析师。基于证据笔记撰写结构化报告。

格式：
# {主题}
## 核心发现（3-5 条，每条标注来源）
## 详细分析
## 风险与局限（说明哪些信息未能核实）

引用纪律（必须遵守）：
- 每条引用保留完整 markdown 链接：[来源N](url)，例如 [来源1](https://example.com/a)
- 禁止只写 [来源N] 而丢弃 url —— 丢链接的引用等于无法核查
铁律：只使用证据笔记中的事实，绝不编造数据和来源。
"""

COMPRESS_SYSTEM = """你是编辑。把下面的研究证据压缩到 3000 字以内：
- 保留全部关键事实、数字和来源链接
- 删除重复表述和过渡句
- 输出压缩后的笔记本身，不加任何说明
"""

COMPRESS_THRESHOLD = 8000  # 超过这个长度的证据才值得花一次压缩调用


class ResearchBrief(BaseModel):
    sub_questions: list[str] = Field(description="需要补充研究的子问题；已充分覆盖时返回空数组")


def make_recall(store: NotesStore):
    def recall(state: dict) -> dict:
        return {
            "existing_notes": store.load_notes(state["topic"]),
            "existing_digest": store.load_digest(state["topic"]),
        }

    return recall


def make_planner(llm: ChatOpenAI):
    structured = llm.with_structured_output(ResearchBrief)

    def planner(state: dict) -> dict:
        digest = state.get("existing_digest", "")
        base_prompt = (
            f"研究主题：{state['topic']}\n"
            + (
                f"\n已有研究档案的目录（覆盖度地图）：\n{digest}\n"
                "对照目录判断哪些方面尚未覆盖，只规划真正的增量子问题；"
                "若主要方面已覆盖，返回空数组。\n"
                if digest
                else ""
            )
            + "拆解成 3-5 个具体、可搜索的子问题，覆盖现状、关键玩家、近期动态、风险。"
        )
        # 模型偶发不守"至少 3 条"的规矩 → 带反馈重试一次（代码拥有不变量）
        feedback = ""
        brief = None
        for _ in range(2):
            brief = structured.invoke(base_prompt + feedback)
            if len(brief.sub_questions) >= 3:
                return {"brief": brief.sub_questions}
            feedback = f"\n（注意：你上次只给出 {len(brief.sub_questions)} 条子问题，必须至少 3 条；若主题已充分覆盖则可以为 0 条，但不能是 1-2 条。）"
        return {"brief": brief.sub_questions}

    return planner


DEPTH_MAX_DOCS = {"fast": 0, "standard": 2, "deep": 5}  # 每子问题深读篇数（规格 §1.6）


def make_research_one(llm: ChatOpenAI, settings: Settings):
    """单问题研究员：并行 fan-out 的"工人"（规格 §1.4 七步流程）。

    fast 档：只用搜索摘要（不深读，无 degraded 标记——这是档位的本意）。
    standard/deep 档：搜索 → LLM 选 URL → 并行深读 → 语义检索相关段落 → 成文；
    深读全败时降级回摘要模式并打 ⚠degraded 标记。
    """
    max_docs = DEPTH_MAX_DOCS[settings.research_depth]
    tavily = TavilySearch(max_results=8)
    embedder = get_embedder_cached(settings)
    blocked = {d.strip() for d in settings.fetch_block_domains.split(",") if d.strip()}

    def _search(query: str) -> list[dict]:
        for _ in range(2):  # 搜索失败重试 1 次
            try:
                raw = tavily.invoke({"query": query})
                results = raw.get("results", []) if isinstance(raw, dict) else []
                if results:
                    return [
                        {
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "snippet": r.get("content", ""),
                        }
                        for r in results
                        if r.get("url")
                    ]
            except Exception:  # noqa: BLE001 - 搜索异常换姿势重试
                continue
        return []

    def research_one(state: dict) -> dict:
        question = state["question"]
        snippets = _search(question)
        if not snippets:
            return {"findings": [f"### 子问题：{question}\n\n⚠degraded：搜索源全部失败，未取得证据。"]}

        pool = EvidencePool()
        pool.bind_embedder(embedder)
        degraded = 0
        if max_docs > 0:
            urls = [
                u
                for u in select_urls(snippets, question, max_docs=max_docs, llm=llm)
                if _domain(u) not in blocked
            ]
            with ThreadPoolExecutor(max_workers=settings.fetch_max_concurrency) as ex:
                docs = list(ex.map(lambda u: fetch_readable(u, timeout=settings.fetch_timeout), urls))
            for doc in docs:
                if doc.status == "fetched" and doc.text:
                    pool.add_chunks(chunk_text(doc.url, doc.text))
                else:
                    degraded += 1
            passages = pool.search(question, top_k=4)
        else:
            passages = []

        if passages:
            context = "\n\n---\n\n".join(f"来源：{c.url}\n段落：{c.text}" for c in passages)
            mode_prompt = "以下为网页正文段落（深读证据）。基于段落原文撰写证据笔记，每条事实标注 [来源N](url)。"
        else:
            context = "\n\n".join(f"来源：{s['url']}\n摘要：{s['snippet']}" for s in snippets)
            if settings.research_depth == "fast":
                mode_prompt = "以下为搜索摘要。基于摘要撰写证据笔记，每条事实标注 [来源N](url)。"
            else:
                degraded = degraded or len(snippets)
                mode_prompt = "⚠本次未能深读原文，仅基于搜索摘要撰写证据笔记，每条事实标注 [来源N](url)。"

        result = llm.invoke(
            [
                ("system", RESEARCHER_SYSTEM),
                ("user", f"研究子问题：{question}\n\n{mode_prompt}\n\n{context}"),
            ]
        )
        header = f"### 子问题：{question}\n\n"
        if degraded:
            header += f"⚠degraded（{degraded} 篇深读失败，已降级为摘要模式）\n\n"
        return {"findings": [f"{header}{result.content}"]}

    return research_one


def make_compress(llm: ChatOpenAI):
    def compress(state: dict) -> dict:
        joined = "\n\n".join(state["findings"])  # 并行证据汇聚成一份
        if len(joined) <= COMPRESS_THRESHOLD:
            return {"compressed_findings": joined}  # 不超限直接过，省一次调用
        result = llm.invoke(
            [("system", COMPRESS_SYSTEM), ("user", joined)]
        )
        return {"compressed_findings": result.content}

    return compress


def make_writer(llm: ChatOpenAI):
    def writer(state: dict) -> dict:
        existing = state.get("existing_notes", "")
        fresh = state.get("compressed_findings", "")
        evidence = (
            f"{existing}\n\n## 本轮新证据\n\n{fresh}" if existing else fresh
        )
        result = llm.invoke(
            [("system", WRITER_SYSTEM), ("user", f"主题：{state['topic']}\n\n证据笔记：\n{evidence}")]
        )
        return {"report": result.content}

    return writer


def make_archive(store: NotesStore, reports_dir: str | Path = "data/reports"):
    def archive(state: dict) -> dict:
        report = state.get("report", "")
        store.merge(state["topic"], "\n\n".join(state.get("findings", [])), report=report or None)
        # 报告单独落一份 Markdown，方便直接翻文件
        if report:
            rdir = Path(reports_dir)
            rdir.mkdir(parents=True, exist_ok=True)
            fname = time.strftime("%Y%m%d_%H%M%S") + ".md"
            (rdir / fname).write_text(f"# {state['topic']}\n\n{report}", encoding="utf-8")
        return {"saved": True}

    return archive
