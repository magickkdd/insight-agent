"""研究流水线的节点集（六个，各司其职）。

  recall   查档案 → existing_notes
  planner  看档案找缺口 → 增量提纲（已充分覆盖则返回空清单）
  researcher 预制 agent + 搜索，只研究增量
  compress 超长证据摘要压缩（M3 窗口管理的产品化）
  writer   旧档案 + 新证据 → 报告
  archive  新证据写回笔记库
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch

from insight_agent.config import Settings
from insight_agent.graph.llm_factory import structured
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
时间纪律：用户消息会给出当前日期。"近期/目前"等表述以它为基准；
报告开头注明数据截止时间，以证据中来源的实际时间为准，不编造。
铁律：只使用证据笔记中的事实，绝不编造数据和来源。
"""

COMPRESS_SYSTEM = """你是编辑。把下面的研究证据压缩到 3000 字以内：
- 保留全部关键事实、数字和来源链接
- 删除重复表述和过渡句
- 输出压缩后的笔记本身，不加任何说明
"""

COMPRESS_THRESHOLD = 8000  # 超过这个长度的证据才值得花一次压缩调用


def _today() -> str:
    """当前日期注入 prompt：LLM 的时间观冻结在训练数据截止那一刻，
    不喂日期它嘴里的"最新/近期"就是它记忆里的旧年份。"""
    return datetime.now().strftime("%Y年%m月%d日")


class ResearchBrief(BaseModel):
    sub_questions: list[str] = Field(description="需要补充研究的子问题；已充分覆盖时返回空数组")


class GapJudgement(BaseModel):
    sufficient: bool = Field(description="现有证据是否足以支撑高质量报告")
    missing_aspects: list[str] = Field(description="不足时需要补充研究的具体子问题（至多 3 个）")


def _rename_keys(data: Any, aliases: dict[str, tuple[str, ...]]) -> Any:
    """把模型自创的键名映射回 schema 的键名。

    **为什么需要。** 实测 `deepseek-v4-flash-0731`（Intern InkStone 平台）返回
    `{"值得研究": true, ...}` —— 内容对、键名不照schema 来。

    **只认白名单。** 别名表显式列出，不是"找第一个 bool 值"或"取最长字符串" ——
    那种猜法会在模型输出意外结构时静默给出错误答案，而错误答案比报错贵得多。
    认不出来就原样返回，让 Pydantic 照实报缺字段。

    键名比对忽略大小写与下划线：`is_research` / `isResearch` / `Is-Research`
    算同一个键 —— JSON 键名约定各家不一，但这不改变模型的语义判断。
    """
    if not isinstance(data, dict):
        return data

    def norm(key: str) -> str:
        return key.replace("_", "").replace("-", "").lower()

    present = {norm(k): k for k in data}
    renamed = dict(data)
    for canonical, candidates in aliases.items():
        # 归一后同形但拼写不同（`isResearch` vs `is_research`）也要改回 schema 键名 ——
        # Pydantic 认的是字面键名，不认"等价"。
        actual = present.get(norm(canonical))
        if actual is not None:
            if actual != canonical:
                renamed[canonical] = renamed.pop(actual)
            continue  # 模型已经照 schema 给了（或只是拼写差异），别名不用找
        for candidate in candidates:
            actual = present.get(norm(candidate))
            if actual is not None:
                renamed[canonical] = renamed.pop(actual)
                break
    return renamed


class IntentJudgement(BaseModel):
    """gate 的判定结果。

    键名归一化见 `_rename_keys` —— 有些模型不照 schema 的键名来（实测
    deepseek-v4-flash-0731 返回 `{"值得研究": true, ...}`）。
    """

    is_research: bool = Field(description="用户输入是否是一个值得联网调研的正式研究主题")
    reason: str = Field(description="一句话理由")

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _normalise_keys(cls, data: Any) -> Any:
        return _rename_keys(
            data,
            {
                "is_research": (
                    "isResearch",
                    "值得研究",
                    "是否研究",
                    "需要研究",
                    "research",
                    "需要联网搜索",
                )
            },
        )


class BriefValidator(BaseModel):
    """`ResearchBrief` 的容错版。

    **为什么需要它。** `ResearchBrief` 只有一个字段，实测不同模型有三种退化形态
    （都发生在 `deepseek-v4-flash-0731` 上）：

    1. **塌陷成裸数组** —— 把单字段 wrapper 省掉，直接返回 `["子问题1", ...]`，
       Pydantic 报 `Input should be an object [input_value=[...]]`。
    2. **改键名** —— 返回 `{"子问题": [...]}` 或 `{"questions": [...]}`。
    3. **给子问题配结构** —— 返回
       `[{"id": 1, "维度": "现状", "子问题": "…做了哪些规定？"}, ...]`，
       把它当多智能体的分解表在用。

    三种都是**内容对、形态不对**。这里都接住，但**只接白名单里认得出来的**：
    第 3 种只取已知文本字段，不做「取字典里最长的那个字符串」那种猜法 ——
    猜出来的错答案比报错贵得多。认不出来照实报错。

    判据在 `tests/test_planner_shape.py`。
    """

    sub_questions: list[str]

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _unwrap_single_field(cls, data: Any) -> Any:
        if isinstance(data, list):
            return {"sub_questions": _flatten_questions(data)}
        renamed = _rename_keys(data, {"sub_questions": ("子问题", "子问题列表", "questions", "subquestions")})
        if isinstance(renamed, dict) and isinstance(renamed.get("sub_questions"), list):
            renamed["sub_questions"] = _flatten_questions(renamed["sub_questions"])
        return renamed


#: 子问题里可能是正文的键名（实测模型给的是「子问题」，也有给 question 的）
_QUESTION_TEXT_KEYS = ("子问题", "question", "text", "内容", "query", "prompt")

#: 端点不支持 `response_format` 时（`llm_factory.NO_STRUCTURED_OUTPUT`），
#: JSON 的形状只能靠提示词保证。写成「不要输出别的」比只给个正例更有效 ——
#: 实测这个模型不加这句时会给 Markdown 编号列表（`**1. 现状：…**`）。
_JSON_SHAPE_HINT = (
    "\n只输出一个 JSON 对象，不要任何解释文字、不要 Markdown、不要代码块围栏。"
    '格式：{"sub_questions": ["子问题一", "子问题二", "子问题三"]}'
)


def _flatten_questions(items: list[Any]) -> list[str]:
    """子问题列表 → 纯字符串列表。

    形态是字符串就原样留着；是字典就按 `_QUESTION_TEXT_KEYS` 取正文，取不到就
    保留原值让 Pydantic 报错 —— **不做「挑最长的字符串」这种启发式**：模型
    给了 `{"id": 1, "维度": "现状", "子问题": "…"}` 时，最长的是子问题；但换成
    别的排版，最长的可能是维度字段，猜错会给出一批语义错乱的问题列表，
    而那比现在直接报错难查得多。
    """
    out: list[Any] = []
    for item in items:
        if isinstance(item, dict):
            picked = next(
                (item[k] for k in _QUESTION_TEXT_KEYS if isinstance(item.get(k), str)),
                item,
            )
            out.append(picked)
        else:
            out.append(item)
    return out


def make_gate(llm: ChatOpenAI, settings: Settings, chats=None):
    """入口意图门卫（Routing 模式）：区分"研究请求"与"闲聊/寒暄"。

    闲聊不进研究流水线（省额度、避免对"你好"硬拆提纲的滑稽场面）。
    规则先行：过短输入直接判闲聊，不花 LLM 调用。
    带最近几轮对话历史："继续"这类依赖上下文的输入才判得准。
    """

    def gate(state: dict) -> dict:
        topic = state["topic"].strip()
        # 规则层：太短或明显寒暄词开头，不值得一次研究
        if len(topic) < 4 and not any(ch.isascii() for ch in topic):
            return {"gate_reason": "输入过短", "is_research": False}
        hist = ""
        if chats is not None:
            turns = chats.load(state.get("session_id", "web"))[-3:]
            if turns:
                recent = "\n".join(
                    f"用户：{t['user'][:80]}\n助手：{t['assistant'][:80]}" for t in turns
                )
                hist = f"最近对话（供参考）：\n{recent}\n\n"
        judgement = structured(llm, IntentJudgement).invoke(
            f"{hist}用户输入：{topic}\n\n"
            "判断它是否是一个值得联网搜索调研的正式研究主题（行业/产品/技术/事件/数据等）。"
            "日常寒暄、闲聊、求助对话、无法定义研究范围的话，判为非研究。"
        )
        return {"gate_reason": judgement.reason, "is_research": judgement.is_research}

    return gate


QUERY_HINTS = ("查", "搜", "找", "推荐", "新闻", "热点", "最近", "了解", "看看")


def _looks_like_query(text: str) -> bool:
    """规则判定：输入是否带查询意图（命中才值得在闲聊分支花一次联网搜索）。"""
    return any(k in text for k in QUERY_HINTS)


def make_smalltalk(llm: ChatOpenAI, chats=None, settings: Settings | None = None):
    """闲聊直答节点：不走研究流水线，带上会话历史对话（有记忆的金鱼）。

    先搜后答：输入带查询意图（"帮我查一下最近很火的那个xx"）时先联网搜一轮，
    把结果塞进 prompt —— 能对上就点名附来源，对不上再反问澄清。
    纯寒暄（"你好"）不搜，照旧零额度。
    """
    router = None
    if settings is not None:
        from insight_agent.tools.search_router import (
            BochaProvider,
            DuckDuckGoProvider,
            SearchRouter,
            TavilyProvider,
        )

        providers = [TavilyProvider(settings.tavily_api_key)]
        if settings.bocha_api_key:
            providers.append(BochaProvider(settings.bocha_api_key))
        providers.append(DuckDuckGoProvider())
        router = SearchRouter(providers)

    def smalltalk(state: dict) -> dict:
        sid = state.get("session_id", "web")
        topic = state["topic"]
        # 先搜后答：查询意图 → 联网一轮；搜索失败退回纯记忆澄清（辅助能力不拦主流程）
        search_block, searched = "", False
        if router is not None and _looks_like_query(topic):
            try:
                batch = router.search(topic, max_results=5)
                if batch.results:
                    searched = True
                    lines = [f"- {r.title}：{r.snippet}（{r.url}）" for r in batch.results]
                    search_block = "\n\n刚联网搜到的参考结果：\n" + "\n".join(lines)
            except Exception:  # noqa: BLE001
                search_block, searched = "", False

        system = (
            "你是 Insight Agent 助手。用户没有提出正式研究请求，请友好简短地回复。\n"
            f"当前日期：{_today()}。问今天日期/星期几时按这个日期回答，不要凭记忆猜。\n"
        )
        if searched:
            system += (
                "用户的话带着查询意图但对象可能模糊。优先依据刚搜到的参考结果回答："
                "能对上的直接点名介绍并附来源链接 [标题](url)；"
                "对不上就列出候选并反问澄清，不要凭记忆硬猜。"
            )
        else:
            system += (
                "如果用户似乎想查什么但说不清，给出候选并提示："
                "输入一个具体主题可以生成研究报告。"
            )

        messages = [("system", system)]
        if chats is not None:
            for t in chats.load(sid):
                messages.append(("user", t["user"]))
                messages.append(("assistant", t["assistant"]))
        messages.append(("user", f"{topic}{search_block}" if search_block else topic))
        result = llm.invoke(messages)
        if chats is not None:
            chats.append(sid, topic, str(result.content))
        return {"direct_reply": result.content, "searched": searched}

    return smalltalk


def make_gap_analyzer(llm: ChatOpenAI, settings: Settings):
    """迭代深研（UPGRADE_SPEC §10）：评估证据充分度，不足则再派一轮研究。

    仅 deep 档启用；MAX_RESEARCH_ROUNDS 是代码不变量，防止长周期失控。
    档位按请求读 state.depth（分级交付），构建期 settings 只作兜底。
    """

    def gap_analyzer(state: dict) -> dict:
        depth = state.get("depth") or settings.research_depth
        max_rounds = 3 if depth == "deep" else 1
        rounds = state.get("rounds", 1)
        if depth != "deep" or rounds >= max_rounds:
            return {"rounds": rounds, "brief": []}
        judgement = structured(llm, GapJudgement).invoke(
            f"研究主题：{state['topic']}\n"
            f"研究提纲：{json.dumps(state.get('brief', []), ensure_ascii=False)}\n"
            f"已收集证据（节选）：\n{state.get('compressed_findings', '')[:4000]}\n\n"
            "判断证据是否足以写出高质量报告；不足则列出至多 3 个需要补充的具体子问题。"
        )
        if judgement.sufficient or not judgement.missing_aspects:
            return {"rounds": rounds, "brief": []}
        return {"rounds": rounds + 1, "brief": judgement.missing_aspects[:3]}

    return gap_analyzer


def make_recall(store: NotesStore, card_store=None):
    """双通道记忆（规格 §9.3）：主题档案（精确） + 事实卡片（跨主题语义）。"""

    def recall(state: dict) -> dict:
        fact_cards: list[str] = []
        if card_store is not None:
            cards = card_store.search(state["topic"], top_k=5)
            fact_cards = [
                f"{c.claim}（[来源]({c.source_url})，置信 {c.confidence}）" for c in cards
            ]
        return {
            "existing_notes": store.load_notes(state["topic"]),
            "existing_digest": store.load_digest(state["topic"]),
            "fact_cards": fact_cards,
        }

    return recall


def make_planner(llm: ChatOpenAI):
    structured_llm = structured(llm, BriefValidator)

    def planner(state: dict) -> dict:
        # 分级交付：用户点选"深入"时聚焦单问题，跳过拆解（省一次调用，且精确响应点击）
        focus = state.get("focus", "")
        if focus:
            return {"brief": [focus]}
        digest = state.get("existing_digest", "")
        cards = state.get("fact_cards", [])
        card_hint = (
            "\n相关历史事实（跨主题语义记忆召回，注意与提纲去重）：\n"
            + "\n".join(f"- {c}" for c in cards)
            if cards
            else ""
        )
        base_prompt = (
            f"当前日期：{_today()}。涉及时间范围的子问题以这个日期为基准"
            "（如\"近一年\"\"今年以来\"），不要沿用你训练数据里的旧年份。\n"
            f"研究主题：{state['topic']}\n"
            + (
                f"\n已有研究档案的目录（覆盖度地图）：\n{digest}\n"
                "对照目录判断哪些方面尚未覆盖，只规划真正的增量子问题；"
                "若主要方面已覆盖，返回空数组。\n"
                if digest
                else ""
            )
            + card_hint
            + "拆解成 3-5 个具体、可搜索的子问题，覆盖现状、关键玩家、近期动态、风险。\n"
            # 形状要求写进提示：端点不支持 response_format 时（见 llm_factory.structured）
            # JSON 约束就只剩这一句，BriefValidator 再兜住模型改键名/给子问题配结构。
            + _JSON_SHAPE_HINT
        )
        # 模型偶发不守"至少 3 条"的规矩 → 带反馈重试一次（代码拥有不变量）
        feedback = ""
        brief = None
        for _ in range(2):
            brief = structured_llm.invoke(base_prompt + feedback)
            if len(brief.sub_questions) >= 3:
                return {"brief": brief.sub_questions}
            feedback = f"\n（注意：你上次只给出 {len(brief.sub_questions)} 条子问题，必须至少 3 条；若主题已充分覆盖则可以为 0 条，但不能是 1-2 条。）"
        return {"brief": brief.sub_questions}

    return planner


DEPTH_MAX_DOCS = {"fast": 0, "standard": 2, "deep": 5}  # 每子问题深读篇数（规格 §1.6）


def make_research_one(llm: ChatOpenAI, settings: Settings, pool: EvidencePool):
    """单问题研究员：并行 fan-out 的"工人"（规格 §1.4 七步流程）。

    深读产出的 chunk 写入**共享证据池**（verify 节点要用它逐句核对）。
    fast 档：只用搜索摘要（不深读，无 degraded 标记——这是档位的本意）。
    standard/deep 档：搜索 → LLM 选 URL → 并行深读 → 语义检索相关段落 → 成文；
    深读全败时降级回摘要模式并打 ⚠degraded 标记。
    档位按请求读 state.depth（分级交付：概览 fast、深入 standard）。
    """
    from insight_agent.tools.search_router import (
        BochaProvider,
        DuckDuckGoProvider,
        SearchRouter,
        TavilyProvider,
    )

    providers = [TavilyProvider(settings.tavily_api_key)]
    if settings.bocha_api_key:
        providers.append(BochaProvider(settings.bocha_api_key))
    providers.append(DuckDuckGoProvider())
    router = SearchRouter(providers)
    blocked = {d.strip() for d in settings.fetch_block_domains.split(",") if d.strip()}

    def _search(query: str) -> list[dict]:
        batch = router.search(query, max_results=8)
        return [
            {"title": r.title, "url": r.url, "snippet": r.snippet}
            for r in batch.results
        ]

    def research_one(state: dict) -> dict:
        depth = state.get("depth") or settings.research_depth
        max_docs = DEPTH_MAX_DOCS[depth]
        question = state["question"]
        snippets = _search(question)
        if not snippets:
            return {"findings": [f"### 子问题：{question}\n\n⚠degraded：搜索源全部失败，未取得证据。"]}

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
            if depth == "fast":
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


def make_writer(llm: ChatOpenAI, revision_notes: str | None = None):
    """revision_notes 非空时注入"上一轮审查意见"（redteam 修订闭环，REDTEAM_SPEC §4）：
    构建期传固定值供 eval/单测驱动修订路径；graph 运行时从 state.redteam 的 issues 现算。
    只修订被质疑的段落，保留引用格式——不做全文重写（省额度且避免引入新问题）。
    """

    def _revision_from_state(state: dict) -> str:
        rt = state.get("redteam") or {}
        if rt.get("verdict") != "revise" or not rt.get("issues"):
            return ""
        return "\n".join(
            f"- [{i['dimension']}/{i['severity']}] 「{i['quote']}」：{i['reason']}"
            for i in rt["issues"]
        )

    def writer(state: dict) -> dict:
        existing = state.get("existing_notes", "")
        fresh = state.get("compressed_findings", "")
        evidence = (
            f"{existing}\n\n## 本轮新证据\n\n{fresh}" if existing else fresh
        )
        notes = revision_notes if revision_notes is not None else _revision_from_state(state)
        revision_block = ""
        if notes:
            revision_block = (
                "\n\n上一轮对抗审查意见（必须逐条处理）：\n"
                f"{notes}\n"
                "修订要求：只修订被质疑的段落，其余内容原样保留；"
                "保留 [来源N](url) 引用格式，不得为平息质疑删除引用。"
            )
        result = llm.invoke(
            [
                ("system", WRITER_SYSTEM),
                (
                    "user",
                    f"主题：{state['topic']}\n当前日期：{_today()}\n\n证据笔记：\n{evidence}{revision_block}",
                ),
            ]
        )
        return {"report": result.content}

    return writer


def make_archive(store: NotesStore, reports_dir: str | Path = "data/reports"):
    def archive(state: dict) -> dict:
        report = state.get("report", "")
        annotated = state.get("annotated_report", report)
        verification = state.get("verification", {})
        store.merge(state["topic"], "\n\n".join(state.get("findings", [])), report=report or None)
        # 双版本落盘：clean 给用户，annotated（含 ⚠️ 标记与幻觉率）供核查
        if report:
            rdir = Path(reports_dir)
            rdir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            (rdir / f"{ts}.md").write_text(f"# {state['topic']}\n\n{report}", encoding="utf-8")
            hr = verification.get("hallucination_rate")
            score = verification.get("score")
            hr_s = f"{hr:.1%}" if isinstance(hr, (int, float)) else "—"
            sc_s = str(score) if score is not None else "—"
            head = (
                f"# {state['topic']}（标注版）\n\n"
                f"- 幻觉率：{hr_s}\n- 可信度评分：{sc_s}\n\n"
            )
            (rdir / f"{ts}_annotated.md").write_text(head + annotated, encoding="utf-8")
        return {"saved": True}

    return archive
