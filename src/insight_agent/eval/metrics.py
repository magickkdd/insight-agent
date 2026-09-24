"""Token 计量：官方 UsageMetadataCallbackHandler 聚合（含子 agent 内部调用）。"""

from langchain_core.callbacks import UsageMetadataCallbackHandler


def attach_callback(config: dict) -> UsageMetadataCallbackHandler:
    """把计量回调挂进 invoke 的 config，返回 handler 供事后读数。"""
    handler = UsageMetadataCallbackHandler()
    config.setdefault("callbacks", []).append(handler)
    return handler


def summarize_tokens(handler: UsageMetadataCallbackHandler) -> dict[str, int]:
    """跨模型合并 token 总量。"""
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for usage in handler.usage_metadata.values():
        for k in total:
            total[k] += usage.get(k, 0)
    return total
