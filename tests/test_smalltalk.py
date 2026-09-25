"""smalltalk 先搜后答的意图判定单测：查询词命中才联网，纯寒暄不花额度。"""

from insight_agent.graph.nodes import _looks_like_query


def test_query_intent_hits():
    assert _looks_like_query("帮我查一下最近很火的那个分类模型")
    assert _looks_like_query("搜一下Agent框架对比")
    assert _looks_like_query("最近有什么值得关注的新闻")
    assert _looks_like_query("推荐几个MCP server")


def test_plain_chitchat_no_search():
    assert not _looks_like_query("你好")
    assert not _looks_like_query("我叫小明")
    assert not _looks_like_query("再见")
    assert not _looks_like_query("谢谢你")
