"""Shared DB fixtures for token lifecycle tests.

Pattern follows ``tests/test_verified_account_snapshot.py``: construct a real
``Database`` on a tmp_path SQLite file, ``init_db()`` the schema, and insert a
token via the public ``add_token(Token(...))`` facade (which atomically seeds
both ``tokens`` and ``token_lifecycle`` rows).
"""

import asyncio

from src.core.database import Database
from src.core.models import Token
from src.core.repositories.token_lifecycle_repository import TokenLifecycleRepository


def make_database_with_token(
    tmp_path,
    *,
    ban_reason=None,
    is_active=False,
    verified_email=None,
):
    """Build a temp DB with one token + lifecycle skeleton.

    Returns ``(db, repo, token_id)`` where ``db`` is the live ``Database``
    facade (also acts as the engine), ``repo`` is a ``TokenLifecycleRepository``
    bound to it, and ``token_id`` is the new token's integer id.
    """
    db_path = tmp_path / "test.db"
    db = Database(db_path=str(db_path))

    async def _setup():
        await db.init_db()
        token_id = await db.add_token(
            Token(
                st="placeholder-st-" + "x" * 1100,
                email="alice@example.com",
                name="Alice",
                is_active=is_active,
                ban_reason=ban_reason,
            )
        )
        if verified_email is not None:
            await db.set_token_verified_email(token_id, verified_email)
        return token_id

    token_id = asyncio.run(_setup())
    repo = TokenLifecycleRepository(db)
    return db, repo, token_id
