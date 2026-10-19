from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set in .env file")

engine = create_async_engine(DATABASE_URL)
AsyncSessionLocal = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

# This function is used in app startup to create tables
async def create_db_and_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_submission_worker_columns(conn)
        await ensure_career_score_kind(conn)
        await ensure_career_evaluation_binding(conn)
        await ensure_career_evaluation_source_binding(conn)
        await ensure_career_outcome_kind(conn)
        await ensure_career_reply_idempotency(conn)


async def ensure_submission_worker_columns(conn):
    """Add lease columns to an existing table without rewriting attempts."""
    # create_all does not upgrade tables already present on staging.
    columns = await conn.run_sync(lambda sync_conn: {
        column["name"] for column in inspect(sync_conn).get_columns("submission_attempts")
    })
    new_columns = {
        "claim_token_hash": "VARCHAR(64)",
        "lease_until": "TIMESTAMP WITH TIME ZONE",
        "browser_session_id": "VARCHAR(100)",
    }
    for name, kind in new_columns.items():
        if name not in columns:
            optional = "IF NOT EXISTS " if conn.dialect.name == "postgresql" else ""
            await conn.exec_driver_sql(
                f"ALTER TABLE submission_attempts ADD COLUMN {optional}{name} {kind}"
            )


async def ensure_career_score_kind(conn):
    """Mark historical scores unverified; legacy resume overlap is not an evaluation."""
    columns = await conn.run_sync(lambda sync_conn: {
        column["name"] for column in inspect(sync_conn).get_columns("career_applications")
    })
    if "score_kind" not in columns:
        optional = "IF NOT EXISTS " if conn.dialect.name == "postgresql" else ""
        await conn.exec_driver_sql(
            "ALTER TABLE career_applications ADD COLUMN "
            f"{optional}score_kind VARCHAR(32) NOT NULL DEFAULT 'unverified'"
        )


async def ensure_career_evaluation_binding(conn):
    """Historical scores remain unbound until a new report is explicitly scored."""
    columns = await conn.run_sync(lambda sync_conn: {
        column["name"] for column in inspect(sync_conn).get_columns("career_applications")
    })
    if "evaluation_artifact_id" not in columns:
        optional = "IF NOT EXISTS " if conn.dialect.name == "postgresql" else ""
        await conn.exec_driver_sql(
            "ALTER TABLE career_applications ADD COLUMN "
            f"{optional}evaluation_artifact_id VARCHAR(36)"
        )


async def ensure_career_evaluation_source_binding(conn):
    """Leave historical reports unverified until a source snapshot is bound."""
    columns = await conn.run_sync(lambda sync_conn: {
        column["name"] for column in inspect(sync_conn).get_columns("career_applications")
    })
    if "evaluation_source_artifact_id" not in columns:
        optional = "IF NOT EXISTS " if conn.dialect.name == "postgresql" else ""
        await conn.exec_driver_sql(
            "ALTER TABLE career_applications ADD COLUMN "
            f"{optional}evaluation_source_artifact_id VARCHAR(36)"
        )


async def ensure_career_outcome_kind(conn):
    """Keep existing terminal records unclassified until the candidate labels them."""
    for table in ("career_applications", "career_stage_events"):
        columns = await conn.run_sync(lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns(table)
        })
        if "outcome_kind" not in columns:
            optional = "IF NOT EXISTS " if conn.dialect.name == "postgresql" else ""
            await conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {optional}outcome_kind VARCHAR(32)"
            )


async def ensure_career_reply_idempotency(conn):
    """Allow retrying a confirmed inbox candidate without duplicating a reply."""
    columns = await conn.run_sync(lambda sync_conn: {
        column["name"] for column in inspect(sync_conn).get_columns("career_reply_events")
    })
    if "idempotency_key" not in columns:
        optional = "IF NOT EXISTS " if conn.dialect.name == "postgresql" else ""
        await conn.exec_driver_sql(
            "ALTER TABLE career_reply_events ADD COLUMN "
            f"{optional}idempotency_key VARCHAR(64)"
        )
    await conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_career_reply_user_idempotency "
        "ON career_reply_events (user_id, idempotency_key)"
    )

# Dependency for FastAPI routes
async def get_db():
    async with AsyncSessionLocal() as db:
        yield db
