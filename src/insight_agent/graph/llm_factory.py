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


#: 端点声明「不支持结构化输出」的方式。实测 Intern InkStone科研模型平台
#: （discovery-api.intern-ai.org.cn）：传 `response_format` 时**两种都不work** ——
#: `json_schema` 与 `json_object` 都 HTTP 200 但 content 是空串，不传时同一提示
#: 正常返回 JSON。langchain 拿到空 content 后会从文本里"尽力猜"JSON，猜出来的是
#: 碎片（实测报 `input_value=2026, input_type=int`）。
#:
#: 所以这里不是换一种 `response_format`，而是**彻底不走它**：用提示词约束形状，
#: 由 `graph/nodes.py` 的 `_rename_keys` / `BriefValidator` 兜住模型改键名与配结构。
#:
#: 用环境变量而不是自动探测 —— 自动探测意味着每次启动都要烧一次请求去问端点
#: 「你支持吗」，而这个答案是配置属性不是运行时属性。
NO_STRUCTURED_OUTPUT = os.environ.get("LLM_NO_STRUCTURED_OUTPUT", "").lower() in ("1", "true", "yes")


class _PromptOnlyStructured:
    """按 schema 出结构化结果的 runnable，但不碰 `response_format`。

    做法：把 schema 的 JSON 形状写进系统提示，用 `JsonOutputParser` 解析后
    **自己构造 schema 实例** —— `JsonOutputParser(pydantic_object=...)` 只在
    `parse_with_pydantic` 时才做构造，`invoke` 返回的是裸 dict，而下游代码写的是
    `judgement.reason`（`.` 取属性）。所以这里必须补上 `validate`。

    解析或校验失败时**照实抛**，不返回空壳：一个"成功"的空结果会让下游以为模型
    什么都没查到，比直接报错难查得多。
    """

    def __init__(self, llm: ChatOpenAI, schema: type):
        from langchain_core.output_parsers import JsonOutputParser

        self._llm = llm
        self._schema = schema
        self._parser = JsonOutputParser(pydantic_object=schema)
        self._instruction = self._parser.get_format_instructions()

    def invoke(self, prompt, *args, **kwargs):
        from langchain_core.prompts import ChatPromptTemplate

        template = ChatPromptTemplate.from_messages(
            [("system", "严格按下面的格式输出，只输出 JSON，不要任何解释文字：\n{shape}"),
             ("human", "{input}")]
        )
        raw = (template | self._llm | self._parser).invoke(
            {"shape": self._instruction, "input": str(prompt)}, *args, **kwargs
        )
        return self._schema.model_validate(raw)


def structured(llm: ChatOpenAI, schema: type):
    """拿一个「按 schema 出结构化结果」的 runnable。

    默认用端点原生 `json_schema` 约束（最可靠，模型跑不掉）。开了
    `LLM_NO_STRUCTURED_OUTPUT` 则退回提示词约束 —— 形状靠提示保证、由
    `graph/nodes.py` 的容错层接住，不是靠信任模型守规矩。
    """
    if NO_STRUCTURED_OUTPUT:
        return _PromptOnlyStructured(llm, schema)
    return llm.with_structured_output(schema)


def get_judge_config(settings: Settings) -> tuple[str, str, str]:
    """judge 的 (base_url, api_key, model)：hybrid/local 走本地，cloud 走云端。"""
    if settings.llm_profile in ("local", "hybrid"):
        return settings.local_base_url, "ollama", settings.local_model
    return settings.llm_base_url, settings.llm_api_key, settings.llm_model
