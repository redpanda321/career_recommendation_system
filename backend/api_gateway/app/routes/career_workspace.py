"""Interoperability boundary for local job-search workspaces.

This router deliberately accepts exported records in the request body.  It never
opens AI Job Search, Career Ops, or Proficiently profile/tracker files, and it never submits an
application to an employer.  That keeps the local projects the system of record
while allowing CareerCompassAI to present one recommendation workspace.
"""

from collections import Counter
import json
import os
from typing import List, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.routes.career_auth import career_user


router = APIRouter(tags=["Career workspace"])


class AiJobSearchRecord(BaseModel):
    """A row from an explicitly exported ``job_search_tracker.csv`` file."""

    company: str
    role: str
    status: str = "saved"
    fit_rating: Optional[float] = None
    date: Optional[str] = None
    sector: Optional[str] = None
    role_type: Optional[str] = None
    channel: Optional[str] = None
    contact_person: Optional[str] = None
    notes: Optional[str] = None
    source: Optional[str] = None
    deadline: Optional[str] = None
    job_url: Optional[str] = None


class CareerOpsRecord(BaseModel):
    """A user-exported Career Ops tracker record.

    Career Ops is intentionally flexible, so this is a small portable contract
    instead of a parser for its private Markdown tracker.
    """

    company: str
    role: str
    status: str = "saved"
    score: Optional[float] = None
    date: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    job_url: Optional[str] = None
    tracker_url: Optional[str] = None


class ProficientlyRecord(BaseModel):
    """A user-exported Proficiently job folder or match."""

    company: str
    role: str
    status: str = "saved"
    date: Optional[str] = None
    location: Optional[str] = None
    job_url: Optional[str] = None
    notes: Optional[str] = None


class WorkspaceImportRequest(BaseModel):
    ai_job_search: List[AiJobSearchRecord] = Field(default_factory=list)
    career_ops: List[CareerOpsRecord] = Field(default_factory=list)
    proficiently: List[ProficientlyRecord] = Field(default_factory=list)


class UnifiedApplication(BaseModel):
    company: str
    role: str
    stage: str
    sources: List[str]
    fit_score: Optional[float] = None
    date: Optional[str] = None
    deadline: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    notes: List[str] = Field(default_factory=list)


class WorkspacePreview(BaseModel):
    applications: List[UnifiedApplication]
    source_counts: dict
    duplicate_count: int
    safety_notice: str


STAGE_ALIASES = {
    "saved": "saved", "new": "saved", "ranked": "saved", "drafted": "saved",
    "researching": "saved", "evaluated": "saved", "applied": "applied",
    "submitted": "applied", "sent": "applied", "in progress": "applied",
    "responded": "screening", "screening": "screening", "assessment": "assessment",
    "interview": "interview", "interviewing": "interview", "offer": "offer",
    "accepted": "accepted", "hired": "accepted", "rejected": "closed",
    "withdrawn": "closed", "declined": "closed", "expired": "closed",
    "skipped": "closed", "skip": "closed", "discarded": "closed",
    "no response": "closed",
}

STAGE_PRIORITY = {
    "saved": 1, "applied": 2, "screening": 3, "assessment": 4,
    "interview": 5, "offer": 6, "accepted": 7, "closed": 8,
}


def _stage(value: str) -> str:
    return STAGE_ALIASES.get(value.strip().lower(), "saved")


def _score(value: Optional[float], five_point_scale: bool = False) -> Optional[float]:
    if value is None:
        return None
    if five_point_scale:
        value *= 20
    return max(0, min(100, round(value, 1)))


def _key(record: UnifiedApplication) -> str:
    if record.url:
        parsed = urlsplit(record.url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return f"url::{parsed.hostname.casefold()}::{parsed.path.rstrip('/')}::{parsed.query}"
    return "::".join(
        part.strip().casefold() for part in (record.company, record.role, record.location or "")
    )


def _merge(records: List[UnifiedApplication]) -> tuple[List[UnifiedApplication], int]:
    merged = {}
    duplicates = 0
    for record in records:
        key = _key(record)
        existing = merged.get(key)
        if existing is None:
            merged[key] = record
            continue
        duplicates += 1
        existing.sources = sorted(set(existing.sources + record.sources))
        existing.notes = list(dict.fromkeys(existing.notes + record.notes))
        existing.fit_score = max(
            (score for score in (existing.fit_score, record.fit_score) if score is not None),
            default=None,
        )
        if STAGE_PRIORITY[record.stage] > STAGE_PRIORITY[existing.stage]:
            existing.stage = record.stage
        existing.date = existing.date or record.date
        existing.deadline = existing.deadline or record.deadline
        existing.location = existing.location or record.location
        existing.url = existing.url or record.url
    return list(merged.values()), duplicates


def build_workspace_preview(payload: WorkspaceImportRequest) -> WorkspacePreview:
    """Normalize three voluntary exports into a single, non-persisted workspace."""
    records: List[UnifiedApplication] = []
    for item in payload.ai_job_search:
        records.append(UnifiedApplication(
            company=item.company, role=item.role, stage=_stage(item.status),
            sources=["ai-job-search"], fit_score=_score(item.fit_rating), date=item.date,
            deadline=item.deadline, url=item.job_url or item.source,
            notes=[note for note in (item.notes, item.contact_person, item.channel) if note],
        ))
    for item in payload.career_ops:
        # Career Ops scores are commonly rubric values from 0 through 5. Values
        # above 5 are already percentages and are retained as such.
        records.append(UnifiedApplication(
            company=item.company, role=item.role, stage=_stage(item.status),
            sources=["career-ops"], fit_score=_score(item.score, bool(item.score is not None and item.score <= 5)),
            date=item.date, location=item.location, url=item.job_url or item.tracker_url,
            notes=[item.notes] if item.notes else [],
        ))
    for item in payload.proficiently:
        records.append(UnifiedApplication(
            company=item.company, role=item.role, stage=_stage(item.status),
            sources=["proficiently"], date=item.date, location=item.location,
            url=item.job_url, notes=[item.notes] if item.notes else [],
        ))
    applications, duplicates = _merge(records)
    applications.sort(key=lambda item: (STAGE_PRIORITY[item.stage], item.fit_score or -1), reverse=True)
    return WorkspacePreview(
        applications=applications,
        source_counts=dict(Counter(source for item in records for source in item.sources)),
        duplicate_count=duplicates,
        safety_notice="Preview only: no local tracker is read or changed, and no application is submitted.",
    )


@router.post("/preview", response_model=WorkspacePreview)
async def preview_workspace_import(
    payload: WorkspaceImportRequest, _user: str = Depends(career_user),
) -> WorkspacePreview:
    """Preview a unified job-search workspace from user-provided exports."""
    return build_workspace_preview(payload)


JOB_CAREER_SERVICE_URL = os.getenv(
    "JOB_CAREER_SERVICE_URL", "http://localhost:8004/career-workspace"
).rstrip("/")


async def _forward(request: Request, path: str, method: str, body: dict | None = None):
    """Forward only explicitly registered paths to the private canonical store."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method, f"{JOB_CAREER_SERVICE_URL}/{path}",
                headers={"Authorization": request.headers["Authorization"]},
                json=body,
            )
    except httpx.RequestError as exc:
        raise HTTPException(503, "Career workspace store is unavailable") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Career workspace store returned an invalid response") from exc
    from fastapi.responses import JSONResponse
    return JSONResponse(payload, status_code=response.status_code)


@router.get("/applications")
async def list_canonical_applications(request: Request, _user: str = Depends(career_user)):
    return await _forward(request, "applications", "GET")


@router.post("/applications")
async def save_canonical_application(request: Request, _user: str = Depends(career_user)):
    return await _forward(request, "applications", "POST", await request.json())


@router.get("/applications/{application_id}/activity")
async def canonical_application_activity(application_id: str, request: Request, _user: str = Depends(career_user)):
    return await _forward(request, f"applications/{application_id}/activity", "GET")


@router.post("/import")
async def import_career_records(
    payload: WorkspaceImportRequest, request: Request, _user: str = Depends(career_user),
):
    """Idempotently import the three adapter formats into the framework store."""
    preview = build_workspace_preview(payload)
    imported = []
    for item in preview.applications:
        response = await _forward(request, "applications", "POST", {
            "company": item.company, "role": item.role, "location": item.location,
            "job_url": item.url, "stage": item.stage, "fit_score": item.fit_score,
            "sources": item.sources, "notes": item.notes,
        })
        if response.status_code >= 400:
            raise HTTPException(response.status_code, json.loads(response.body))
        imported.append(json.loads(response.body)["id"])
    return {"application_ids": imported, "duplicate_count": preview.duplicate_count,
            "source_counts": preview.source_counts}


@router.post("/applications/{application_id}/artifacts")
async def save_career_artifact(application_id: str, request: Request, _user: str = Depends(career_user)):
    return await _forward(request, f"applications/{application_id}/artifacts", "POST", await request.json())


@router.post("/authorizations")
async def create_submission_authorization(request: Request, _user: str = Depends(career_user)):
    return await _forward(request, "authorizations", "POST", await request.json())


@router.post("/authorizations/{authorization_id}/revoke")
async def revoke_submission_authorization(authorization_id: str, request: Request, _user: str = Depends(career_user)):
    return await _forward(request, f"authorizations/{authorization_id}/revoke", "POST")


@router.post("/applications/{application_id}/submission-attempts")
async def create_submission_attempt(application_id: str, request: Request, _user: str = Depends(career_user)):
    return await _forward(request, f"applications/{application_id}/submission-attempts", "POST", await request.json())
