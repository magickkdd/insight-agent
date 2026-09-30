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


def with_llm_callbacks(llm: ChatOpenAI, callbacks: list) -> ChatOpenAI:
    """返回额外挂了回调的同配置副本（评测 token 计量用）。

    为什么不用 llm.bind(callbacks=[h])：langchain-core 1.x 的 ChatOpenAI.invoke 会把
    config.callbacks 和 bound kwargs 各传一次给 generate_prompt，纯 invoke 路径直接
    TypeError；而 with_structured_output 走 RunnableBinding.__getattr__ 代理回原实例，
    绑定层回调被整个丢掉（静默计成 0）。实例级 callbacks 两条路径都能覆盖。
    """
    return llm.model_copy(update={"callbacks": list(llm.callbacks or []) + list(callbacks)})


def get_judge_config(settings: Settings) -> tuple[str, str, str]:
    """judge 的 (base_url, api_key, model)：hybrid/local 走本地，cloud 走云端。"""
    if settings.llm_profile in ("local", "hybrid"):
        return settings.local_base_url, "ollama", settings.local_model
    return settings.llm_base_url, settings.llm_api_key, settings.llm_model
