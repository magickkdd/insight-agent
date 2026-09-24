"""LLM 工厂单测：三档 profile 路由正确性（不发起网络调用）。"""

from insight_agent.config import Settings
from insight_agent.graph.llm_factory import (
    get_judge_config,
    get_llm,
    reset_llm_cache,
)


def _settings(profile: str) -> Settings:
    return Settings(
        llm_base_url="https://cloud.example/v1",
        llm_api_key="cloud-key",
        llm_model="cloud-model",
        tavily_api_key="t",
        llm_profile=profile,
    )


def test_cloud_profile_routes_to_cloud():
    reset_llm_cache()
    llm = get_llm("main", _settings("cloud"))
    assert llm.model_name == "cloud-model"


def test_local_profile_routes_everything_local():
    reset_llm_cache()
    llm = get_llm("main", _settings("local"))
    assert llm.model_name == "qwen3:8b"


def test_hybrid_routes_judge_to_local():
    s = _settings("hybrid")
    reset_llm_cache()
    main_llm = get_llm("main", s)
    judge_llm = get_llm("judge", s)
    assert main_llm.model_name == "cloud-model"
    assert judge_llm.model_name == "qwen3:8b"


def test_judge_config_tuple():
    reset_llm_cache()
    base, _, model = get_judge_config(_settings("hybrid"))
    assert "11434" in base and model == "qwen3:8b"
    base2, _, model2 = get_judge_config(_settings("cloud"))
    assert "cloud.example" in base2 and model2 == "cloud-model"
