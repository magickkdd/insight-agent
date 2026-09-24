"""配置加载：LLM 服务 + Tavily 搜索。缺项启动即炸（fail-fast）。"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    tavily_api_key: str
    # ---- 研究深度与成本档位（规格 §1.6）----
    research_depth: str = "standard"      # fast | standard | deep
    fetch_timeout: float = 15.0
    fetch_max_concurrency: int = 4
    embedding_provider: str = "auto"      # api | local | keyword | auto
    embedding_model: str = "text-embedding-3-small"
    fetch_block_domains: str = ""         # 逗号分隔黑名单


def load_settings(env_path: str = ".env") -> Settings:
    load_dotenv(env_path)
    required = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL_ID", "TAVILY_API_KEY")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"缺少配置项: {', '.join(missing)}（检查 {env_path}）")
    depth = os.environ.get("RESEARCH_DEPTH", "standard")
    if depth not in ("fast", "standard", "deep"):
        raise RuntimeError(f"RESEARCH_DEPTH 非法: {depth}（fast/standard/deep）")
    return Settings(
        llm_base_url=os.environ["LLM_BASE_URL"],
        llm_api_key=os.environ["LLM_API_KEY"],
        llm_model=os.environ["LLM_MODEL_ID"],
        tavily_api_key=os.environ["TAVILY_API_KEY"],
        research_depth=depth,
        fetch_timeout=float(os.environ.get("FETCH_TIMEOUT", "15")),
        fetch_max_concurrency=int(os.environ.get("FETCH_MAX_CONCURRENCY", "4")),
        embedding_provider=os.environ.get("EMBEDDING_PROVIDER", "auto"),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
        fetch_block_domains=os.environ.get("FETCH_BLOCK_DOMAINS", ""),
    )
