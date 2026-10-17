"""The browser lease columns must upgrade an existing career database."""

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import database


def test_existing_attempt_table_adds_worker_columns(monkeypatch):
    async def check():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        monkeypatch.setattr(database, "engine", engine)
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE submission_attempts (id VARCHAR(36) PRIMARY KEY)"))
        async with engine.begin() as connection:
            await database.ensure_submission_worker_columns(connection)
        async with engine.begin() as connection:
            await database.ensure_submission_worker_columns(connection)  # restart remains safe
        async with engine.connect() as connection:
            columns = await connection.exec_driver_sql("PRAGMA table_info(submission_attempts)")
            names = {row[1] for row in columns}
            assert {"claim_token_hash", "lease_until", "browser_session_id"} <= names
        await engine.dispose()

    asyncio.run(check())
