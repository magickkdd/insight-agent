"""planner 的结构化输出容错：单字段 schema 会被部分模型塌陷成裸数组。

实测来源：切到 `deepseek-v4-flash-0731`（Intern InkStone 科研模型平台）后，
`ResearchBrief` 那个「只有一个字段」的 schema 稳定返回裸数组，Pydantic 报
`Input should be an object [input_value=[...], input_type=list]`。

同一个模型在两字段的 `IntentJudgement` 上是正常的 —— 所以这不是「这个模型
不支持结构化输出」，而是单字段 wrapper 容易被省掉。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from insight_agent.graph.nodes import BriefValidator, IntentJudgement, ResearchBrief


def test_a_chinese_key_is_normalised_back():
    """实测 deepseek-v4-flash-0731 返回 `{"值得研究": true}` —— 内容对、键名不照 schema。

    归一化只认白名单里的别名，认不出来的照旧报缺字段（见下面那条）。
    """
    judgement = IntentJudgement.model_validate(
        {"值得研究": True, "reason": "行业政策类主题", "多余的键": "忽略"}
    )
    assert judgement.is_research is True
    assert judgement.reason == "行业政策类主题"


def test_underscore_and_case_variants_count_as_the_same_key():
    """`isResearch` / `Is-Research` 与 `is_research` 是同一个键。"""
    assert IntentJudgement.model_validate(
        {"isResearch": False, "reason": "闲聊"}
    ).is_research is False


def test_a_chinese_key_on_the_brief_is_normalised():
    brief = BriefValidator.model_validate({"子问题": ["a", "b", "c"]})
    assert brief.sub_questions == ["a", "b", "c"]


def test_a_structured_brief_is_flattened_to_its_text():
    """实测形态：模型给每个子问题配了 id 与「维度」，把拆解表当结构化输出了。

    `{"id": 1, "维度": "现状", "子问题": "低空经济现行政策做了哪些规定？"}`
    要变成那一句正文 —— 维度是这个模型的组织方式，不是我们要的子问题。
    """
    brief = BriefValidator.model_validate(
        [
            {"id": 1, "维度": "现状", "子问题": "低空经济现行政策做了哪些规定？"},
            {"id": 2, "维度": "趋势", "子问题": "有哪些企业或案例值得注意？"},
        ]
    )
    assert brief.sub_questions == [
        "低空经济现行政策做了哪些规定？",
        "有哪些企业或案例值得注意？",
    ]


def test_the_same_flattening_happens_under_a_chinese_key():
    brief = BriefValidator.model_validate(
        {"子问题": [{"id": 1, "维度": "现状", "子问题": "政策规定有哪些？"}]}
    )
    assert brief.sub_questions == ["政策规定有哪些？"]


def test_a_dict_without_a_recognised_text_field_is_left_alone():
    """猜不出来就照实报错 —— 「挑最长的字符串」会把「维度」当成子问题。"""
    with pytest.raises(ValidationError):
        BriefValidator.model_validate([{"id": 1, "维度": "现状", "备注": "随手写的一句很长很长的说明文字"}])


def test_a_key_we_do_not_recognise_is_still_an_error():
    """白名单之外不猜 —— 猜出来的错答案比报错贵。"""
    with pytest.raises(ValidationError):
        BriefValidator.model_validate({"完全不相干的键": ["a", "b", "c"]})


def test_a_bare_list_is_unwrapped():
    """模型直接给 `["a", "b"]` —— 当成唯一字段接住。"""
    brief = BriefValidator.model_validate(["低空经济政策", "关键玩家", "近期动态"])
    assert brief.sub_questions == ["低空经济政策", "关键玩家", "近期动态"]


def test_the_normal_object_shape_still_works():
    brief = BriefValidator.model_validate({"sub_questions": ["a", "b", "c"]})
    assert brief.sub_questions == ["a", "b", "c"]


def test_an_empty_brief_is_legitimate():
    """「已充分覆盖，规划 0 条」是设计上允许的（见 planner 的 digest 分支）。"""
    assert BriefValidator.model_validate([]).sub_questions == []


@pytest.mark.parametrize(
    "junk",
    [
        {"sub_questions": "不是列表"},
        {"other_field": ["a"]},
        "完全不是 JSON 的东西",
    ],
)
def test_shapes_we_cannot_salvage_are_rejected(junk):
    """容错有边界：能认出来的接住，认不出来的照实报错。

    把所有畸形都「猜」成能跑，等于把问题推到更下游才炸 —— 那比现在这里报错更贵。
    """
    with pytest.raises(ValidationError):
        BriefValidator.model_validate(junk)


def test_the_original_schema_is_untouched():
    """容错版不能反过来改坏原schema —— 别的地方还在用它做类型标注。"""
    assert "sub_questions" in ResearchBrief.model_fields
    with pytest.raises(ValidationError):
        ResearchBrief.model_validate(["裸数组不被原 schema 接受"])