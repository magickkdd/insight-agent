"""研究流水线的节点集（六个，各司其职）。

  recall   查档案 → existing_notes
  planner  看档案找缺口 → 增量提纲（已充分覆盖则返回空清单）
  researcher 预制 agent + 搜索，只研究增量
  compress 超长证据摘要压缩（M3 窗口管理的产品化）
  writer   旧档案 + 新证据 → 报告
  archive  新证据写回笔记库
"""

import time
from pathlib import Path

from pydantic import BaseModel, Field

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch

from insight_agent.memory.notes import NotesStore

RESEARCHER_SYSTEM = """你是严谨的行业研究员。针对研究提纲中的子问题逐个搜索取证。

纪律：
- 每个子问题至少一次搜索；搜索失败就换关键词重试（失败信息会返回给你）
- 只记录有出处的事实，每条证据标注来源链接
- 证据用要点列出，格式：[来源](url) 的 markdown 链接
- 搜不到的子问题明确标注"未找到可靠来源"，不要编造
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


def make_research_one(llm: ChatOpenAI):
    """单问题研究员：并行 fan-out 的"工人"。

    每个实例只研究一个子问题（接收 {"question": str}），
    多个实例由 Send 并行拉起 —— 串行 4×30s 变并行 ~30s。
    """
    agent = create_agent(
        llm,
        tools=[TavilySearch(max_results=5)],
        system_prompt=RESEARCHER_SYSTEM,
    )

    def research_one(state: dict) -> dict:
        result = agent.invoke(
            {"messages": [("user", f"研究子问题：{state['question']}\n请取证并汇总成证据笔记。")]},
            config={"recursion_limit": 30},
        )
        return {"findings": [f"### 子问题：{state['question']}\n\n{result['messages'][-1].content}"]}

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
