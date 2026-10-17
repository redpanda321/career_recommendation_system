import uuid
from sqlalchemy import (
    Column,
    String,
    Integer,
    ARRAY,
    DateTime,
    Float,
    ForeignKey,
    JSON,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class Job(Base):
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    title = Column(String, nullable=False, index=True)
    description = Column(String, nullable=False)
    salary = Column(Integer)
    location = Column(String)
    tags = Column(ARRAY(String))
    jobType = Column(String)
    companyName = Column(String)
    contactEmail = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CareerApplication(Base):
    """The Career Recommendations system of record for one candidate and role."""

    __tablename__ = "career_applications"
    __table_args__ = (
        UniqueConstraint("user_id", "canonical_key", name="uq_career_user_job"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(80), nullable=False, index=True)
    canonical_key = Column(String(500), nullable=False)
    company = Column(String(200), nullable=False)
    role = Column(String(200), nullable=False)
    location = Column(String(200))
    job_url = Column(Text)
    stage = Column(String(32), nullable=False, default="saved")
    fit_score = Column(Float)
    sources = Column(JSON, nullable=False, default=list)
    notes = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CareerCandidateProfile(Base):
    """Candidate-owned facts shared by the career workflow, never site credentials."""

    __tablename__ = "career_candidate_profiles"

    user_id = Column(String(80), primary_key=True)
    full_name = Column(String(200), nullable=False, default="")
    headline = Column(String(300), nullable=False, default="")
    location = Column(String(200), nullable=False, default="")
    target_roles = Column(JSON, nullable=False, default=list)
    skills = Column(JSON, nullable=False, default=list)
    resume_markdown = Column(Text, nullable=False, default="")
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CareerStageEvent(Base):
    """Append-only candidate feedback; not proof of an external submission."""

    __tablename__ = "career_stage_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id = Column(
        String(36), ForeignKey("career_applications.id"), nullable=False, index=True
    )
    user_id = Column(String(80), nullable=False, index=True)
    from_stage = Column(String(32), nullable=False)
    to_stage = Column(String(32), nullable=False)
    note = Column(Text, nullable=False)
    source = Column(String(32), nullable=False, default="candidate")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CareerContactEvent(Base):
    """Candidate-confirmed employer contact, separate from a review-only draft."""

    __tablename__ = "career_contact_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id = Column(
        String(36), ForeignKey("career_applications.id"), nullable=False, index=True
    )
    user_id = Column(String(80), nullable=False, index=True)
    channel = Column(String(20), nullable=False)
    note = Column(Text, nullable=False)
    contacted_at = Column(DateTime(timezone=True), nullable=False)
    next_followup_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CareerArtifact(Base):
    """Immutable, versioned output from an adapter or a candidate edit."""

    __tablename__ = "career_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "application_id", "kind", "version", name="uq_career_artifact_version"
        ),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id = Column(
        String(36), ForeignKey("career_applications.id"), nullable=False, index=True
    )
    user_id = Column(String(80), nullable=False, index=True)
    kind = Column(String(40), nullable=False)
    source = Column(String(40), nullable=False)
    version = Column(Integer, nullable=False)
    content_markdown = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SubmissionAuthorization(Base):
    """Explicit, bounded candidate consent for one application or a campaign."""

    __tablename__ = "submission_authorizations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(80), nullable=False, index=True)
    mode = Column(String(20), nullable=False)  # confirm_each | auto
    application_id = Column(String(36), ForeignKey("career_applications.id"))
    allowed_sites = Column(JSON, nullable=False, default=list)
    max_applications = Column(Integer, nullable=False)
    used_applications = Column(Integer, nullable=False, default=0)
    min_fit_score = Column(Float, nullable=False, default=80)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SubmissionAttempt(Base):
    """A durable attempt; only a verified external receipt means submitted."""

    __tablename__ = "submission_attempts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_career_submission_idempotency"
        ),
        UniqueConstraint(
            "authorization_id",
            "application_id",
            name="uq_career_authorization_application",
        ),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(80), nullable=False, index=True)
    application_id = Column(
        String(36), ForeignKey("career_applications.id"), nullable=False, index=True
    )
    authorization_id = Column(
        String(36), ForeignKey("submission_authorizations.id"), nullable=False
    )
    idempotency_key = Column(String(120), nullable=False)
    site = Column(String(40), nullable=False)
    status = Column(String(24), nullable=False, default="queued")
    claim_token_hash = Column(String(64))
    lease_until = Column(DateTime(timezone=True))
    browser_session_id = Column(String(100))
    receipt = Column(JSON)
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
