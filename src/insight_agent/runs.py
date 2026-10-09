"""运行台账：一次 `research` 落一个 JSON + 一个 log，定时调度才有事后可查。

**为什么需要。** 定时跑三次失败了，终端输出已经回滚，你不知道哪一次、
跑到哪个节点、烧了多少 token。评测层 `eval/runner.py` 早就把这套做对了
一半（`error_kind` 区分 rate_limit / exception、`CaseResult` 带 tokens 与
latency），这里是把它搬到真实运行路径上，不是发明新机制。

**为什么 JSON 与 log 分开写。** 读者不同：台账给脚本读（要能被 `jq` 查），
日志给人读（要能顺着时间线看）。把结构化数据塞进日志行，脚本就得写正则；
把日志塞进 JSON，人读就得 `jq`。

**键必须都有写入点。** `RunRecord.to_dict()` 里出现的每个键，都要有至少
一处代码给它赋值 —— `tests/test_runs.py::test_every_record_field_has_a_writer`
把这条钉住。同一个套路来自 `mini-claude-code` 的 `test_metrics_have_producers`：
报表里的数字如果只有默认值没有写入点，那它不是"没发生"而是"没人记"。

**为什么 run_id 不是 UUID。** 这个文件名是要被人 `ls` 出来看的：
`<UTC时间戳>-<主题slug>-<随机6位>`，一眼看到哪天跑的哪的主题。纯时间戳
在同秒并发下会撞，纯随机认不出是什么。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from insight_agent.exits import ExitKind

SCHEMA_VERSION = "1.0"

#: 台账与日志都不该出现的东西。跟 mini-claude-code 的 `_SECRET_LIKE` 同思路，
#: 但本地实现 —— 跨仓 import 一个实习项目会把两边焊死。
_SECRET_LIKE = re.compile(
    r"(sk|pk|key|token|secret|bearer|authorization)[-_][A-Za-z0-9\-_]{8,}",
    re.IGNORECASE,
)
#: `Authorization: Bearer <token>` —— 这是最常漏的一种形态：第一条正则要求
#: 前缀后面紧跟 `-` 或 `_`，而 `Bearer` 后面是空格，token 就原样留在了日志里。
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+\S+")
#: `KEY=值` 形态。左边**不锚定词首**：`\b` 在 `_` 后面不成立，所以
#: `TAVILY_API_KEY=xxx` 这类带前缀的环境变量名会整条绕过脱敏 —— 而这正是
#: `.env` 里真实存在的写法。改成 `[A-Za-z0-9_]*` 前缀通吃。
_ASSIGN_LIKE = re.compile(
    r"(?i)([A-Za-z0-9_]*(?:api[_-]?key|authorization|password|secret|access[_-]?key|token))"
    r"\s*[:=]\s*\S+"
)

#: 报告正文不进台账。完整 traceback 在 log 里 —— 台账是给脚本读的，
#: 放正文会让 `data/runs/` 迅速变大，而且异常消息里可能带 URL query 里的 token。
_ERROR_SUMMARY_CHARS = 200


def redact(text: str) -> str:
    """把像密钥的东西换成 `***`。日志与台账共用这一个入口。

    顺序有讲究：先吃掉 `Bearer xxx` 这种「前缀 + 空格 + 凭据」，再吃
    `KEY=值`。反过来做的话，`authorization: Bearer xxxx` 会先被赋值规则
    砍成 `authorization=***`，`Bearer` 反而留在输出里 —— 词还在、值没了，
    看起来像脱敏过了，其实等于没脱。
    """
    cleaned = _BEARER.sub("Bearer ***", text)
    cleaned = _SECRET_LIKE.sub("***", cleaned)
    return _ASSIGN_LIKE.sub(lambda m: f"{m.group(1)}=***", cleaned)


def _slug(topic: str) -> str:
    """主题 → 短 slug，给 run_id 用。非 ASCII 会被压成短哈希。

    主题可能是中文，而 run_id 会进文件名与日志首行 —— 一段转义过的中文
    在 Windows 上是合法文件名，但丑且脆（不同代码页行为不一）。
    """
    ascii_like = re.sub(r"[^a-zA-Z0-9]+", "-", topic).strip("-").lower()
    if ascii_like:
        return ascii_like[:8]
    digest = f"{abs(hash(topic)):x}"[:8]
    return f"topic{digest[:5]}"


def new_run_id(topic: str) -> str:
    """`<UTC时间戳>-<主题slug>-<随机6位>`。同秒不同 run 靠随机段分开。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{_slug(topic)}-{secrets.token_hex(3)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunRecord:
    """一次运行的台账。字段与 SPEC §4.1 一一对应。"""

    run_id: str
    topic: str
    depth: str
    started_at: str = ""
    ended_at: str = ""
    elapsed_s: float = 0.0
    termination: str = ExitKind.COMPLETED.name.lower()
    exit_code: int = int(ExitKind.COMPLETED)
    error_kind: str | None = None
    error_message: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None
    cost_note: str = "未计量（运行未走到 token 计量）"
    verification: dict[str, Any] = field(default_factory=dict)
    redteam: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str | None] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "topic": self.topic,
            "depth": self.depth,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_s": round(self.elapsed_s, 1),
            "termination": self.termination,
            "exit_code": self.exit_code,
            "error_kind": self.error_kind,
            "error_message": self.error_message,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "cost_note": self.cost_note,
            "verification": self.verification,
            "redteam": self.redteam,
            "artifacts": self.artifacts,
        }


class RunLedger:
    """一次 `research` 的生命周期：进入时开文件与日志，退出时收口写台账。

    用法就是上下文管理器：

        with RunLedger(topic="x", depth="standard") as ledger:
            ledger.graph.invoke(...)
            ledger.set_tokens(tokens)
            ledger.finish(ExitKind.COMPLETED)

    **异常路径也要写出台账**（判据 S6）。定时任务最需要的信息恰恰出现在
    失败时，只在成功时写台账等于失败那次没有任何记录。
    """

    def __init__(
        self,
        *,
        topic: str,
        depth: str,
        log_dir: str | Path = "data/runs",
        price_per_mtok: float = 0.0,
    ) -> None:
        self.record = RunRecord(run_id=new_run_id(topic), topic=topic, depth=depth)
        self.record.started_at = _now_iso()
        self.dir = Path(log_dir)
        self.price_per_mtok = price_per_mtok
        self._t0 = time.perf_counter()
        self._logger: logging.Logger | None = None
        self._finished = False

    # ------------------------------------------------------------------ 日志

    @property
    def logger(self) -> logging.Logger:
        """按 run_id 落一份日志。没有它定时跑失败时只剩终端回滚。"""
        if self._logger is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            logger = logging.getLogger(f"insight_agent.run.{self.record.run_id}")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = logging.FileHandler(
                self.log_path, encoding="utf-8", delay=True
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            self._logger = logger
        return self._logger

    @property
    def log_path(self) -> Path:
        return self.dir / f"{self.record.run_id}.log"

    @property
    def ledger_path(self) -> Path:
        return self.dir / f"{self.record.run_id}.json"

    def log(self, message: str, level: int = logging.INFO) -> None:
        self.logger.log(level, redact(message))

    # ------------------------------------------------------------------ 字段

    def set_tokens(self, tokens: dict[str, int]) -> None:
        """token 口径与评测层同源（`eval.metrics.summarize_tokens`），判据 S4。

        顺手算钱 —— **只在配了单价时算**（SPEC §3 D4）。各家各时各模型的
        单价差着数量级，代码里写死会产出"看起来精确、实际没人敢用"的成本
        数字。没配就留 `cost_usd: null` 并在 `cost_note` 里说明原因，
        而不是悄悄给个 0 当成本。

        运行崩在 invoke 里时压根到不了这里，此时 `cost_note` 为空而
        `cost_usd` 也是 None —— 读台账的人要能分清"没烧钱"和"没走到计量那步"，
        所以 `cost_note` 一开始就写明"未计量"，不是空串。
        """
        self.record.tokens = dict(tokens or {})
        total = self.record.tokens.get("total_tokens", 0)
        if self.price_per_mtok > 0:
            self.record.cost_usd = round(total / 1_000_000 * self.price_per_mtok, 6)
            self.record.cost_note = ""
        else:
            self.record.cost_usd = None
            self.record.cost_note = "未配置 LLM_PRICE_PER_MTOK，只记 token"

    def set_verification(self, verification: dict[str, Any]) -> None:
        """只留四个可判定的数，claims 正文不进台账。

        键名与 `graph/verify.py::VerificationReport.to_dict()` 对齐（判据 S1/S3）：
        `hallucination_rate` / `coverage_rate` / `score` / `skipped`。
        `claims` 整段不进台账 —— 它带着每条论断的原文，台账是给脚本读的文件，
        正文明细在 `data/reports/*_annotated.md` 里。
        """
        self.record.verification = {
            "hallucination_rate": verification.get("hallucination_rate"),
            "coverage_rate": verification.get("coverage_rate"),
            "score": verification.get("score"),
            "skipped": bool(verification.get("skipped", False)),
        }

    def set_redteam(self, redteam: dict[str, Any] | None) -> None:
        self.record.redteam = {
            "verdict": (redteam or {}).get("verdict"),
            "action": (redteam or {}).get("action"),
            "issues": len((redteam or {}).get("issues") or []),
        }

    def set_artifacts(self, **paths: str | None) -> None:
        self.record.artifacts.update(paths)

    # ------------------------------------------------------------------ 收口

    def finish(self, kind: ExitKind, *, error: BaseException | None = None) -> None:
        """写台账并关日志。幂等：重复调用只生效第一次。

        `error_message` 只留前 N 个字符（去敏后）。完整 traceback 在 log 里 ——
        异常消息里可能带 URL 的 query token，而台账是给脚本读的文件。
        """
        if self._finished:
            return
        self._finished = True

        self.record.ended_at = _now_iso()
        self.record.elapsed_s = time.perf_counter() - self._t0
        self.record.termination = kind.name.lower()
        self.record.exit_code = int(kind)

        if error is not None:
            self.record.error_kind = type(error).__name__
            self.record.error_message = redact(str(error))[:_ERROR_SUMMARY_CHARS]
            self.log(f"terminated: {self.record.termination} ({self.record.error_kind})",
                     logging.ERROR)
        else:
            self.log(f"terminated: {self.record.termination} in {self.record.elapsed_s:.1f}s")

        self.dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(
            json.dumps(self.record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if self._logger is not None:
            self._logger.info("run_id=%s ledger=%s", self.record.run_id, self.ledger_path)
            for handler in list(self._logger.handlers):
                handler.close()
                self._logger.removeHandler(handler)