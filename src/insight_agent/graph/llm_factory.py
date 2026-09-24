"""LLM 工厂（UPGRADE_SPEC §7/§8）：档位路由 + Langfuse trace 挂载。

profile 三档：
  cloud  全部角色走云端（现状）
  local  全部角色走本地 Ollama/vLLM（离线演示，JD5"本地推理"）
  hybrid 主力生成走云端，judge 走本地（省额度）

Langfuse：配置了 key 时自动挂 CallbackHandler，全链路 trace 上报。
"""

import os
from functools import lru_cache

from langchain_openai import ChatOpenAI

from insight_agent.config import Settings


def _langfuse_callbacks(settings: Settings) -> list:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return []
    from langfuse.langchain import CallbackHandler

    handler = CallbackHandler(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        mask_input=settings.langfuse_mask_content,
        mask_output=settings.langfuse_mask_content,
    )
    return [handler]


@lru_cache(maxsize=8)
def get_llm(role: str, settings: Settings) -> ChatOpenAI:
    """role: main（planner/research/writer）| judge。lru 缓存：同 role+配置复用实例。"""
    profile = settings.llm_profile
    if profile == "local" or (profile == "hybrid" and role == "judge"):
        base, key, model = settings.local_base_url, "ollama", settings.local_model
    else:
        base, key, model = settings.llm_base_url, settings.llm_api_key, settings.llm_model

    return ChatOpenAI(
        model=model,
        api_key=key,
        base_url=base,
        temperature=0,
        callbacks=_langfuse_callbacks(settings),
    )


def reset_llm_cache() -> None:
    """测试用：清空工厂缓存。"""
    get_llm.cache_clear()


def get_judge_config(settings: Settings) -> tuple[str, str, str]:
    """judge 的 (base_url, api_key, model)：hybrid/local 走本地，cloud 走云端。"""
    if settings.llm_profile in ("local", "hybrid"):
        return settings.local_base_url, "ollama", settings.local_model
    return settings.llm_base_url, settings.llm_api_key, settings.llm_model
