"""Canonical career records and bounded submission authorizations.

This service never performs a third-party submission. An attempt stays queued
until a separate, authenticated browser worker records a verified outcome.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.career_auth import career_user
from app.comparison_evidence import parse_requirement_evidence, parse_score_evidence
from app.database import get_db
from app.models import (
    CareerApplication, CareerArtifact, CareerCandidateProfile, CareerComparisonSnapshot,
    CareerContactEvent, CareerPipelineEntry, CareerStageEvent,
    SubmissionAuthorization, SubmissionAttempt,
)


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
    score_kind: str = Field(default="unverified", pattern="^(unverified|resume_overlap|career_ops_import|career_ops_evaluation)$")
    sources: list[str] = Field(default_factory=list, max_length=5)
    notes: list[str] = Field(default_factory=list, max_length=20)


class PipelineIn(BaseModel):
    job_url: str = Field(min_length=10, max_length=2000)
    company: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=200)


class CandidateProfileIn(BaseModel):
    full_name: str = Field(default="", max_length=200)
    headline: str = Field(default="", max_length=300)
    location: str = Field(default="", max_length=200)
    target_roles: list[str] = Field(default_factory=list, max_length=20)
    skills: list[str] = Field(default_factory=list, max_length=100)
    resume_markdown: str = Field(default="", max_length=100000)


class ComparisonIn(BaseModel):
    application_ids: list[UUID] = Field(min_length=2, max_length=5)


class EvaluationScoreIn(BaseModel):
    artifact_id: UUID


class StageFeedbackIn(BaseModel):
    stage: str
    note: str = Field(min_length=3, max_length=2000)


class ContactIn(BaseModel):
    channel: str = Field(pattern="^(email|linkedin|phone|other)$")
    note: str = Field(min_length=3, max_length=2000)
    contacted_at: datetime
    next_followup_at: datetime | None = None
    confirmed_contact: bool


class ArtifactIn(BaseModel):
    kind: str = Field(pattern="^(resume|cover_letter|application_package|evaluation|research|research_task_link|source_observation|liveness_check|interview_prep|followup|offer_prep)$")
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


class ClaimIn(BaseModel):
    browser_session_id: str = Field(min_length=1, max_length=100)


class FinishIn(BaseModel):
    claim_token: str = Field(min_length=40, max_length=100)
    status: str = Field(pattern="^(submitted|needs_user|failed|uncertain)$")
    receipt_url: str | None = None
    confirmation_text: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=500)


class ResolveIn(BaseModel):
    confirmed_not_submitted: bool
    note: str = Field(min_length=10, max_length=500)


def _application_out(
    item: CareerApplication, *, blocking_attempt: tuple[str, str] | None = None,
    latest_contact: CareerContactEvent | None = None,
) -> dict:
    return {
        "id": item.id, "company": item.company, "role": item.role,
        "location": item.location, "job_url": item.job_url, "stage": item.stage,
        "fit_score": item.fit_score, "score_kind": item.score_kind,
        "evaluation_artifact_id": item.evaluation_artifact_id,
        "sources": item.sources, "notes": item.notes,
        "created_at": item.created_at, "updated_at": item.updated_at,
        "submission_blocked": blocking_attempt is not None,
        "blocking_attempt_id": blocking_attempt[0] if blocking_attempt else None,
        "blocking_attempt_status": blocking_attempt[1] if blocking_attempt else None,
        "last_contact_at": latest_contact.contacted_at if latest_contact else None,
        "next_followup_at": latest_contact.next_followup_at if latest_contact else None,
    }


def _pipeline_out(item: CareerPipelineEntry) -> dict:
    return {
        "id": item.id, "job_url": item.job_url, "company": item.company,
        "role": item.role, "status": item.status,
        "created_at": item.created_at, "updated_at": item.updated_at,
    }


def _comparison_out(item: CareerComparisonSnapshot) -> dict:
    bound = all(entry.get("score_provenance") in {"artifact_bound", "unranked"} for entry in item.entries)
    return {"id": item.id, "entries": item.entries, "created_at": item.created_at,
            "basis": "bound_evaluation_scores_only" if bound else "legacy_report_presence",
            "external_action": "none"}


def _expiry(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _profile_out(profile: CareerCandidateProfile | None) -> dict:
    if profile is None:
        return CandidateProfileIn().model_dump()
    return {
        "full_name": profile.full_name, "headline": profile.headline,
        "location": profile.location, "target_roles": profile.target_roles,
        "skills": profile.skills, "resume_markdown": profile.resume_markdown,
    }


@router.get("/profile")
async def get_candidate_profile(
    user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    profile = await db.get(CareerCandidateProfile, user_id)
    return _profile_out(profile)


@router.put("/profile")
async def save_candidate_profile(
    body: CandidateProfileIn, user_id: str = Depends(career_user),
    db: AsyncSession = Depends(get_db),
):
    profile = await db.get(CareerCandidateProfile, user_id)
    if profile is None:
        profile = CareerCandidateProfile(user_id=user_id)
        db.add(profile)
    for field, value in body.model_dump().items():
        setattr(profile, field, value)
    await db.commit()
    return _profile_out(profile)


@router.post("/applications/{application_id}/feedback")
async def record_candidate_feedback(
    application_id: str, body: StageFeedbackIn,
    user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    if body.stage not in STAGES:
        raise HTTPException(400, "Invalid stage")
    if len(body.note.strip()) < 3:
        raise HTTPException(400, "A meaningful feedback note is required")
    application = (await db.execute(select(CareerApplication).where(
        CareerApplication.id == application_id, CareerApplication.user_id == user_id,
    ).with_for_update())).scalar_one_or_none()
    if application is None:
        raise HTTPException(404, "Application not found")
    previous = application.stage
    event = CareerStageEvent(
        application_id=application_id, user_id=user_id, from_stage=previous,
        to_stage=body.stage, note=body.note.strip(), source="candidate",
    )
    application.stage = body.stage
    db.add(event)
    await db.commit()
    await db.refresh(application)
    return {"application": _application_out(application), "event_id": event.id,
            "external_submission": "candidate_reported" if body.stage == "applied" else "not_claimed"}


@router.post("/applications/{application_id}/contacts")
async def record_employer_contact(
    application_id: str, body: ContactIn,
    user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    application = (await db.execute(select(CareerApplication).where(
        CareerApplication.id == application_id, CareerApplication.user_id == user_id,
    ))).scalar_one_or_none()
    if application is None:
        raise HTTPException(404, "Application not found")
    if application.stage in {"saved", "closed", "accepted"}:
        raise HTTPException(409, "Employer contact requires an active application")
    if not body.confirmed_contact or len(body.note.strip()) < 3:
        raise HTTPException(400, "Confirm actual contact and include a meaningful note")
    if body.contacted_at.tzinfo is None or (
        body.next_followup_at is not None and body.next_followup_at.tzinfo is None
    ):
        raise HTTPException(400, "Contact and reminder times must include a timezone")
    now = datetime.now(timezone.utc)
    if body.contacted_at > now + timedelta(minutes=5):
        raise HTTPException(400, "Contact cannot be in the future")
    if body.next_followup_at is not None and body.next_followup_at <= body.contacted_at:
        raise HTTPException(400, "Reminder must be after the contact")
    event = CareerContactEvent(
        application_id=application_id, user_id=user_id, channel=body.channel,
        note=body.note.strip(), contacted_at=body.contacted_at,
        next_followup_at=body.next_followup_at,
    )
    db.add(event)
    await db.commit()
    return {"id": event.id, "channel": event.channel, "note": event.note,
            "contacted_at": event.contacted_at, "next_followup_at": event.next_followup_at,
            "source": "candidate_confirmed"}


@router.get("/submission-attempts/{attempt_id}")
async def get_submission_attempt(
    attempt_id: str, user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    attempt = (await db.execute(select(SubmissionAttempt).where(
        SubmissionAttempt.id == attempt_id, SubmissionAttempt.user_id == user_id,
    ))).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(404, "Submission attempt not found")
    application = (await db.execute(select(CareerApplication).where(
        CareerApplication.id == attempt.application_id, CareerApplication.user_id == user_id,
    ))).scalar_one()
    policy = (await db.execute(select(SubmissionAuthorization).where(
        SubmissionAuthorization.id == attempt.authorization_id,
        SubmissionAuthorization.user_id == user_id,
    ))).scalar_one()
    return {
        "id": attempt.id, "status": attempt.status, "site": attempt.site,
        "job_url": application.job_url, "company": application.company,
        "role": application.role, "mode": policy.mode,
        "authorization_expires_at": _expiry(policy.expires_at).isoformat(),
        "authorization_revoked": policy.revoked_at is not None,
        "lease_until": _expiry(attempt.lease_until).isoformat() if attempt.lease_until else None,
        "user_id": user_id,
    }


@router.post("/submission-attempts/{attempt_id}/claim")
async def claim_submission_attempt(
    attempt_id: str, body: ClaimIn, user_id: str = Depends(career_user),
    db: AsyncSession = Depends(get_db),
):
    attempt = (await db.execute(select(SubmissionAttempt).where(
        SubmissionAttempt.id == attempt_id, SubmissionAttempt.user_id == user_id,
    ).with_for_update())).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(404, "Submission attempt not found")
    now = datetime.now(timezone.utc)
    if attempt.status == "running" and attempt.lease_until and _expiry(attempt.lease_until) <= now:
        attempt.status = "uncertain"
        attempt.error = "Worker lease expired; external submission outcome is unknown"
        await db.commit()
        raise HTTPException(409, "Previous attempt outcome is uncertain; do not retry automatically")
    if attempt.status != "queued":
        raise HTTPException(409, "Submission attempt is not queued")
    await db.execute(select(CareerApplication.id).where(
        CareerApplication.id == attempt.application_id, CareerApplication.user_id == user_id,
    ).with_for_update())
    other_active = (await db.execute(select(SubmissionAttempt.id).where(
        SubmissionAttempt.user_id == user_id,
        SubmissionAttempt.application_id == attempt.application_id,
        SubmissionAttempt.id != attempt.id,
        SubmissionAttempt.status.in_(("running", "uncertain", "submitted")),
    ).limit(1))).scalar_one_or_none()
    if other_active:
        raise HTTPException(409, "Another attempt for this application is active or unresolved")
    policy = (await db.execute(select(SubmissionAuthorization).where(
        SubmissionAuthorization.id == attempt.authorization_id,
        SubmissionAuthorization.user_id == user_id,
    ).with_for_update())).scalar_one()
    if policy.revoked_at is not None or _expiry(policy.expires_at) <= now:
        raise HTTPException(403, "Submission authorization is revoked or expired")
    token = secrets.token_urlsafe(48)
    attempt.status = "running"
    attempt.browser_session_id = body.browser_session_id
    attempt.claim_token_hash = hashlib.sha256(token.encode()).hexdigest()
    attempt.lease_until = now + timedelta(minutes=10)
    await db.commit()
    return {"id": attempt.id, "status": "running", "claim_token": token,
            "lease_until": attempt.lease_until.isoformat()}


@router.post("/submission-attempts/{attempt_id}/finish")
async def finish_submission_attempt(
    attempt_id: str, body: FinishIn, user_id: str = Depends(career_user),
    db: AsyncSession = Depends(get_db),
):
    attempt = (await db.execute(select(SubmissionAttempt).where(
        SubmissionAttempt.id == attempt_id, SubmissionAttempt.user_id == user_id,
    ).with_for_update())).scalar_one_or_none()
    if attempt is None or attempt.status != "running" or not attempt.claim_token_hash:
        raise HTTPException(409, "Submission attempt is not running")
    digest = hashlib.sha256(body.claim_token.encode()).hexdigest()
    if not secrets.compare_digest(digest, attempt.claim_token_hash):
        raise HTTPException(403, "Invalid worker claim")
    if body.status == "submitted":
        if not body.receipt_url or site_for_url(body.receipt_url) != attempt.site:
            raise HTTPException(400, "A receipt on the authorized site is required")
        evidence = (body.confirmation_text or "").casefold()
        if not any(phrase in evidence for phrase in (
            "application submitted", "application sent", "thank you for applying",
            "your application has been received",
        )):
            raise HTTPException(400, "A recognizable submission confirmation is required")
        application = (await db.execute(select(CareerApplication).where(
            CareerApplication.id == attempt.application_id, CareerApplication.user_id == user_id,
        ))).scalar_one()
        application.stage = "applied"
        attempt.receipt = {"url": body.receipt_url, "confirmation_text": body.confirmation_text}
    attempt.status = body.status
    attempt.error = body.reason
    attempt.claim_token_hash = None
    attempt.lease_until = None
    await db.commit()
    return {"id": attempt.id, "status": attempt.status,
            "external_submission": "verified" if attempt.status == "submitted" else "not_verified"}


@router.post("/submission-attempts/{attempt_id}/resolve")
async def resolve_uncertain_attempt(
    attempt_id: str, body: ResolveIn, user_id: str = Depends(career_user),
    db: AsyncSession = Depends(get_db),
):
    attempt = (await db.execute(select(SubmissionAttempt).where(
        SubmissionAttempt.id == attempt_id, SubmissionAttempt.user_id == user_id,
    ).with_for_update())).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(404, "Submission attempt not found")
    if attempt.status != "uncertain" or not body.confirmed_not_submitted:
        raise HTTPException(409, "Only an uncertain attempt with explicit candidate verification can be cleared")
    attempt.status = "not_submitted"
    attempt.error = "Candidate verified no application was sent: " + body.note
    attempt.claim_token_hash = None
    attempt.lease_until = None
    await db.commit()
    return {"id": attempt.id, "status": attempt.status}


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
    feedback = (await db.execute(select(CareerStageEvent).where(
        CareerStageEvent.application_id == application_id, CareerStageEvent.user_id == user_id,
    ).order_by(CareerStageEvent.created_at.desc()))).scalars().all()
    contacts = (await db.execute(select(CareerContactEvent).where(
        CareerContactEvent.application_id == application_id, CareerContactEvent.user_id == user_id,
    ).order_by(CareerContactEvent.contacted_at.desc(), CareerContactEvent.created_at.desc()))).scalars().all()
    return {
        "artifacts": [{"id": item.id, "kind": item.kind, "source": item.source,
                       "version": item.version, "content_markdown": item.content_markdown}
                      for item in artifacts],
        "submission_attempts": [{"id": item.id, "site": item.site, "status": item.status,
                                 "receipt": item.receipt, "error": item.error}
                                for item in attempts],
        "feedback": [{"id": item.id, "from_stage": item.from_stage,
                      "to_stage": item.to_stage, "note": item.note,
                      "source": item.source, "created_at": item.created_at}
                     for item in feedback],
        "contacts": [{"id": item.id, "channel": item.channel, "note": item.note,
                      "contacted_at": item.contacted_at, "next_followup_at": item.next_followup_at,
                      "source": "candidate_confirmed"} for item in contacts],
    }


@router.get("/applications")
async def list_applications(user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db)):
    # A crashed worker cannot hold a job in "running" forever. Its external
    # outcome is unknown, so require candidate verification before another try.
    expired = (await db.execute(select(SubmissionAttempt).where(
        SubmissionAttempt.user_id == user_id,
        SubmissionAttempt.status == "running",
        SubmissionAttempt.lease_until <= datetime.now(timezone.utc),
    ))).scalars().all()
    for attempt in expired:
        attempt.status = "uncertain"
        attempt.error = "Worker lease expired; verify the employer site before retrying"
        attempt.claim_token_hash = None
        attempt.lease_until = None
    if expired:
        await db.commit()
    rows = (await db.execute(
        select(CareerApplication).where(CareerApplication.user_id == user_id)
        .order_by(CareerApplication.created_at.desc())
    )).scalars().all()
    blocked_rows = (await db.execute(select(
        SubmissionAttempt.application_id, SubmissionAttempt.id, SubmissionAttempt.status,
    ).where(
        SubmissionAttempt.user_id == user_id,
        SubmissionAttempt.status.in_(("running", "uncertain", "submitted")),
    ).order_by(SubmissionAttempt.created_at.desc()))).all()
    blocked = {}
    for application_id, attempt_id, status in blocked_rows:
        blocked.setdefault(application_id, (attempt_id, status))
    contact_rows = (await db.execute(select(CareerContactEvent).where(
        CareerContactEvent.user_id == user_id,
    ).order_by(CareerContactEvent.contacted_at.desc(), CareerContactEvent.created_at.desc()))).scalars().all()
    latest_contacts = {}
    for contact in contact_rows:
        latest_contacts.setdefault(contact.application_id, contact)
    return [_application_out(row, blocking_attempt=blocked.get(row.id),
                             latest_contact=latest_contacts.get(row.id)) for row in rows]


@router.get("/pipeline")
async def list_pipeline(user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CareerPipelineEntry).where(
        CareerPipelineEntry.user_id == user_id,
    ).order_by(CareerPipelineEntry.created_at.desc()))).scalars().all()
    return [_pipeline_out(row) for row in rows]


@router.get("/comparisons")
async def list_comparisons(user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CareerComparisonSnapshot).where(
        CareerComparisonSnapshot.user_id == user_id,
    ).order_by(CareerComparisonSnapshot.created_at.desc(), CareerComparisonSnapshot.id.desc()).limit(20))).scalars().all()
    return [_comparison_out(row) for row in rows]


@router.post("/comparisons")
async def create_comparison(
    body: ComparisonIn, user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    ids = [str(value) for value in body.application_ids]
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "Choose distinct applications")
    applications = (await db.execute(select(CareerApplication).where(
        CareerApplication.user_id == user_id, CareerApplication.id.in_(ids),
    ))).scalars().all()
    if len(applications) != len(ids):
        raise HTTPException(404, "One or more applications were not found")
    bound_ids = [item.evaluation_artifact_id for item in applications if item.evaluation_artifact_id]
    evaluations = (await db.execute(select(CareerArtifact).where(
        CareerArtifact.user_id == user_id, CareerArtifact.id.in_(bound_ids),
        CareerArtifact.kind == "evaluation", CareerArtifact.source == "career-ops",
    ))).scalars().all()
    bound_evaluation = {artifact.id: artifact for artifact in evaluations}
    entries = []
    for application in applications:
        evidence = bound_evaluation.get(application.evaluation_artifact_id)
        ranked_score = application.fit_score if (
            application.score_kind == "career_ops_evaluation" and evidence is not None
            and evidence.application_id == application.id
        ) else None
        entries.append({
            "application_id": application.id, "company": application.company,
            "role": application.role, "stage": application.stage,
            "fit_score": ranked_score, "score_kind": application.score_kind,
            "evaluation_artifact_id": evidence.id if ranked_score is not None else None,
            "evaluation_version": evidence.version if ranked_score is not None else None,
            "score_evidence": parse_score_evidence(evidence.content_markdown) if ranked_score is not None else None,
            "requirement_evidence": parse_requirement_evidence(evidence.content_markdown)
            if ranked_score is not None else None,
            "score_provenance": "artifact_bound" if ranked_score is not None else "unranked",
            "rank": None,
        })
    entries.sort(key=lambda item: (
        item["fit_score"] is None, -(item["fit_score"] or 0),
        item["company"].casefold(), item["role"].casefold(), item["application_id"],
    ))
    for rank, entry in enumerate((item for item in entries if item["fit_score"] is not None), start=1):
        entry["rank"] = rank
    snapshot = CareerComparisonSnapshot(user_id=user_id, entries=entries)
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return _comparison_out(snapshot)


@router.post("/pipeline")
async def queue_pipeline_url(
    body: PipelineIn, user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    try:
        url = canonical_url(body.job_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    url_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    row = (await db.execute(select(CareerPipelineEntry).where(
        CareerPipelineEntry.user_id == user_id, CareerPipelineEntry.url_key == url_key,
    ))).scalar_one_or_none()
    if row is None:
        row = CareerPipelineEntry(
            user_id=user_id, url_key=url_key, job_url=url,
            company=body.company.strip(), role=body.role.strip(), status="pending",
        )
        db.add(row)
    else:
        row.company = body.company.strip() or row.company
        row.role = body.role.strip() or row.role
        # An evaluated URL must not return to the pending inbox on a re-save.
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Job URL was concurrently queued; retry") from exc
    await db.refresh(row)
    return _pipeline_out(row)


@router.get("/applications/{application_id}")
async def get_application(
    application_id: str, user_id: str = Depends(career_user),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(CareerApplication).where(
        CareerApplication.id == application_id, CareerApplication.user_id == user_id,
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Application not found")
    contact = (await db.execute(select(CareerContactEvent).where(
        CareerContactEvent.application_id == application_id, CareerContactEvent.user_id == user_id,
    ).order_by(CareerContactEvent.contacted_at.desc(), CareerContactEvent.created_at.desc()).limit(1))).scalar_one_or_none()
    return _application_out(row, latest_contact=contact)


@router.post("/applications")
async def upsert_application(
    body: ApplicationIn, user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    if body.stage not in STAGES or not set(body.sources).issubset(SOURCE_NAMES):
        raise HTTPException(400, "Invalid stage or source")
    if body.score_kind == "career_ops_evaluation" and (
        "career-ops" not in body.sources or body.fit_score is None
    ):
        raise HTTPException(400, "A career-ops evaluation requires its source and score")
    if body.score_kind == "career_ops_import" and "career-ops" not in body.sources:
        raise HTTPException(400, "An imported score requires its career-ops source")
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
            score_kind=body.score_kind, sources=sorted(set(body.sources)),
            notes=list(dict.fromkeys(body.notes)),
        )
        db.add(row)
    else:
        row.sources = sorted(set(row.sources or []) | set(body.sources))
        row.notes = list(dict.fromkeys((row.notes or []) + body.notes))
        priority = {"saved": 1, "applied": 2, "screening": 3, "assessment": 4,
                    "interview": 5, "offer": 6, "accepted": 7, "closed": 8}
        if priority[body.stage] > priority[row.stage]:
            row.stage = body.stage
        # A career-ops global evaluation supersedes a preliminary resume
        # overlap score, including when the evaluation is less favorable or
        # the imported tracker has no score. A later search-only sync must not
        # silently restore that old score.
        if body.score_kind == "career_ops_evaluation":
            row.fit_score = body.fit_score
            row.score_kind = body.score_kind
            row.evaluation_artifact_id = None
        elif body.score_kind == "career_ops_import" and row.score_kind != "career_ops_evaluation":
            row.fit_score = body.fit_score
            row.score_kind = body.score_kind
        elif body.fit_score is not None and row.score_kind not in {"career_ops_evaluation", "career_ops_import"}:
            row.fit_score = max(row.fit_score or 0, body.fit_score)
            row.score_kind = body.score_kind
    if url:
        url_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        inbox = (await db.execute(select(CareerPipelineEntry).where(
            CareerPipelineEntry.user_id == user_id, CareerPipelineEntry.url_key == url_key,
        ))).scalar_one_or_none()
        processed = row.stage != "saved" or row.score_kind == "career_ops_evaluation"
        if inbox is None:
            db.add(CareerPipelineEntry(
                user_id=user_id, url_key=url_key, job_url=url,
                company=row.company, role=row.role,
                status="processed" if processed else "pending",
            ))
        elif processed:
            inbox.status = "processed"
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Application was concurrently imported; retry") from exc
    await db.refresh(row)
    return _application_out(row)


@router.post("/applications/{application_id}/evaluation-score")
async def bind_evaluation_score(
    application_id: str, body: EvaluationScoreIn,
    user_id: str = Depends(career_user), db: AsyncSession = Depends(get_db),
):
    """Atomically score a role from one owned report; no caller-supplied score."""
    application = (await db.execute(select(CareerApplication).where(
        CareerApplication.id == application_id, CareerApplication.user_id == user_id,
    ))).scalar_one_or_none()
    artifact = (await db.execute(select(CareerArtifact).where(
        CareerArtifact.id == str(body.artifact_id), CareerArtifact.application_id == application_id,
        CareerArtifact.user_id == user_id, CareerArtifact.kind == "evaluation",
        CareerArtifact.source == "career-ops",
    ))).scalar_one_or_none()
    if application is None or artifact is None:
        raise HTTPException(404, "Application or evaluation report not found")
    score_matches = re.findall(r"^\*\*Score:\*\*\s*([1-5](?:\.\d+)?)\s*/\s*5\s*$",
                               artifact.content_markdown, flags=re.MULTILINE)
    if len(score_matches) != 1 or not 1 <= float(score_matches[0]) <= 5:
        raise HTTPException(400, "Evaluation report has no valid 1-5 score")
    if parse_score_evidence(artifact.content_markdown) is None:
        raise HTTPException(400, "Evaluation report has no complete score evidence")
    application.fit_score = round(float(score_matches[0]) * 20, 2)
    application.score_kind = "career_ops_evaluation"
    application.evaluation_artifact_id = artifact.id
    application.sources = sorted(set(application.sources or []) | {"career-ops"})
    await db.commit()
    await db.refresh(application)
    return _application_out(application)


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
    if policy.mode == "auto" and (
        application.score_kind != "career_ops_evaluation"
        or application.evaluation_artifact_id is None
        or application.fit_score is None or application.fit_score < policy.min_fit_score
    ):
        raise HTTPException(403, "Job does not meet the authorized fit threshold")
    blocking_attempt = (await db.execute(select(SubmissionAttempt.id).where(
        SubmissionAttempt.user_id == user_id,
        SubmissionAttempt.application_id == application_id,
        SubmissionAttempt.status.in_(("running", "uncertain", "submitted")),
    ).limit(1))).scalar_one_or_none()
    if blocking_attempt:
        raise HTTPException(409, "A running, uncertain, or submitted application must be resolved first")
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
