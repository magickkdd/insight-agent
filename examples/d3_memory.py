"""D3：记忆效果实测 —— 同一主题跑两遍，对比冷/热成本。

运行: uv run python examples/d3_memory.py
"""

import time

from langchain_openai import ChatOpenAI

from insight_agent.config import load_settings
from insight_agent.graph.build import build_research_graph
from insight_agent.memory.notes import NotesStore

TOPIC = "国内 AI Agent 开发框架的最新格局与趋势"


def run_once(graph, label: str) -> None:
    t0 = time.time()
    result = graph.invoke({"topic": TOPIC})
    elapsed = time.time() - t0
    print(f"--- {label} ---")
    print(f"提纲条数（增量研究量）：{len(result['brief'])}")
    for q in result["brief"]:
        print(f"  · {q}")
    new_chars = sum(len(f) for f in result.get("findings", []))
    print(f"新证据长度：{new_chars} 字")
    print(f"耗时：{elapsed:.1f}s，写回笔记库：{result['saved']}\n")


def main() -> None:
    s = load_settings()
    llm = ChatOpenAI(
        model=s.llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0,
    )
    store = NotesStore()
    graph = build_research_graph(llm, notes_store=store)

    print(f"=== 主题：{TOPIC} ===\n")
    run_once(graph, "第一遍（冷启动：无档案，全量研究）")
    run_once(graph, "第二遍（热启动：有档案，只补增量）")
    print("档案文件：", list(store._root.glob("*.json")))


if __name__ == "__main__":
    main()
