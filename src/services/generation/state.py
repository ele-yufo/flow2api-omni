"""Generation-request state helpers + tier-based model-key resolution (pure).

Extracted from GenerationHandler (P4). No instance state / no I/O — unit-testable.
GenerationHandler keeps thin delegating methods so its ~28 call sites stay unchanged.
"""
from typing import Any, Dict, Optional, Tuple


def create_generation_result() -> Dict[str, Any]:
    """Fresh per-request generation result accumulator."""
    return dict(success=False, error_message=None, error_emitted=False)


def create_response_state() -> Dict[str, Any]:
    """为单次请求创建独立的响应状态，避免并发请求互相污染。"""
    return {
        "url": None,
        "generated_assets": None,
        "base_url": None,
    }


def mark_generation_failed(generation_result: Optional[Dict[str, Any]], error_message: str) -> None:
    """Mark the result accumulator failed (in place)."""
    if isinstance(generation_result, dict):
        generation_result["success"] = False
        generation_result["error_message"] = error_message
        generation_result["error_emitted"] = True


def mark_generation_succeeded(generation_result: Optional[Dict[str, Any]]) -> None:
    """Mark the result accumulator succeeded (in place)."""
    if isinstance(generation_result, dict):
        generation_result["success"] = True
        generation_result["error_message"] = None
        generation_result["error_emitted"] = False


def normalize_error_message(error_message: Any, max_length: int = 1000) -> str:
    """归一化错误文本，避免写入超长内容。"""
    text = str(error_message or "").strip() or "未知错误"
    if len(text) <= max_length:
        return text
    return f"{text[:max_length - 3]}..."


def resolve_video_model_key_for_tier(
    model_config: Dict[str, Any], user_tier: str
) -> Tuple[str, Optional[str]]:
    """根据账号层级调整视频模型 key。返回 (model_key, 可选的说明日志)。"""
    model_key = model_config["model_key"]
    allow_tier_upgrade = bool(model_config.get("allow_tier_upgrade", True))

    if user_tier == "PAYGATE_TIER_TWO":
        if allow_tier_upgrade and "ultra" not in model_key:
            if "_fl" in model_key:
                model_key = model_key.replace("_fl", "_ultra_fl")
            else:
                model_key = model_key + "_ultra"
            return model_key, f"TIER_TWO 账号自动切换到 ultra 模型: {model_key}"
        return model_key, None

    if user_tier == "PAYGATE_TIER_ONE" and "ultra" in model_key:
        model_key = model_key.replace("_ultra_fl", "_fl").replace("_ultra", "")
        return model_key, f"TIER_ONE 账号自动切换到标准模型: {model_key}"

    return model_key, None
