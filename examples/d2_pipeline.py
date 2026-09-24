"""D2：研究流水线端到端。

运行: uv run python examples/d2_pipeline.py
"""

from langchain_openai import ChatOpenAI

from insight_agent.config import load_settings
from insight_agent.graph.build import build_research_graph


def main() -> None:
    s = load_settings()
    llm = ChatOpenAI(
        model=s.llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0,
    )
    graph = build_research_graph(llm)

    topic = "国内 AI Agent 开发框架的最新格局与趋势"
    print(f"=== 主题：{topic} ===\n")

    result = graph.invoke({"topic": topic})

    print("=== 研究提纲（planner）===")
    for i, q in enumerate(result["brief"], 1):
        print(f"{i}. {q}")

    print(f"\n=== 证据笔记（researcher，{len(result['findings'])} 字）===")
    print(result["findings"][:500] + "\n……")

    print("\n=== 最终报告（writer）===")
    print(result["report"])


if __name__ == "__main__":
    main()
