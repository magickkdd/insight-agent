"""对抗审查层单测（REDTEAM_SPEC T1）：fake LLM 脚本驱动，零网络。

覆盖六类判据：全绿 pass / 假引用被 flag / 相互矛盾结论被 flag / 覆盖缺失被 flag /
轮数上限终止不死循环 / JSON 畸形走容错不崩；另加 quote 定位校验（防注入）、
writer 修订注入与路由决策。
"""

from insight_agent.config import Settings
from insight_agent.graph.build import route_after_redteam
from insight_agent.graph.nodes import make_writer
from insight_agent.graph.redteam import (
    Issue,
    RedTeamOut,
    RedTeamReport,
    _locate,
    make_redteam_node,
)
from insight_agent.graph.verify import ClaimsOut, VerdictOut, make_verify_node
from insight_agent.memory.evidence_pool import EvidencePool
from insight_agent.tools.chunker import chunk_text
from insight_agent.tools.embedder import KeywordEmbedder

REPORT = (
    "# 某行业报告\n\n## 核心发现\n\n"
    "该市场2026年规模达3.7亿元 [来源1](https://example.com/a)。\n"
    "另一来源称该市场2026年规模下滑至2.1亿元 [来源2](https://example.com/b)。\n"
    "头部厂商份额集中度持续提升 [来源1](https://example.com/a)。\n"
)

GOOD_VERIFICATION = {
    "claims": [
        {"text": "该市场2026年规模达3.7亿元", "verdict": "supported"},
        {"text": "头部厂商份额集中度持续提升", "verdict": "supported"},
    ],
    "hallucination_rate": 0.0,
}


def _cfg(**over) -> Settings:
    kw = dict(
        llm_base_url="https://cloud.example/v1",
        llm_api_key="k",
        llm_model="m",
        tavily_api_key="t",
        redteam_enabled="true",
    )
    kw.update(over)
    return Settings(**kw)


def _pool() -> EvidencePool:
    pool = EvidencePool()
    pool.bind_embedder(KeywordEmbedder())
    pool.add_chunks(chunk_text("https://example.com/a", "市场规模 达3.7亿元 头部厂商 份额集中。"))
    pool.add_chunks(chunk_text("https://example.com/b", "规模 下滑 2.1亿元 市场萎缩。"))
    return pool


class FakeRedteamLLM:
    """脚本化审查者：with_structured_output 返回预设 issues；boom=True 模拟畸形输出。"""

    def __init__(self, issues: list[Issue] | None = None, boom: Exception | None = None):
        self.issues = issues or []
        self.boom = boom
        self.prompts: list[str] = []

    def with_structured_output(self, model):
        fake = self

        class _Runnable:
            def invoke(self, prompt: str):
                fake.prompts.append(prompt)
                if fake.boom:
                    raise fake.boom
                return RedTeamOut(issues=fake.issues)

        return _Runnable()


class FakeWriterLLM:
    """脚本化 writer：捕获收到的 messages，返回固定报告。"""

    def __init__(self, content="修订后的报告"):
        self.content = content
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(messages)
        return type("Msg", (), {"content": self.content})()


def _state(**over) -> dict:
    st = {
        "topic": "某行业",
        "report": REPORT,
        "annotated_report": REPORT,
        "depth": "deep",
        "verification": GOOD_VERIFICATION,
        "brief": ["市场规模", "竞争格局", "风险"],
        "redteam_rounds": 0,
    }
    st.update(over)
    return st


def test_issue_model_and_report_dict():
    rd = RedTeamReport(verdict="revise", issues=[
        Issue(dimension="evidence", severity="blocker", quote="x", reason="y"),
        Issue(dimension="logic", severity="minor", quote="z", reason="w"),
    ])
    d = rd.to_dict()
    assert rd.blocker_count == 1
    assert d["verdict"] == "revise" and d["blocker_count"] == 1
    assert len(d["issues"]) == 2 and d["issues"][0]["dimension"] == "evidence"


def test_all_green_passes():
    node = make_redteam_node(FakeRedteamLLM([]), _cfg(), _pool())
    out = node(_state())
    assert out["redteam"]["verdict"] == "pass"
    assert out["redteam"]["issues"] == []
    assert out["redteam"]["action"] == "review"
    assert route_after_redteam(out) == "archive"


def test_fabricated_citation_flagged_as_blocker_revises():
    issue = Issue(
        dimension="evidence", severity="blocker",
        quote="该市场2026年规模达3.7亿元",
        reason="证据池查无此数字的出处段落",
    )
    llm = FakeRedteamLLM([issue])
    node = make_redteam_node(llm, _cfg(redteam_max_rounds=2), _pool())
    out = node(_state())
    assert out["redteam"]["verdict"] == "revise"
    assert out["redteam"]["action"] == "revise"
    assert out["redteam_rounds"] == 1
    # 抽查样本确实注入了提示词（evidence 维度不是空转）
    assert "证据池实际内容" in llm.prompts[0]
    assert route_after_redteam(out) == "writer"


def test_contradictory_conclusions_flagged_as_logic():
    issue = Issue(
        dimension="logic", severity="blocker",
        quote="另一来源称该市场2026年规模下滑至2.1亿元",
        reason="与前文规模达3.7亿元的结论直接矛盾且未并列说明",
    )
    node = make_redteam_node(FakeRedteamLLM([issue]), _cfg(redteam_max_rounds=2), _pool())
    out = node(_state())
    assert out["redteam"]["verdict"] == "revise"
    assert route_after_redteam(out) == "writer"


def test_coverage_gap_minor_passes_but_issues_kept():
    issue = Issue(dimension="coverage", severity="minor", quote="头部厂商份额集中度持续提升", reason="未回答提纲中的风险子问题")
    node = make_redteam_node(FakeRedteamLLM([issue]), _cfg(), _pool())
    out = node(_state())
    # 仅 minor → pass（issues 照写，供 archive 留档）；判定规则不给模型自由裁量
    assert out["redteam"]["verdict"] == "pass"
    assert len(out["redteam"]["issues"]) == 1
    assert route_after_redteam(out) == "archive"


def test_default_budget_degrades_on_first_fail_without_revision():
    """REDTEAM_SPEC §3/§4：轮数预算含首轮审查，max_rounds=1（默认）时 fail 直接降级归档、不打回。"""
    issue = Issue(dimension="evidence", severity="blocker", quote="该市场2026年规模达3.7亿元", reason="编造数字")
    node = make_redteam_node(FakeRedteamLLM([issue]), _cfg(redteam_max_rounds=1), _pool())
    out = node(_state())
    assert out["redteam"]["verdict"] == "revise"  # 不粉饰成 pass
    assert out["redteam"]["action"] == "degrade"
    assert "redteam_rounds" not in out  # 一次修订都没触发
    assert "降级归档" in out["annotated_report"]
    assert route_after_redteam(out) == "archive"


def test_round_limit_terminates_with_degrade_not_pass():
    """max_rounds=2：第 1 次 fail 打回修订，第 2 轮（预算耗尽）降级归档。"""
    issue = Issue(dimension="evidence", severity="blocker", quote="该市场2026年规模达3.7亿元", reason="编造数字")
    node = make_redteam_node(FakeRedteamLLM([issue]), _cfg(redteam_max_rounds=2), _pool())
    # 第 1 轮：打回
    r1 = node(_state())
    assert r1["redteam"]["action"] == "revise" and r1["redteam_rounds"] == 1
    # 第 2 轮（rounds=1 已耗尽 n-1 次修订预算）：降级归档，verdict 保持 revise 不粉饰
    r2 = node(_state(redteam_rounds=1))
    assert r2["redteam"]["verdict"] == "revise"
    assert r2["redteam"]["action"] == "degrade"
    assert "redteam_rounds" not in r2  # 不再消耗轮数 → 终止
    assert "降级归档" in r2["annotated_report"]
    assert route_after_redteam(r2) == "archive"


def test_recheck_round_only_reviews_previous_issues():
    prev = [{"dimension": "evidence", "severity": "blocker", "quote": "旧引用", "reason": "编造"}]
    llm = FakeRedteamLLM([])
    node = make_redteam_node(llm, _cfg(redteam_max_rounds=2), _pool())
    out = node(_state(redteam_rounds=1, redteam={"verdict": "revise", "issues": prev}))
    assert "上一轮审查意见" in llm.prompts[0]  # 复查模式：带上一轮 issues
    assert out["redteam"]["verdict"] == "pass"


def test_malformed_json_degrades_to_pass_without_crash():
    node = make_redteam_node(FakeRedteamLLM(boom=RuntimeError("bad json")), _cfg(), _pool())
    out = node(_state())
    assert out["redteam"]["verdict"] == "pass"  # 审查失败不拦交付
    assert "RuntimeError" in out["redteam"]["error"]
    assert route_after_redteam(out) == "archive"


def test_unlocatable_quote_is_dropped_injection_defense():
    issue = Issue(dimension="logic", severity="blocker", quote="这段话报告里根本不存在", reason="注入的伪意见")
    node = make_redteam_node(FakeRedteamLLM([issue]), _cfg(), _pool())
    out = node(_state())
    # quote 无法在报告中定位 → 意见按畸形输出丢弃，无 blocker → pass
    assert out["redteam"]["verdict"] == "pass"
    assert out["redteam"]["issues"] == []


def test_locate_whitespace_tolerant():
    assert _locate("规模达3.7亿元", REPORT)
    assert _locate("规模 达 3.7 亿元", REPORT)  # 去空白后命中
    assert not _locate("不存在的句子", REPORT)
    assert not _locate("", REPORT)


def test_issues_truncated_to_max():
    many = [
        Issue(dimension="coverage", severity="minor", quote=q, reason="r")
        for q in ("规模达3.7亿元", "下滑至2.1亿元", "份额集中度持续提升")
    ]
    node = make_redteam_node(FakeRedteamLLM(many), _cfg(redteam_max_issues=2), _pool())
    out = node(_state())
    assert len(out["redteam"]["issues"]) == 2


def test_disabled_auto_skips_for_non_deep():
    node = make_redteam_node(FakeRedteamLLM([]), _cfg(redteam_enabled="auto"), _pool())
    out = node(_state(depth="standard"))
    assert out["redteam"]["skipped"] is True
    assert out["redteam"]["verdict"] == "pass"
    assert route_after_redteam(out) == "archive"


def test_writer_injects_revision_notes_from_state():
    llm = FakeWriterLLM()
    writer = make_writer(llm)
    rt = {"verdict": "revise", "issues": [
        {"dimension": "evidence", "severity": "blocker", "quote": "规模达3.7亿元", "reason": "无出处"},
    ]}
    writer(_state(redteam=rt))
    prompt = llm.calls[0][1][1]  # [user] 消息
    assert "上一轮对抗审查意见" in prompt
    assert "规模达3.7亿元" in prompt and "无出处" in prompt
    assert "只修订被质疑的段落" in prompt


def test_writer_revision_notes_param_overrides_state():
    llm = FakeWriterLLM()
    writer = make_writer(llm, revision_notes="- [logic/blocker] 「q」：前后矛盾")
    writer(_state())  # state 里没有 redteam，也应注入固定意见
    assert "前后矛盾" in llm.calls[0][1][1]


def test_writer_clean_run_has_no_revision_block():
    llm = FakeWriterLLM()
    make_writer(llm)(_state())
    assert "上一轮对抗审查意见" not in llm.calls[0][1][1]


def test_full_revision_loop_terminates():
    """修订闭环端到端（节点级）：redteam→writer→(verify 省略)→redteam，轮数上限兜底。"""
    quote = "头部厂商份额集中度持续提升"  # 修订后仍存留的段落，二轮审查才能再次定位
    issue = Issue(dimension="evidence", severity="blocker", quote=quote, reason="编造")
    rt_node = make_redteam_node(FakeRedteamLLM([issue]), _cfg(redteam_max_rounds=2), _pool())
    w_llm = FakeWriterLLM(f"# 修订后报告\n\n{quote} [来源1](https://example.com/a)。")
    writer = make_writer(w_llm)
    state = _state()
    for _ in range(10):  # 防御性上界，跑飞即测试失败
        state.update(rt_node(state))
        nxt = route_after_redteam(state)
        if nxt != "writer":
            break
        state.update(writer(state))
    else:
        raise AssertionError("修订循环未终止")
    assert nxt == "archive"
    assert state["redteam"]["verdict"] == "revise"  # 降级不粉饰
    assert "降级归档" in state["annotated_report"]
    assert w_llm.calls  # 至少走了一次修订


def test_revision_fixing_flagged_passage_passes_round_two():
    """T4 语义：修订确实移除了被质疑段落 → 二轮复查通过（quote 定位不到旧问题）。"""
    issue = Issue(dimension="evidence", severity="blocker", quote="规模达3.7亿元", reason="编造")
    rt_node = make_redteam_node(FakeRedteamLLM([issue]), _cfg(redteam_max_rounds=2), _pool())
    w_llm = FakeWriterLLM("# 修订后报告\n\n该市场规模另有权威口径。")
    writer = make_writer(w_llm)
    state = _state()
    state.update(rt_node(state))
    assert route_after_redteam(state) == "writer"
    state.update(writer(state))
    state.update(rt_node(state))
    assert route_after_redteam(state) == "archive"
    assert state["redteam"]["verdict"] == "pass"  # 旧问题已不在报告中 → 放行


class FakeVerifyLLM:
    """verify 的两段结构化输出：抽 claim → 判 verdict（supported 才会触发入卡）。"""

    def __init__(self, claims: list[str]):
        self.claims = claims

    def with_structured_output(self, model):
        outer = self

        class _R:
            def invoke(self, prompt: str):
                if model is ClaimsOut:
                    return ClaimsOut(claims=outer.claims)
                return VerdictOut(verdict="supported", reason="证据一致")

        return _R()


class FakePool:
    def search(self, query: str, top_k: int = 3):
        return [type("C", (), {"url": "https://example.com/a", "text": "市场规模 达3.7亿元"})()]


def test_verify_re_admits_no_fact_cards_on_revision_pass():
    """redteam 打回 → writer → verify 二次进入：同 claim 同 URL 会重复入卡污染召回，只在首轮准入。"""
    added: list = []
    store = type("S", (), {"add": lambda self, card: added.append(card)})()
    verify = make_verify_node(FakeVerifyLLM(["该市场2026年规模达3.7亿元"]), "true", 30, 1, FakePool(), store)
    verify(_state())
    assert len(added) == 1
    verify(_state(redteam_rounds=1))  # 修订回环的第二次 verify
    assert len(added) == 1
