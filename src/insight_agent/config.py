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


def load_settings(env_path: str = ".env") -> Settings:
    load_dotenv(env_path)
    required = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL_ID", "TAVILY_API_KEY")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"缺少配置项: {', '.join(missing)}（检查 {env_path}）")
    return Settings(
        llm_base_url=os.environ["LLM_BASE_URL"],
        llm_api_key=os.environ["LLM_API_KEY"],
        llm_model=os.environ["LLM_MODEL_ID"],
        tavily_api_key=os.environ["TAVILY_API_KEY"],
    )
