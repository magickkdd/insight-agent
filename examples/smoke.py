"""D1 冒烟：官方预制 create_agent + TavilySearch，端到端验证技术栈。

注意：create_react_agent 已弃用（LangGraph 1.0 起），当前官方主推
`from langchain.agents import create_agent` —— 求职作品跟主线走。

运行: uv run python examples/smoke.py
"""

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_tavily import TavilySearch

from insight_agent.config import load_settings


def main() -> None:
    s = load_settings()
    llm = ChatOpenAI(
        model=s.llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0,
    )
    search = TavilySearch(max_results=3)
    agent = create_agent(
        llm,
        tools=[search],
        system_prompt="你是一个严谨的研究助手。凡是涉及时效性/事实性的问题，"
        "必须先调用搜索工具获取证据再回答，不允许凭记忆回答。",
    )

    # 时效性问题：训练数据里没有可靠答案，强制走搜索
    result = agent.invoke(
        {"messages": [("user", "搜索一下 LangGraph 最近一次大版本更新是什么，给出处链接")]}
    )
    print("=== 最终回答 ===")
    print(result["messages"][-1].content)
    print(f"\n=== 轨迹：共 {len(result['messages'])} 条消息 ===")
    for m in result["messages"]:
        print(f"[{m.type}] {str(m.content)[:80]}")


if __name__ == "__main__":
    main()
