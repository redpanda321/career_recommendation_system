"""Canonical career records and bounded submission authorizations.

This service never performs a third-party submission. An attempt stays queued
until a separate, authenticated browser worker records a verified outcome.
"""

from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.career_auth import career_user
from app.database import get_db
from app.models import CareerApplication, CareerArtifact, SubmissionAuthorization, SubmissionAttempt


router = APIRouter(tags=["career-workspace"])

SITES = {"indeed", "linkedin", "greenhouse", "lever", "workday"}
STAGES = {"saved", "applied", "screening", "assessment", "interview", "offer", "accepted", "closed"}
SOURCE_NAMES = {"career-compass", "career-ops", "ai-job-search", "proficiently", "hanggent"}


def canonical_url(raw: str) -> str:
    parsed = urlsplit(raw.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("A public HTTP(S) job URL is required")
    host = parsed.hostname.lower().removeprefix("www.")
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".local", ".internal")):
        raise ValueError("A public job URL is required")
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("A public job URL is required")
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def site_for_url(raw: str) -> str | None:
    host = urlsplit(raw).hostname or ""
    host = host.lower()
    domains = {
        "indeed": ("indeed.com",),
        "linkedin": ("linkedin.com",),
        "greenhouse": ("greenhouse.io", "greenhouse.com"),
        "lever": ("lever.co",),
        "workday": ("myworkdayjobs.com",),
    }
    for site, suffixes in domains.items():
        if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
            return site
    return None


class ApplicationIn(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    job_url: str | None = None
    stage: str = "saved"
    fit_score: float | None = Field(default=None, ge=0, le=100)
    sources: list[str] = Field(default_factory=list, max_length=5)
    notes: list[str] = Field(default_factory=list, max_length=20)


class ArtifactIn(BaseModel):
    kind: str = Field(pattern="^(resume|cover_letter|application_package|evaluation|interview_prep)$")
    source: str = Field(pattern="^(career-compass|career-ops|ai-job-search|proficiently|hanggent)$")
    content_markdown: str = Field(min_length=1, max_length=100000)


class AuthorizationIn(BaseModel):
    mode: str = Field(pattern="^(confirm_each|auto)$")
    application_id: str | None = None
    allowed_sites: list[str] = Field(min_length=1, max_length=5)
    max_applications: int = Field(ge=1, le=10)
    min_fit_score: float = Field(default=80, ge=0, le=100)
    expires_at: datetime


class AttemptIn(BaseModel):
    authorization_id: str
    idempotency_key: str = Field(min_length=8, max_length=120)


def _application_out(item: CareerApplication) -> dict:
    return {
        "id": item.id, "company": item.company, "role": item.role,
        "location": item.location, "job_url": item.job_url, "stage": item.stage,
        "fit_score": item.fit_score, "sources": item.sources, "notes": item.notes,
    }


@router.get("/applications/{application_id}/activity")
async def application_activity(
    application_id: str, user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    application = (await db.execute(select(CareerApplication).where(
        CareerApplication.id == application_id, CareerApplication.user_id == user_id,
    ))).scalar_one_or_none()
    if application is None:
        raise HTTPException(404, "Application not found")
    artifacts = (await db.execute(select(CareerArtifact).where(
        CareerArtifact.application_id == application_id, CareerArtifact.user_id == user_id,
    ).order_by(CareerArtifact.created_at.desc()))).scalars().all()
    attempts = (await db.execute(select(SubmissionAttempt).where(
        SubmissionAttempt.application_id == application_id, SubmissionAttempt.user_id == user_id,
    ).order_by(SubmissionAttempt.created_at.desc()))).scalars().all()
    return {
        "artifacts": [{"id": item.id, "kind": item.kind, "source": item.source,
                       "version": item.version, "content_markdown": item.content_markdown}
                      for item in artifacts],
        "submission_attempts": [{"id": item.id, "site": item.site, "status": item.status,
                                 "receipt": item.receipt, "error": item.error}
                                for item in attempts],
    }


@router.get("/applications")
async def list_applications(user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(CareerApplication).where(CareerApplication.user_id == user_id)
        .order_by(CareerApplication.created_at.desc())
    )).scalars().all()
    return [_application_out(row) for row in rows]


@router.post("/applications")
async def upsert_application(
    body: ApplicationIn, user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    if body.stage not in STAGES or not set(body.sources).issubset(SOURCE_NAMES):
        raise HTTPException(400, "Invalid stage or source")
    try:
        url = canonical_url(body.job_url) if body.job_url else None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    key = url or "::".join(
        part.strip().casefold() for part in (body.company, body.role, body.location or "")
    )
    row = (await db.execute(select(CareerApplication).where(
        CareerApplication.user_id == user_id, CareerApplication.canonical_key == key,
    ))).scalar_one_or_none()
    if row is None:
        row = CareerApplication(
            user_id=user_id, canonical_key=key, company=body.company.strip(), role=body.role.strip(),
            location=body.location, job_url=url, stage=body.stage, fit_score=body.fit_score,
            sources=sorted(set(body.sources)), notes=list(dict.fromkeys(body.notes)),
        )
        db.add(row)
    else:
        row.sources = sorted(set(row.sources or []) | set(body.sources))
        row.notes = list(dict.fromkeys((row.notes or []) + body.notes))
        priority = {"saved": 1, "applied": 2, "screening": 3, "assessment": 4,
                    "interview": 5, "offer": 6, "accepted": 7, "closed": 8}
        if priority[body.stage] > priority[row.stage]:
            row.stage = body.stage
        if body.fit_score is not None:
            row.fit_score = max(row.fit_score or 0, body.fit_score)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Application was concurrently imported; retry") from exc
    await db.refresh(row)
    return _application_out(row)


@router.post("/applications/{application_id}/artifacts")
async def add_artifact(
    application_id: str, body: ArtifactIn, user_id: str = Depends(career_user),
    db: AsyncSession = Depends(get_db),
):
    application = (await db.execute(select(CareerApplication).where(
        CareerApplication.id == application_id, CareerApplication.user_id == user_id,
    ))).scalar_one_or_none()
    if application is None:
        raise HTTPException(404, "Application not found")
    last_version = (await db.execute(select(func.max(CareerArtifact.version)).where(
        CareerArtifact.application_id == application_id, CareerArtifact.kind == body.kind,
    ))).scalar_one() or 0
    artifact = CareerArtifact(
        application_id=application_id, user_id=user_id, kind=body.kind,
        source=body.source, version=last_version + 1, content_markdown=body.content_markdown,
    )
    db.add(artifact)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Artifact version conflict; retry") from exc
    return {"id": artifact.id, "application_id": application_id, "kind": body.kind, "version": artifact.version}


@router.post("/authorizations")
async def authorize_submission(
    body: AuthorizationIn, user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    expires = body.expires_at if body.expires_at.tzinfo else body.expires_at.replace(tzinfo=timezone.utc)
    if expires <= now or expires > now + timedelta(hours=24):
        raise HTTPException(400, "Authorization must expire within 24 hours")
    sites = sorted(set(body.allowed_sites))
    if not set(sites).issubset(SITES):
        raise HTTPException(400, "Unsupported submission site")
    if body.mode == "confirm_each":
        if not body.application_id or body.max_applications != 1:
            raise HTTPException(400, "A one-application confirmation is required")
        application = (await db.execute(select(CareerApplication).where(
            CareerApplication.id == body.application_id, CareerApplication.user_id == user_id,
        ))).scalar_one_or_none()
        if application is None:
            raise HTTPException(404, "Application not found")
    elif body.application_id:
        raise HTTPException(400, "Auto authorization applies to a bounded campaign, not one application")
    policy = SubmissionAuthorization(
        user_id=user_id, mode=body.mode, application_id=body.application_id,
        allowed_sites=sites, max_applications=body.max_applications,
        min_fit_score=body.min_fit_score, expires_at=expires,
    )
    db.add(policy)
    await db.commit()
    return {"id": policy.id, "mode": policy.mode, "allowed_sites": sites,
            "max_applications": policy.max_applications, "expires_at": expires.isoformat()}


@router.post("/authorizations/{authorization_id}/revoke")
async def revoke_authorization(
    authorization_id: str, user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    policy = (await db.execute(select(SubmissionAuthorization).where(
        SubmissionAuthorization.id == authorization_id, SubmissionAuthorization.user_id == user_id,
    ))).scalar_one_or_none()
    if policy is None:
        raise HTTPException(404, "Authorization not found")
    policy.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    return {"revoked": True}


@router.post("/applications/{application_id}/submission-attempts")
async def queue_submission(
    application_id: str, body: AttemptIn, user_id: str = Depends(career_user),
    db: AsyncSession = Depends(get_db),
):
    existing = (await db.execute(select(SubmissionAttempt).where(
        SubmissionAttempt.user_id == user_id, SubmissionAttempt.idempotency_key == body.idempotency_key,
    ))).scalar_one_or_none()
    if existing is not None:
        if existing.application_id != application_id:
            raise HTTPException(409, "Idempotency key already used for another application")
        return {"id": existing.id, "status": existing.status}
    application = (await db.execute(select(CareerApplication).where(
        CareerApplication.id == application_id, CareerApplication.user_id == user_id,
    ))).scalar_one_or_none()
    if application is None or not application.job_url:
        raise HTTPException(404, "Application with a job URL not found")
    policy = (await db.execute(select(SubmissionAuthorization).where(
        SubmissionAuthorization.id == body.authorization_id,
        SubmissionAuthorization.user_id == user_id,
    ).with_for_update())).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    expiry = policy.expires_at if policy and policy.expires_at.tzinfo else (
        policy.expires_at.replace(tzinfo=timezone.utc) if policy else None
    )
    if policy is None or policy.revoked_at or expiry < now:
        raise HTTPException(403, "Submission authorization is missing or expired")
    site = site_for_url(application.job_url)
    if site not in (policy.allowed_sites or []):
        raise HTTPException(403, "Job site is outside the authorization")
    if policy.mode == "confirm_each" and policy.application_id != application_id:
        raise HTTPException(403, "Authorization is for another application")
    if policy.mode == "auto" and (application.fit_score is None or application.fit_score < policy.min_fit_score):
        raise HTTPException(403, "Job does not meet the authorized fit threshold")
    already_queued = (await db.execute(select(SubmissionAttempt.id).where(
        SubmissionAttempt.authorization_id == policy.id,
        SubmissionAttempt.application_id == application_id,
    ))).scalar_one_or_none()
    if already_queued:
        raise HTTPException(409, "This application is already queued under the authorization")
    if policy.used_applications >= policy.max_applications:
        raise HTTPException(403, "Authorization limit reached")
    attempt = SubmissionAttempt(
        user_id=user_id, application_id=application_id, authorization_id=policy.id,
        idempotency_key=body.idempotency_key, site=site, status="queued",
    )
    policy.used_applications += 1
    db.add(attempt)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Submission was concurrently queued; retry") from exc
    return {"id": attempt.id, "status": "queued", "external_submission": "not_started"}
