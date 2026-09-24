"""CLI 入口（UPGRADE_SPEC §6）：research / serve / eval 三命令。

Agent 作为可编程组件的接口形态：research --json 输出结构化结果供脚本消费。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import typer

app = typer.Typer(help="Insight Agent · 行业洞察研究助手", no_args_is_help=True)


@app.command()
def research(
    topic: str = typer.Argument(..., help="研究主题"),
    depth: str = typer.Option("standard", "--depth", help="fast | standard | deep"),
    out: Path = typer.Option(None, "--out", help="报告输出路径（默认打印到终端）"),
    json_out: bool = typer.Option(False, "--json", help="输出结构化 JSON（report+verification+metrics）"),
    no_verify: bool = typer.Option(False, "--no-verify", help="跳过验证层（省额度）"),
):
    """对指定主题执行完整研究。"""
    import os

    from insight_agent.config import load_settings
    from insight_agent.graph.build import build_research_graph
    from insight_agent.memory.notes import NotesStore
    from langchain_openai import ChatOpenAI

    if depth not in ("fast", "standard", "deep"):
        typer.echo(f"非法 depth: {depth}", err=True)
        raise typer.Exit(2)
    os.environ["RESEARCH_DEPTH"] = depth
    if no_verify:
        os.environ["VERIFY_ENABLED"] = "false"

    settings = load_settings()
    llm = ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=0,
    )
    graph = build_research_graph(llm, notes_store=NotesStore(), settings=settings)

    t0 = time.perf_counter()
    result = graph.invoke({"topic": topic})
    latency = time.perf_counter() - t0
    verification = result.get("verification", {})

    if json_out:
        payload = {
            "topic": topic,
            "report": result["report"],
            "brief": result["brief"],
            "verification": verification,
            "latency_s": round(latency, 1),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = result["report"]

    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"已写入 {out}")
    else:
        typer.echo(text)
    # 验证层判定异常失败时退出码 1（供脚本判断）
    if verification.get("error"):
        raise typer.Exit(1)


@app.command()
def serve(
    port: int = typer.Option(8000, "--port"),
):
    """启动 Web 服务（SSE 可观测前端）。"""
    import uvicorn

    uvicorn.run("insight_agent.api.app:app", host="127.0.0.1", port=port)


@app.command()
def eval_cmd(
    tier: str = typer.Option(None, "--tier", help="unit | e2e"),
    limit: int = typer.Option(None, "--limit"),
):
    """转发到评测 CLI（详见 python -m insight_agent.eval --help）。"""
    from insight_agent.eval.__main__ import main as eval_main

    sys_argv = sys.argv[1:]
    forward = ["python", "-m", "insight_agent.eval"]
    if tier:
        forward += ["--tier", tier]
    if limit:
        forward += ["--limit", str(limit)]
    typer.echo(f"转发: {' '.join(forward[1:])}")
    eval_main()


if __name__ == "__main__":
    app()
