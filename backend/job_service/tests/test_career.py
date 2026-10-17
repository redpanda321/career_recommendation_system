"""Cross-user, consent and idempotency checks for the canonical career store."""

import asyncio
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-career-bridge-secret")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from app.database import AsyncSessionLocal, Base, engine
from app.models import (
    CareerApplication, CareerArtifact, CareerCandidateProfile, CareerStageEvent,
    SubmissionAuthorization, SubmissionAttempt,
)
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
                CareerCandidateProfile.__table__, CareerStageEvent.__table__,
                SubmissionAuthorization.__table__, SubmissionAttempt.__table__,
            ])
    asyncio.run(create_tables())


def teardown_module():
    asyncio.run(engine.dispose())


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


def test_profile_and_feedback_are_candidate_scoped_and_auditable():
    with TestClient(app) as client:
        assert client.get("/career-workspace/profile", headers=headers(41)).json()["full_name"] == ""
        saved = client.put("/career-workspace/profile", headers=headers(41), json={
            "full_name": "Alex Example", "target_roles": ["Engineer"],
            "skills": ["Python"], "resume_markdown": "# Reviewed resume",
        })
        assert saved.status_code == 200, saved.text
        assert client.get("/career-workspace/profile", headers=headers(41)).json()["skills"] == ["Python"]
        assert client.get("/career-workspace/profile", headers=headers(42)).json()["full_name"] == ""
        case = client.post("/career-workspace/applications", headers=headers(41), json={
            "company": "Example", "role": "Engineer", "sources": ["career-ops"],
        }).json()
        endpoint = f"/career-workspace/applications/{case['id']}/feedback"
        assert client.post(endpoint, headers=headers(42), json={
            "stage": "interview", "note": "Employer invited me",
        }).status_code == 404
        changed = client.post(endpoint, headers=headers(41), json={
            "stage": "interview", "note": "Employer invited me",
        })
        assert changed.status_code == 200, changed.text
        assert changed.json()["application"]["stage"] == "interview"
        events = client.get(
            f"/career-workspace/applications/{case['id']}/activity", headers=headers(41)
        ).json()["feedback"]
        assert events[-1]["from_stage"] == "saved"
        assert events[-1]["to_stage"] == "interview"
        assert events[-1]["note"] == "Employer invited me"


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


def test_worker_claim_and_verified_receipt_are_user_scoped():
    with TestClient(app) as client:
        job = client.post("/career-workspace/applications", json={
            "company": "Field", "role": "Analyst", "fit_score": 94,
            "job_url": "https://boards.greenhouse.io/field/jobs/321",
            "sources": ["career-compass"],
        }, headers=headers(8)).json()
        policy = client.post("/career-workspace/authorizations", json={
            "mode": "confirm_each", "application_id": job["id"],
            "allowed_sites": ["greenhouse"], "max_applications": 1,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }, headers=headers(8)).json()
        attempt = client.post(
            f"/career-workspace/applications/{job['id']}/submission-attempts",
            json={"authorization_id": policy["id"], "idempotency_key": "field-analyst-321"},
            headers=headers(8),
        ).json()
        endpoint = f"/career-workspace/submission-attempts/{attempt['id']}"
        assert client.get(endpoint, headers=headers(9)).status_code == 404
        assert client.post(endpoint + "/claim", json={
            "browser_session_id": "owned-browser-8",
        }, headers=headers(9)).status_code == 404
        claimed = client.post(endpoint + "/claim", json={
            "browser_session_id": "owned-browser-8",
        }, headers=headers(8))
        assert claimed.status_code == 200, claimed.text
        token = claimed.json()["claim_token"]
        assert client.post(endpoint + "/claim", json={
            "browser_session_id": "second-browser",
        }, headers=headers(8)).status_code == 409
        assert client.post(endpoint + "/finish", json={
            "claim_token": token, "status": "submitted",
            "receipt_url": "https://evil.example/receipt",
            "confirmation_text": "Application submitted",
        }, headers=headers(8)).status_code == 400
        result = client.post(endpoint + "/finish", json={
            "claim_token": token, "status": "submitted",
            "receipt_url": "https://boards.greenhouse.io/field/jobs/321/confirmation",
            "confirmation_text": "Thank you for applying",
        }, headers=headers(8))
        assert result.status_code == 200, result.text
        assert result.json()["external_submission"] == "verified"
        assert client.get(endpoint, headers=headers(8)).json()["status"] == "submitted"
        tracked = client.get("/career-workspace/applications", headers=headers(8)).json()[-1]
        assert tracked["stage"] == "applied"
        assert tracked["submission_blocked"] is True
        next_policy = client.post("/career-workspace/authorizations", json={
            "mode": "confirm_each", "application_id": job["id"],
            "allowed_sites": ["greenhouse"], "max_applications": 1,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }, headers=headers(8)).json()
        assert client.post(
            f"/career-workspace/applications/{job['id']}/submission-attempts",
            json={"authorization_id": next_policy["id"], "idempotency_key": "duplicate-field-321"},
            headers=headers(8),
        ).status_code == 409


def test_uncertain_attempt_requires_candidate_resolution_before_retry():
    with TestClient(app) as client:
        job = client.post("/career-workspace/applications", json={
            "company": "Maybe", "role": "Designer", "fit_score": 92,
            "job_url": "https://jobs.lever.co/maybe/555", "sources": ["career-compass"],
        }, headers=headers(10)).json()
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        def authorize():
            return client.post("/career-workspace/authorizations", json={
                "mode": "confirm_each", "application_id": job["id"],
                "allowed_sites": ["lever"], "max_applications": 1, "expires_at": expires,
            }, headers=headers(10)).json()["id"]
        first_policy = authorize()
        endpoint = f"/career-workspace/applications/{job['id']}/submission-attempts"
        attempt = client.post(endpoint, json={
            "authorization_id": first_policy, "idempotency_key": "maybe-designer-555",
        }, headers=headers(10)).json()
        attempt_url = f"/career-workspace/submission-attempts/{attempt['id']}"
        token = client.post(attempt_url + "/claim", json={
            "browser_session_id": "owned-browser-10",
        }, headers=headers(10)).json()["claim_token"]
        assert client.post(attempt_url + "/finish", json={
            "claim_token": token, "status": "uncertain",
            "reason": "Submit was clicked but no receipt appeared",
        }, headers=headers(10)).status_code == 200
        tracked = client.get("/career-workspace/applications", headers=headers(10)).json()[-1]
        assert tracked["blocking_attempt_status"] == "uncertain"
        next_policy = authorize()
        assert client.post(endpoint, json={
            "authorization_id": next_policy, "idempotency_key": "retry-designer-555",
        }, headers=headers(10)).status_code == 409
        assert client.post(attempt_url + "/resolve", json={
            "confirmed_not_submitted": False, "note": "Checked my account and email",
        }, headers=headers(10)).status_code == 409
        assert client.post(attempt_url + "/resolve", json={
            "confirmed_not_submitted": True, "note": "Checked my account and email",
        }, headers=headers(11)).status_code == 404
        assert client.post(attempt_url + "/resolve", json={
            "confirmed_not_submitted": True, "note": "Checked my account and email",
        }, headers=headers(10)).status_code == 200
        assert client.post(endpoint, json={
            "authorization_id": next_policy, "idempotency_key": "retry-designer-555",
        }, headers=headers(10)).status_code == 200


def test_expired_worker_lease_requires_manual_verification():
    with TestClient(app) as client:
        job = client.post("/career-workspace/applications", json={
            "company": "Lease", "role": "Tester", "fit_score": 90,
            "job_url": "https://jobs.lever.co/lease/111", "sources": ["career-compass"],
        }, headers=headers(12)).json()
        policy = client.post("/career-workspace/authorizations", json={
            "mode": "confirm_each", "application_id": job["id"],
            "allowed_sites": ["lever"], "max_applications": 1,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }, headers=headers(12)).json()
        attempt = client.post(
            f"/career-workspace/applications/{job['id']}/submission-attempts",
            json={"authorization_id": policy["id"], "idempotency_key": "lease-tester-111"},
            headers=headers(12),
        ).json()
        attempt_url = f"/career-workspace/submission-attempts/{attempt['id']}"
        claimed = client.post(attempt_url + "/claim", json={
            "browser_session_id": "owned-browser-12",
        }, headers=headers(12)).json()

        async def expire_lease():
            async with AsyncSessionLocal() as db:
                row = await db.get(SubmissionAttempt, attempt["id"])
                row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
                await db.commit()
        asyncio.run(expire_lease())

        tracked = client.get("/career-workspace/applications", headers=headers(12)).json()[-1]
        assert tracked["blocking_attempt_status"] == "uncertain"
        assert client.post(attempt_url + "/finish", json={
            "claim_token": claimed["claim_token"], "status": "submitted",
            "receipt_url": "https://jobs.lever.co/lease/111/receipt",
            "confirmation_text": "Application submitted",
        }, headers=headers(12)).status_code == 409
