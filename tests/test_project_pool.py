"""Tests for idempotent token project-pool provisioning."""

import pytest

from src.core.models import Project, Token
from src.services.tokens.project_pool import ensure_project_pool


LONG_ST = "eyJ" + "s" * 1100


class FakeDatabase:
    def __init__(self, projects=None):
        self.projects = list(projects or [])
        self.added_projects = []
        self.token_updates = []
        self._next_id = max((project.id or 0 for project in self.projects), default=0) + 1

    async def get_projects_by_token(self, token_id):
        return [project for project in self.projects if project.token_id == token_id]

    async def add_project(self, project):
        project.id = self._next_id
        self._next_id += 1
        self.projects.append(project)
        self.added_projects.append(project)
        return project.id

    async def update_token(self, token_id, **fields):
        self.token_updates.append((token_id, fields))


class FakeFlowClient:
    def __init__(self):
        self.create_calls = []

    async def create_project(self, st, title):
        self.create_calls.append((st, title))
        return f"created-project-{len(self.create_calls)}"


def make_token(**overrides):
    values = {
        "id": 17,
        "st": LONG_ST,
        "email": "pool@example.com",
        "current_project_id": None,
        "current_project_name": None,
    }
    values.update(overrides)
    return Token(**values)


def make_project(project_id, project_name, *, row_id, active=True):
    return Project(
        id=row_id,
        project_id=project_id,
        token_id=17,
        project_name=project_name,
        is_active=active,
    )


@pytest.mark.asyncio
async def test_full_pool_with_valid_current_project_performs_no_writes():
    projects = [
        make_project("project-1", "Launch P1", row_id=1),
        make_project("project-2", "Launch P2", row_id=2),
    ]
    db = FakeDatabase(projects)
    flow_client = FakeFlowClient()
    token = make_token(
        current_project_id="project-1",
        current_project_name="Launch P1",
    )

    result = await ensure_project_pool(db, flow_client, token, pool_size=2)

    assert [project.project_id for project in result] == ["project-1", "project-2"]
    assert flow_client.create_calls == []
    assert db.added_projects == []
    assert db.token_updates == []


@pytest.mark.asyncio
async def test_undersized_pool_creates_only_missing_projects_after_provided_row():
    provided_project = make_project("provided-project", "Client Launch P1", row_id=4)
    db = FakeDatabase([provided_project])
    flow_client = FakeFlowClient()
    token = make_token(
        current_project_id="provided-project",
        current_project_name="Client Launch P1",
    )

    result = await ensure_project_pool(db, flow_client, token, pool_size=3)

    assert flow_client.create_calls == [
        (LONG_ST, "Client Launch P2"),
        (LONG_ST, "Client Launch P3"),
    ]
    assert [project.project_name for project in db.added_projects] == [
        "Client Launch P2",
        "Client Launch P3",
    ]
    assert [project.project_id for project in result] == [
        "provided-project",
        "created-project-1",
        "created-project-2",
    ]
    assert db.token_updates == []


@pytest.mark.asyncio
async def test_valid_current_pointer_is_preserved_without_rotation():
    projects = [
        make_project("project-1", "Stable P1", row_id=10),
        make_project("project-2", "Stable P2", row_id=20),
        make_project("project-3", "Stable P3", row_id=30),
    ]
    db = FakeDatabase(projects)
    flow_client = FakeFlowClient()
    token = make_token(
        current_project_id="project-2",
        current_project_name="Stable P2",
    )

    await ensure_project_pool(db, flow_client, token, pool_size=3)

    assert token.current_project_id == "project-2"
    assert token.current_project_name == "Stable P2"
    assert db.token_updates == []


@pytest.mark.asyncio
async def test_missing_current_pointer_is_repaired_to_first_project():
    projects = [
        make_project("project-later", "Repair P2", row_id=9),
        make_project("project-first", "Repair P1", row_id=3),
    ]
    db = FakeDatabase(projects)
    flow_client = FakeFlowClient()
    token = make_token(
        current_project_id="deleted-project",
        current_project_name="Deleted P1",
    )

    result = await ensure_project_pool(db, flow_client, token, pool_size=2)

    assert [project.project_id for project in result] == ["project-first", "project-later"]
    assert db.token_updates == [
        (
            17,
            {
                "current_project_id": "project-first",
                "current_project_name": "Repair P1",
            },
        )
    ]
    assert token.current_project_id == "project-first"
    assert token.current_project_name == "Repair P1"


@pytest.mark.asyncio
async def test_returns_only_active_projects_in_stable_row_order():
    projects = [
        make_project("project-z", "Ordered P3", row_id=30),
        make_project("inactive", "Ordered P2", row_id=2, active=False),
        make_project("project-a", "Ordered P1", row_id=10),
        make_project("project-m", "Ordered P2", row_id=20),
    ]
    db = FakeDatabase(projects)
    flow_client = FakeFlowClient()
    token = make_token(
        current_project_id="project-m",
        current_project_name="Ordered P2",
    )

    result = await ensure_project_pool(db, flow_client, token, pool_size=3)

    assert [project.project_id for project in result] == [
        "project-a",
        "project-m",
        "project-z",
    ]
    assert all(project.is_active for project in result)
    assert db.token_updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "error"),
    [
        (None, "token"),
        (make_token(id=None), "id"),
        (make_token(st="   "), "ST"),
    ],
)
async def test_rejects_invalid_token_identity_or_session(token, error):
    db = FakeDatabase()
    flow_client = FakeFlowClient()

    with pytest.raises(ValueError, match=error):
        await ensure_project_pool(db, flow_client, token, pool_size=1)

    assert flow_client.create_calls == []
    assert db.added_projects == []
    assert db.token_updates == []


@pytest.mark.asyncio
async def test_pool_size_is_clamped_to_supported_bounds():
    low_db = FakeDatabase()
    low_client = FakeFlowClient()
    low_token = make_token()

    low_result = await ensure_project_pool(
        low_db,
        low_client,
        low_token,
        pool_size=0,
        base_name="Clamp",
    )

    assert [project.project_name for project in low_result] == ["Clamp P1"]
    assert len(low_client.create_calls) == 1

    high_projects = [
        make_project(f"project-{index:02d}", f"Clamp P{index}", row_id=index)
        for index in range(1, 51)
    ]
    high_db = FakeDatabase(high_projects)
    high_client = FakeFlowClient()
    high_token = make_token(
        current_project_id="project-01",
        current_project_name="Clamp P1",
    )

    high_result = await ensure_project_pool(high_db, high_client, high_token, pool_size=500)

    assert len(high_result) == 50
    assert high_client.create_calls == []
    assert high_db.added_projects == []
    assert high_db.token_updates == []
