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


def test_existing_applications_gain_nullable_evaluation_binding():
    async def check():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE career_applications (id VARCHAR PRIMARY KEY, fit_score FLOAT)"))
            await connection.execute(text("INSERT INTO career_applications (id, fit_score) VALUES ('old', 85)"))
            await database.ensure_career_evaluation_binding(connection)
            await database.ensure_career_evaluation_binding(connection)
            await database.ensure_career_evaluation_source_binding(connection)
            await database.ensure_career_evaluation_source_binding(connection)
            result = await connection.execute(text(
                "SELECT fit_score, evaluation_artifact_id, evaluation_source_artifact_id "
                "FROM career_applications WHERE id = 'old'"
            ))
            assert result.one() == (85, None, None)
        await engine.dispose()

    asyncio.run(check())
