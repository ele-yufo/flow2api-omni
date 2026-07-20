"""Idempotent provisioning for a token's active Flow project pool."""

from typing import TYPE_CHECKING, List, Optional, Tuple

from ...core.models import Project, Token
from .project_naming import build_project_name, normalize_project_name_base

if TYPE_CHECKING:
    from ...core.database import Database
    from ..flow_client import FlowClient


MIN_POOL_SIZE = 1
MAX_POOL_SIZE = 50


def _validate_token(token: Token) -> Tuple[int, str]:
    if token is None:
        raise ValueError("token is required")

    token_id = getattr(token, "id", None)
    if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id <= 0:
        raise ValueError("token id must be a positive integer")

    st = str(getattr(token, "st", "") or "").strip()
    if not st:
        raise ValueError("token ST is required")
    return token_id, st


def _clamp_pool_size(pool_size: int) -> int:
    try:
        requested_size = int(pool_size)
    except (TypeError, ValueError) as error:
        raise ValueError("pool_size must be an integer") from error
    return max(MIN_POOL_SIZE, min(MAX_POOL_SIZE, requested_size))


def _sort_active_projects(projects: List[Project]) -> List[Project]:
    return sorted(
        (project for project in projects if project.is_active),
        key=lambda project: (project.id or 0, project.project_id),
    )


def _resolve_base_name(
    token: Token,
    projects: List[Project],
    base_name: Optional[str],
) -> str:
    if str(base_name or "").strip():
        return normalize_project_name_base(base_name)

    current_project_id = str(token.current_project_id or "").strip()
    if current_project_id:
        for project in projects:
            if project.project_id == current_project_id:
                return normalize_project_name_base(project.project_name)

    if projects:
        return normalize_project_name_base(projects[0].project_name)
    return normalize_project_name_base(token.current_project_name)


async def ensure_project_pool(
    db: "Database",
    flow_client: "FlowClient",
    token: Token,
    pool_size: int,
    base_name: Optional[str] = None,
) -> List[Project]:
    """Return a stable active pool, creating only missing persisted projects."""
    token_id, st = _validate_token(token)
    target_size = _clamp_pool_size(pool_size)
    projects = _sort_active_projects(await db.get_projects_by_token(token_id))
    resolved_base_name = _resolve_base_name(token, projects, base_name)

    while len(projects) < target_size:
        pool_index = len(projects) + 1
        project_name = build_project_name(pool_index, resolved_base_name)
        project_id = str(await flow_client.create_project(st, project_name) or "").strip()
        if not project_id:
            raise ValueError("FlowClient.create_project returned an empty project id")

        project = Project(
            project_id=project_id,
            token_id=token_id,
            project_name=project_name,
        )
        project.id = await db.add_project(project)
        projects = _sort_active_projects([*projects, project])

    resulting_pool = projects[:target_size]
    current_project_id = str(token.current_project_id or "").strip()
    if current_project_id not in {project.project_id for project in resulting_pool}:
        first_project = resulting_pool[0]
        await db.update_token(
            token_id,
            current_project_id=first_project.project_id,
            current_project_name=first_project.project_name,
        )
        token.current_project_id = first_project.project_id
        token.current_project_name = first_project.project_name

    return resulting_pool
