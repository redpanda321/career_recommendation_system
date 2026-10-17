"""Cross-user, consent and idempotency checks for the canonical career store."""

import asyncio
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-career-bridge-secret")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from app.database import Base, engine
from app.models import CareerApplication, CareerArtifact, SubmissionAuthorization, SubmissionAttempt
from app.routers.career import router


app = FastAPI()
app.include_router(router, prefix="/career-workspace")


def token(user_id: int, **overrides: object) -> str:
    claims = {
        "sub": str(user_id), "kind": "career_bridge", "iss": "hanggent",
        "aud": "career-compass", "exp": datetime.now(timezone.utc) + timedelta(minutes=1),
    }
    claims.update(overrides)
    return jwt.encode(claims, os.environ["SECRET_KEY"], algorithm="HS256")


def headers(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user_id)}"}


def setup_module():
    async def create_tables():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=[
                CareerApplication.__table__, CareerArtifact.__table__,
                SubmissionAuthorization.__table__, SubmissionAttempt.__table__,
            ])
    asyncio.run(create_tables())


def test_requires_scoped_bridge_identity():
    with TestClient(app) as client:
        assert client.get("/career-workspace/applications").status_code == 401
        ordinary = token(1, kind="ordinary")
        assert client.get("/career-workspace/applications", headers={
            "Authorization": f"Bearer {ordinary}",
        }).status_code == 401


def test_applications_are_idempotent_and_user_isolated():
    body = {
        "company": "Acme", "role": "Engineer", "job_url": "https://www.linkedin.com/jobs/view/123",
        "fit_score": 91, "sources": ["career-ops", "ai-job-search"],
    }
    with TestClient(app) as client:
        first = client.post("/career-workspace/applications", json=body, headers=headers(1))
        assert first.status_code == 200, first.text
        second = client.post("/career-workspace/applications", json=body, headers=headers(1))
        assert second.json()["id"] == first.json()["id"]
        applied = client.post("/career-workspace/applications", json={
            **body, "stage": "applied", "sources": ["proficiently"],
        }, headers=headers(1))
        assert applied.json()["id"] == first.json()["id"]
        assert applied.json()["stage"] == "applied"
        assert set(applied.json()["sources"]) == {"career-ops", "ai-job-search", "proficiently"}
        assert len(client.get("/career-workspace/applications", headers=headers(1)).json()) == 1
        assert client.get("/career-workspace/applications", headers=headers(2)).json() == []

        artifact = client.post(
            f"/career-workspace/applications/{first.json()['id']}/artifacts",
            json={"kind": "resume", "source": "ai-job-search", "content_markdown": "# Tailored resume"},
            headers=headers(1),
        )
        assert artifact.status_code == 200, artifact.text
        assert client.post(
            f"/career-workspace/applications/{first.json()['id']}/artifacts",
            json={"kind": "resume", "source": "proficiently", "content_markdown": "# Other resume"},
            headers=headers(2),
        ).status_code == 404
        assert client.get(
            f"/career-workspace/applications/{first.json()['id']}/activity", headers=headers(2)
        ).status_code == 404
        activity = client.get(
            f"/career-workspace/applications/{first.json()['id']}/activity", headers=headers(1)
        ).json()
        assert len(activity["artifacts"]) == 1


def test_one_job_confirmation_and_bounded_auto_authorization():
    with TestClient(app) as client:
        case = client.post("/career-workspace/applications", json={
            "company": "Example", "role": "Developer", "fit_score": 85,
            "job_url": "https://boards.greenhouse.io/example/jobs/456",
            "sources": ["proficiently"],
        }, headers=headers(1)).json()
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        confirm = client.post("/career-workspace/authorizations", json={
            "mode": "confirm_each", "application_id": case["id"],
            "allowed_sites": ["greenhouse"], "max_applications": 1, "expires_at": expires,
        }, headers=headers(1))
        assert confirm.status_code == 200, confirm.text
        endpoint = f"/career-workspace/applications/{case['id']}/submission-attempts"
        attempt = client.post(endpoint, json={
            "authorization_id": confirm.json()["id"], "idempotency_key": "first-attempt-456",
        }, headers=headers(1))
        assert attempt.status_code == 200, attempt.text
        assert attempt.json()["external_submission"] == "not_started"
        assert client.post(endpoint, json={
            "authorization_id": confirm.json()["id"], "idempotency_key": "first-attempt-456",
        }, headers=headers(1)).json()["id"] == attempt.json()["id"]
        assert client.post(endpoint, json={
            "authorization_id": confirm.json()["id"], "idempotency_key": "second-attempt-456",
        }, headers=headers(1)).status_code == 409

        auto = client.post("/career-workspace/authorizations", json={
            "mode": "auto", "allowed_sites": ["greenhouse"], "max_applications": 2,
            "min_fit_score": 90, "expires_at": expires,
        }, headers=headers(1))
        assert auto.status_code == 200, auto.text
        assert client.post(endpoint, json={
            "authorization_id": auto.json()["id"], "idempotency_key": "auto-attempt-456",
        }, headers=headers(1)).status_code == 403
        assert client.post(
            f"/career-workspace/authorizations/{auto.json()['id']}/revoke", headers=headers(1)
        ).status_code == 200


def test_auto_campaign_is_site_scoped_bounded_and_revocable():
    with TestClient(app) as client:
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        policy = client.post("/career-workspace/authorizations", json={
            "mode": "auto", "allowed_sites": ["linkedin"], "max_applications": 1,
            "min_fit_score": 90, "expires_at": expires,
        }, headers=headers(3)).json()
        allowed = client.post("/career-workspace/applications", json={
            "company": "North", "role": "Engineer", "fit_score": 95,
            "job_url": "https://www.linkedin.com/jobs/view/999", "sources": ["career-compass"],
        }, headers=headers(3)).json()
        other_site = client.post("/career-workspace/applications", json={
            "company": "South", "role": "Engineer", "fit_score": 95,
            "job_url": "https://boards.greenhouse.io/south/jobs/999", "sources": ["career-compass"],
        }, headers=headers(3)).json()
        assert client.post(
            f"/career-workspace/applications/{other_site['id']}/submission-attempts",
            json={"authorization_id": policy["id"], "idempotency_key": "other-site-999"},
            headers=headers(3),
        ).status_code == 403
        attempt = client.post(
            f"/career-workspace/applications/{allowed['id']}/submission-attempts",
            json={"authorization_id": policy["id"], "idempotency_key": "allowed-site-999"},
            headers=headers(3),
        )
        assert attempt.status_code == 200, attempt.text
        assert attempt.json()["status"] == "queued"
        assert client.get(
            f"/career-workspace/applications/{allowed['id']}/activity", headers=headers(3)
        ).json()["submission_attempts"][0]["status"] == "queued"
        assert client.post(
            f"/career-workspace/authorizations/{policy['id']}/revoke", headers=headers(4)
        ).status_code == 404
        assert client.post(
            f"/career-workspace/authorizations/{policy['id']}/revoke", headers=headers(3)
        ).status_code == 200
