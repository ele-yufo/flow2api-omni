"""Upstream (Google Flow) response normalization (pure).

Extracted from GenerationHandler (P4). Transforms the varied upstream submit/status
response shapes (operations vs media vs workflow) into the internal shape used
downstream. Pure — golden-locked against representative response shapes.
"""
import uuid
from typing import Any, Dict, List


def poll_progress_percent(attempt: int, max_attempts: int, cap: int = 95) -> int:
    """轮询进度百分比:min(int((attempt/max_attempts)*100), cap)。

    与原 4 处内联 `min(int((x / y) * 100), 95)` 逐字等价。
    """
    return min(int((attempt / max_attempts) * 100), cap)


def extract_video_info(operation: Dict[str, Any]) -> Dict[str, Any]:
    """取 operation 里的 video 元数据子字典:operation['operation'].metadata.video。

    与原内联 `X["operation"].get("metadata", {}).get("video", {})` 逐字等价——
    保留 `["operation"]` 的 KeyError 语义(该键缺失时抛,而非静默 {})。
    """
    return operation["operation"].get("metadata", {}).get("video", {})


def is_media_generation_failed(status: Any) -> bool:
    """媒体生成终态失败判定:显式 FAILED,或任何 MEDIA_GENERATION_STATUS_ERROR* 前缀。

    None-safe。与原 3 处内联
    `x in ("MEDIA_GENERATION_STATUS_FAILED",) or (x or "").startswith("...ERROR")`
    逐字等价。
    """
    return status in ("MEDIA_GENERATION_STATUS_FAILED",) or \
        (status or "").startswith("MEDIA_GENERATION_STATUS_ERROR")


def normalize_media_id_to_uuid_str(raw_media_id: str) -> str:
    """把原始 media id 归一化为标准 UUID 字符串;不是合法 UUID 时原样返回。

    行为与原 3 处内联 `try: str(uuid.UUID(x)) except ValueError: x` 完全一致——
    只吞 ValueError,其它异常(如 x 为 None 触发的 TypeError)照旧向上传播。
    """
    try:
        return str(uuid.UUID(raw_media_id))
    except ValueError:
        return raw_media_id


def normalize_video_submit_response(result: Dict[str, Any], project_id: str) -> Dict[str, Any]:
    """Normalize old operation responses and new media/workflow responses."""
    operations = [
        operation for operation in (result.get("operations") or [])
        if isinstance(operation, dict)
        and (operation.get("operation") or {}).get("name")
    ]

    workflow_items = result.get("workflows") or []
    if not workflow_items and isinstance(result.get("workflow"), dict):
        workflow_items = [result["workflow"]]
    workflow_id = None
    if workflow_items and isinstance(workflow_items[0], dict):
        workflow_id = workflow_items[0].get("name")

    raw_media = result.get("media") or []
    if isinstance(raw_media, dict):
        raw_media = [raw_media]

    media_refs: List[Dict[str, Any]] = []
    scene_id = None
    for media in raw_media:
        if not isinstance(media, dict):
            continue
        media_name = media.get("name")
        if not media_name:
            continue
        media_project_id = media.get("projectId") or project_id
        media_refs.append({"name": media_name, "projectId": media_project_id})
        scene_id = scene_id or media.get("sceneId")
        workflow_id = workflow_id or media.get("workflowId")

    task_id = None
    if operations:
        first_operation = operations[0]
        task_id = (first_operation.get("operation") or {}).get("name")
        scene_id = first_operation.get("sceneId") or scene_id
    elif media_refs:
        task_id = media_refs[0].get("name")

    return {
        "operations": operations,
        "media": media_refs if not operations else [],
        "workflow_id": workflow_id,
        "scene_id": scene_id,
        "task_id": task_id,
    }


def coerce_media_status_to_operations(
    result: Dict[str, Any], status_refs: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Convert media-format status responses into the operation shape used downstream."""
    media_items = result.get("media") or []
    if isinstance(media_items, dict):
        media_items = [media_items]
    if not media_items:
        return []

    operations: List[Dict[str, Any]] = []
    media_refs = status_refs.get("media") or []
    fallback_scene_id = status_refs.get("scene_id")
    if media_refs and isinstance(media_refs[0], dict):
        fallback_scene_id = fallback_scene_id or media_refs[0].get("sceneId")

    for media in media_items:
        if not isinstance(media, dict):
            continue
        media_name = media.get("name")
        if not media_name:
            continue

        metadata = media.get("mediaMetadata") or {}
        media_status = metadata.get("mediaStatus") or {}
        status = media_status.get("mediaGenerationStatus")
        video_wrapper = media.get("video") or {}
        video_info = video_wrapper.get("generatedVideo") or video_wrapper
        if not status and video_info.get("fifeUrl"):
            status = "MEDIA_GENERATION_STATUS_SUCCESSFUL"

        operation_metadata = {"video": dict(video_info)} if isinstance(video_info, dict) else {}
        if "mediaGenerationId" not in operation_metadata.get("video", {}):
            operation_metadata.setdefault("video", {})["mediaGenerationId"] = media_name

        operations.append({
            "operation": {
                "name": media_name,
                "metadata": operation_metadata,
                "error": media.get("error") or media_status.get("error") or {},
            },
            "sceneId": media.get("sceneId") or fallback_scene_id,
            "mediaGenerationId": media_name,
            "status": status or "MEDIA_GENERATION_STATUS_ACTIVE",
        })

    return operations
