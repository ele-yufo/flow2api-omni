"""OpenAI-compatible response / SSE chunk formatting.

Pure formatters extracted from GenerationHandler (P4). No instance state — safe to
unit-test in isolation. GenerationHandler keeps thin delegating methods so its 91
internal call sites stay unchanged.
"""
import json
import time
from typing import Optional


def create_stream_chunk(
    content: str, role: Optional[str] = None, finish_reason: Optional[str] = None
) -> str:
    """创建流式响应 chunk。"""
    chunk = {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "flow2api",
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": finish_reason
        }]
    }

    if role:
        chunk["choices"][0]["delta"]["role"] = role

    if finish_reason:
        chunk["choices"][0]["delta"]["content"] = content
    else:
        chunk["choices"][0]["delta"]["reasoning_content"] = content

    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def create_completion_response(
    content: str, media_type: str = "image", is_availability_check: bool = False
) -> str:
    """创建非流式响应。

    Args:
        content: 媒体 URL 或纯文本消息
        media_type: 媒体类型 ("image" 或 "video")
        is_availability_check: 是否为可用性检查响应 (纯文本消息)

    Returns:
        JSON 格式的响应
    """
    # 可用性检查: 返回纯文本消息
    if is_availability_check:
        formatted_content = content
    else:
        # 媒体生成: 根据媒体类型格式化内容为 Markdown
        if media_type == "video":
            formatted_content = f"```html\n<video src='{content}' controls></video>\n```"
        else:  # image
            formatted_content = f"![Generated Image]({content})"

    response = {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "flow2api",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": formatted_content
            },
            "finish_reason": "stop"
        }]
    }

    return json.dumps(response, ensure_ascii=False)


def create_error_response(error_message: str, status_code: int = 500) -> str:
    """创建错误响应。"""
    error = {
        "error": {
            "message": error_message,
            "type": "server_error" if status_code >= 500 else "invalid_request_error",
            "code": "generation_failed",
            "status_code": status_code,
        }
    }

    return json.dumps(error, ensure_ascii=False)
