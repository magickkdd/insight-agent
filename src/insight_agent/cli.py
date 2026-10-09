"""CLI 入口（UPGRADE_SPEC §6）：research / serve / eval 三命令。

Agent 作为可编程组件的接口形态：research --json 输出结构化结果供脚本消费。
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
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
    """对指定主题执行完整研究。

    每次运行落一份台账到 `data/runs/<run_id>.json` 与同名 `.log`，退出码语义
    见 `insight_agent/exits.py`（0 成功 / 3 限流 / 4 上游不可用 …）。
    """
    import os

    from langchain_core.callbacks import UsageMetadataCallbackHandler

    from insight_agent.config import load_settings
    from insight_agent.eval.metrics import summarize_tokens
    from insight_agent.exits import ExitKind, classify_exception
    from insight_agent.graph.build import build_research_graph
    from insight_agent.graph.llm_factory import get_llm
    from insight_agent.memory.notes import NotesStore
    from insight_agent.runs import RunLedger

    if depth not in ("fast", "standard", "deep"):
        typer.echo(f"非法 depth: {depth}", err=True)
        raise typer.Exit(int(ExitKind.USAGE_ERROR))
    os.environ["RESEARCH_DEPTH"] = depth
    if no_verify:
        os.environ["VERIFY_ENABLED"] = "false"

    settings = load_settings()
    ledger = RunLedger(
        topic=topic,
        depth=depth,
        log_dir=settings.log_dir,
        price_per_mtok=settings.price_per_mtok,
    )

    try:
        ledger.log(f"开始研究 depth={depth} verify={not no_verify}")
        llm = get_llm("main", settings)
        graph = build_research_graph(llm, notes_store=NotesStore(), settings=settings)

        # 计量回调挂进 config 而不是 llm.bind(callbacks=)：后者在 langchain-core 1.x
        # 下会被 with_structured_output 的代理层整个吞掉，token 静默计成 0
        # （见 graph/llm_factory.py::with_llm_callbacks 的注释）。
        handler = UsageMetadataCallbackHandler()
        config = {"callbacks": [handler]}
        result = graph.invoke({"topic": topic}, config=config)
        verification = result.get("verification", {}) or {}

        ledger.set_tokens(summarize_tokens(handler))
        ledger.set_verification(verification)
        ledger.set_redteam(result.get("redteam"))

        if verification.get("error"):
            ledger.log(f"验证层异常: {verification['error']}")
            ledger.finish(ExitKind.VERIFY_FAILED)
            raise typer.Exit(int(ExitKind.VERIFY_FAILED))

        report_path = out
        if out:
            out.write_text(result["report"], encoding="utf-8")
        ledger.set_artifacts(
            report=str(report_path) if report_path else None,
            annotated=None,
            ledger=str(ledger.ledger_path),
        )

        payload = {
            "topic": topic,
            "report": result["report"],
            "brief": result.get("brief"),
            "verification": verification,
            "latency_s": round(ledger.record.elapsed_s, 1),
            # 定时调度需要的五个字段（SCHEDULING_SPEC §2.1）：脚本靠它们判断
            # 「这次成没成」「是哪一次」「烧了多少」，不该去解析人看的报告正文。
            "run_id": ledger.record.run_id,
            "termination": ExitKind.COMPLETED.name.lower(),
            "tokens": ledger.record.tokens,
            "elapsed_s": round(ledger.record.elapsed_s, 1),
            "artifacts": ledger.record.artifacts,
        }
        ledger.log(f"完成 tokens={ledger.record.tokens}")
        ledger.finish(ExitKind.COMPLETED)

        if json_out:
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if out:
                typer.echo(f"已写入 {out}")
            else:
                typer.echo(result["report"])
    except typer.Exit:
        raise
    except (KeyboardInterrupt, Exception) as exc:  # noqa: BLE001 - 收口见 SPEC §3 D6
        kind = classify_exception(exc)
        ledger.log(f"异常收口: {kind.name} / {type(exc).__name__}", logging.ERROR)
        ledger.log(traceback.format_exc(), logging.ERROR)
        ledger.finish(kind, error=exc)
        typer.echo(f"运行失败（{kind.name}）：{exc}", err=True)
        raise typer.Exit(int(kind)) from exc


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
