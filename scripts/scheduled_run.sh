#!/usr/bin/env bash
# 定时调度的推荐入口（SCHEDULING_SPEC §6 S10）。
#
# 为什么是脚本而不是让 agent 自带一个调度循环：cron、Windows 计划任务、
# GitHub Actions 都是现成的，自带一个循环等于让用户同时维护两套调度。
# 真正缺的是「被调起来之后那些运维接口」—— 那是 CLI 那一层的事。
#
# 用法：
#   bash scripts/scheduled_run.sh "研究主题"              # 默认 standard 档
#   bash scripts/scheduled_run.sh "研究主题" --depth deep
#   crontab -e 里写：
#     30 8 * * 1-5 cd /path/to/insight-agent && bash scripts/scheduled_run.sh "行业周报" >> /var/log/ia.log 2>&1
#
# 退出码与 `insight_agent/exits.py` 一致，脚本按「该不该重试」分支：
#   0 完成 · 1 验证层异常 · 2 用法错 · 3 限流 · 4 上游不可用 · 5 内部异常 · 130 中断
#
# 为什么不自动重试：重试策略属于部署决策（额度窗口有多长、允不允许重复消费）。
# 这里只把「该不该重试」这个判断做对并交给调用方，决定权留在 crontab 那一行。

set -uo pipefail

TOPIC="${1:-}"
if [[ -z "$TOPIC" ]]; then
  echo "用法: bash scripts/scheduled_run.sh <主题> [--depth fast|standard|deep] [--out 路径]" >&2
  exit 2
fi
shift

# 仓库根 = 本脚本的上两级（scripts/ 的父目录）
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 5

# .env 在仓库根，但子进程按 cwd 找配置 —— cd 之后才找得到
if [[ ! -f .env ]]; then
  echo "缺少 .env（缺项启动即炸是有意的 fail-fast）：$ROOT/.env" >&2
  exit 2
fi

START=$(date +%s)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 开始：$TOPIC $*"

if command -v uv >/dev/null 2>&1; then
  uv run --frozen python -m insight_agent.cli research "$TOPIC" "$@"
  CODE=$?
else
  # 没装 uv 就退回项目自己的 venv；两个都没有的话跑不了，如实说
  if [[ -x .venv/bin/python ]]; then
    .venv/bin/python -m insight_agent.cli research "$TOPIC" "$@"
    CODE=$?
  elif [[ -x .venv/Scripts/python.exe ]]; then
    .venv/Scripts/python.exe -m insight_agent.cli research "$TOPIC" "$@"
    CODE=$?
  else
    echo "找不到 uv 或项目 .venv，无法启动" >&2
    exit 5
  fi
fi

ELAPSED=$(( $(date +%s) - START ))

# 退出码分诊：这一段是整个脚本的重点 —— 它把「该不该退避重试」写成可读的分支，
# 而不是让读 cron 日志的人自己去查码表。
case "$CODE" in
  0)
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 完成，用时 ${ELAPSED}s"
    ;;
  3|4)
    # 外部依赖的问题：额度窗口或服务端抖动，退避后重试有意义
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 外部依赖不可用（code=$CODE），建议退避后重试" >&2
    ;;
  1)
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 验证层异常（code=1），重试无用，看 data/runs/ 的台账" >&2
    ;;
  2)
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 用法或配置错（code=2），改命令或 .env" >&2
    ;;
  *)
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 内部异常（code=$CODE），别重试，看 data/runs/*.log" >&2
    ;;
esac

echo "台账：ls -t data/runs/*.json | head -1"
exit "$CODE"