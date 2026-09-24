# Insight Agent 必须在容器内可复现：锁定依赖由 uv.lock 保证，
# 多阶段构建减小镜像；运行时只暴露 8000 端口。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
ENV UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY web ./web
COPY eval ./eval
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "insight_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
