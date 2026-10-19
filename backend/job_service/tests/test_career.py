"""Cross-user, consent and idempotency checks for the canonical career store."""

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-career-bridge-secret")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import AsyncSessionLocal, Base, engine
from app.database import ensure_career_outcome_kind, ensure_career_score_kind
from app.models import (
    CareerApplication, CareerArtifact, CareerCandidateProfile, CareerComparisonSnapshot,
    CareerContactEvent, CareerPipelineEntry, CareerReplyEvent, CareerStageEvent,
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
                CareerCandidateProfile.__table__, CareerStageEvent.__table__, CareerContactEvent.__table__,
                CareerReplyEvent.__table__,
                CareerPipelineEntry.__table__, CareerComparisonSnapshot.__table__,
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


def test_pipeline_is_database_owned_and_user_isolated(monkeypatch, tmp_path):
    # A leftover file-root setting must not redirect the canonical write path.
    monkeypatch.setenv("CAREER_OPS_DATA_ROOT", str(tmp_path / "unused"))
    with TestClient(app) as client:
        url = "https://jobs.example.com/acme/991"
        queued = client.post("/career-workspace/pipeline", headers=headers(991), json={
            "job_url": url, "company": "Acme", "role": "Engineer",
        })
        assert queued.status_code == 200, queued.text
        repeated = client.post("/career-workspace/pipeline", headers=headers(991), json={
            "job_url": url, "company": "Acme", "role": "Engineer",
        })
        assert repeated.status_code == 200 and repeated.json()["id"] == queued.json()["id"]
        assert len(client.get("/career-workspace/pipeline", headers=headers(991)).json()) == 1
        assert client.get("/career-workspace/pipeline", headers=headers(992)).json() == []

        evaluated = client.post("/career-workspace/applications", headers=headers(991), json={
            "company": "Acme", "role": "Engineer", "job_url": url,
            "fit_score": 88, "score_kind": "career_ops_evaluation", "sources": ["career-ops"],
        })
        assert evaluated.status_code == 200, evaluated.text
        assert client.get("/career-workspace/pipeline", headers=headers(991)).json()[0]["status"] == "processed"
        assert not (tmp_path / "unused").exists()

        saved_profile = client.put("/career-workspace/profile", headers=headers(991), json={
            "full_name": "Alex", "resume_markdown": "# CV",
        })
        assert saved_profile.status_code == 200
        assert client.get("/career-workspace/profile", headers=headers(991)).json()["full_name"] == "Alex"
        feedback = client.post(
            f"/career-workspace/applications/{evaluated.json()['id']}/feedback",
            headers=headers(991), json={"stage": "applied", "note": "Submitted myself"},
        )
        assert feedback.status_code == 200
        material = client.post(
            f"/career-workspace/applications/{evaluated.json()['id']}/artifacts",
            headers=headers(991), json={
                "kind": "cover_letter", "source": "career-ops", "content_markdown": "# Reviewed",
            },
        )
        assert material.status_code == 200
        activity = client.get(
            f"/career-workspace/applications/{evaluated.json()['id']}/activity",
            headers=headers(991),
        ).json()
        assert activity["feedback"][0]["note"] == "Submitted myself"
        assert activity["artifacts"][0]["content_markdown"] == "# Reviewed"
        assert not (tmp_path / "unused").exists()


def test_comparison_snapshots_are_owned_immutable_and_rank_only_reported_scores():
    with TestClient(app) as client:
        owner = headers(2001)
        first = client.post("/career-workspace/applications", headers=owner, json={
            "company": "Alpha", "role": "Engineer", "fit_score": 10,
            "score_kind": "career_ops_evaluation", "sources": ["career-ops"],
        }).json()
        second = client.post("/career-workspace/applications", headers=owner, json={
            "company": "Beta", "role": "Engineer", "fit_score": 20,
            "score_kind": "career_ops_evaluation", "sources": ["career-ops"],
        }).json()
        preliminary = client.post("/career-workspace/applications", headers=owner, json={
            "company": "Gamma", "role": "Engineer", "fit_score": 99,
            "score_kind": "resume_overlap", "sources": ["ai-job-search"],
        }).json()
        missing_report = client.post("/career-workspace/applications", headers=owner, json={
            "company": "Delta", "role": "Engineer", "fit_score": 97,
            "score_kind": "career_ops_evaluation", "sources": ["career-ops"],
        }).json()
        outsider = client.post("/career-workspace/applications", headers=headers(2002), json={
            "company": "Private", "role": "Engineer",
        }).json()
        for application, report_score in ((first, "4.25"), (second, "4.6")):
            response = client.post(
                f"/career-workspace/applications/{application['id']}/artifacts",
                headers=owner, json={"kind": "evaluation", "source": "career-ops",
                                     "content_markdown": f"**Score:** {report_score}/5\n"
                                     "## B) Match with CV\n"
                                     "| Requirement | Importance | Match | JD signal | Evidence / gap |\n"
                                     "| --- | --- | --- | --- | --- |\n"
                                     "| Python services | high (structural) | ✅ Strong | Responsibilities: Python services | CV: Built Python APIs |\n"
                                     "## C) Level and Strategy\nContext.\n## Score Evidence\n"
                                     "| Dimension | Status | Observation | Open question |\n"
                                     "| CV match | supported | Resume names Python. | Confirm depth. |\n"
                                     "| North Star alignment | partial | Target role matches. | Confirm scope. |\n"
                                     "| Compensation | unknown | Pay missing. | Ask salary. |\n"
                                     "| Cultural signals | unknown | Culture missing. | Ask team. |\n"
                                     "| Red flags | partial | Posting age unknown. | Check liveness. |"},
            )
            assert response.status_code == 200, response.text
            bound = client.post(
                f"/career-workspace/applications/{application['id']}/evaluation-score",
                headers=owner, json={"artifact_id": response.json()["id"]},
            )
            assert bound.status_code == 200, bound.text
            assert bound.json()["fit_score"] == float(report_score) * 20
            assert bound.json()["evaluation_artifact_id"] == response.json()["id"]

        endpoint = "/career-workspace/comparisons"
        selected = [preliminary["id"], first["id"], second["id"], missing_report["id"]]
        assert client.post(endpoint, headers=owner, json={
            "application_ids": selected + [first["id"], second["id"]],
        }).status_code == 422
        assert client.post(endpoint, headers=owner, json={
            "application_ids": [first["id"], first["id"]],
        }).status_code == 400
        assert client.post(endpoint, headers=owner, json={
            "application_ids": [first["id"], outsider["id"]],
        }).status_code == 404
        response = client.post(endpoint, headers=owner, json={"application_ids": selected})
        assert response.status_code == 200, response.text
        snapshot = response.json()
        assert snapshot["basis"] == "bound_evaluation_scores_only"
        assert snapshot["external_action"] == "none"
        assert [(entry["company"], entry["rank"], entry["fit_score"])
                for entry in snapshot["entries"]] == [
            ("Beta", 1, 92), ("Alpha", 2, 85),
            ("Delta", None, None), ("Gamma", None, None),
        ]
        assert all(entry["evaluation_artifact_id"] for entry in snapshot["entries"][:2])
        assert all(entry["score_provenance"] == "artifact_bound" for entry in snapshot["entries"][:2])
        assert all(entry["source_provenance"] == "legacy_unverified" for entry in snapshot["entries"][:2])
        assert all(len(entry["score_evidence"]) == 5 for entry in snapshot["entries"][:2])
        assert all(entry["requirement_evidence"][0]["requirement"] == "Python services"
                   for entry in snapshot["entries"][:2])
        assert snapshot["entries"][0]["score_evidence"][2]["status"] == "unknown"
        assert all(entry["evaluation_artifact_id"] is None for entry in snapshot["entries"][2:])
        assert all(entry["score_evidence"] is None for entry in snapshot["entries"][2:])
        assert all(entry["requirement_evidence"] is None for entry in snapshot["entries"][2:])
        assert all(entry["source_provenance"] == "unranked" for entry in snapshot["entries"][2:])
        assert client.get(endpoint, headers=headers(2002)).json() == []

        changed = client.post("/career-workspace/applications", headers=owner, json={
            "company": "Alpha", "role": "Engineer", "fit_score": 70,
            "score_kind": "career_ops_evaluation", "sources": ["career-ops"],
        })
        assert changed.status_code == 200
        assert changed.json()["evaluation_artifact_id"] is None
        replacement = client.post(
            f"/career-workspace/applications/{first['id']}/artifacts", headers=owner,
            json={"kind": "evaluation", "source": "career-ops",
                  "content_markdown": "## Score Evidence\nIncomplete replacement report"},
        )
        assert replacement.status_code == 200
        saved = client.get(endpoint, headers=owner).json()
        assert saved[0]["id"] == snapshot["id"]
        assert saved[0]["entries"] == snapshot["entries"]


def test_legacy_comparison_is_not_relabelled_as_artifact_bound():
    from app.routers.career import _comparison_out

    historical = CareerComparisonSnapshot(entries=[{"rank": 1, "fit_score": 85}])
    assert _comparison_out(historical)["basis"] == "legacy_report_presence"


def test_evaluation_score_binding_rejects_cross_user_wrong_role_and_invalid_report():
    with TestClient(app) as client:
        owner = headers(2101)
        role = client.post("/career-workspace/applications", headers=owner,
                           json={"company": "Bound", "role": "Engineer"}).json()
        other = client.post("/career-workspace/applications", headers=owner,
                            json={"company": "Other", "role": "Analyst"}).json()
        report = client.post(f"/career-workspace/applications/{role['id']}/artifacts",
                             headers=owner, json={"kind": "evaluation", "source": "career-ops",
                                                  "content_markdown": "**Score:** 4/5\n## Score Evidence\nIncomplete"}).json()
        path = f"/career-workspace/applications/{role['id']}/evaluation-score"
        assert client.post(path, headers=headers(2102), json={"artifact_id": report["id"]}).status_code == 404
        assert client.post(f"/career-workspace/applications/{other['id']}/evaluation-score",
                           headers=owner, json={"artifact_id": report["id"]}).status_code == 404
        assert client.post(path, headers=owner, json={"artifact_id": report["id"]}).status_code == 400
        duplicate = client.post(f"/career-workspace/applications/{role['id']}/artifacts",
                                headers=owner, json={"kind": "evaluation", "source": "career-ops",
                                                     "content_markdown": "**Score:** 4/5\n**Score:** 5/5\n"
                                                     "## Score Evidence\n"
                                                     "| CV match | supported | Resume facts | Confirm skills |\n"
                                                     "| North Star alignment | partial | Target role | Confirm scope |\n"
                                                     "| Compensation | unknown | Pay absent | Ask salary |\n"
                                                     "| Cultural signals | unknown | Team absent | Ask team |\n"
                                                     "| Red flags | partial | Date absent | Check date |"}).json()
        assert client.post(path, headers=owner, json={"artifact_id": duplicate["id"]}).status_code == 400
        missing_matrix = client.post(f"/career-workspace/applications/{role['id']}/artifacts",
                                     headers=owner, json={"kind": "evaluation", "source": "career-ops",
                                                          "content_markdown": "**Score:** 4/5\n## Score Evidence\n"
                                                          "| CV match | supported | Resume facts | Confirm skills |\n"
                                                          "| North Star alignment | partial | Target role | Confirm scope |\n"
                                                          "| Compensation | unknown | Pay absent | Ask salary |\n"
                                                          "| Cultural signals | unknown | Team absent | Ask team |\n"
                                                          "| Red flags | partial | Date absent | Check date |"}).json()
        rejected = client.post(path, headers=owner, json={"artifact_id": missing_matrix["id"]})
        assert rejected.status_code == 400
        assert rejected.json()["detail"] == "Evaluation report has no complete requirement evidence"
        unchanged = client.get(f"/career-workspace/applications/{role['id']}", headers=owner).json()
        assert unchanged["fit_score"] is None
        assert unchanged["evaluation_artifact_id"] is None


def test_evaluation_score_binds_owned_immutable_sources_and_checks_quotes():
    with TestClient(app) as client:
        owner = headers(2151)
        role = client.post("/career-workspace/applications", headers=owner,
                           json={"company": "Evidence", "role": "Engineer"}).json()
        other = client.post("/career-workspace/applications", headers=owner,
                            json={"company": "Other Evidence", "role": "Engineer"}).json()
        resume = "Built Python APIs with SQL and maintained production reliability."
        job = "Build Python APIs and maintain reliable production services."
        snapshot = {"schema_version": 1, "resume_markdown": resume, "job_description": job,
                    "resume_sha256": hashlib.sha256(resume.encode()).hexdigest(),
                    "job_sha256": hashlib.sha256(job.encode()).hexdigest()}

        def add_source(application_id, payload=snapshot):
            response = client.post(f"/career-workspace/applications/{application_id}/artifacts",
                                   headers=owner, json={"kind": "evaluation_sources", "source": "hanggent",
                                                        "content_markdown": json.dumps(payload)})
            assert response.status_code == 200, response.text
            return response.json()["id"]

        source_id = add_source(role["id"])
        other_source_id = add_source(other["id"])

        def add_report(source_ref, cv_quote="Built Python APIs"):
            report = ("**Score:** 4/5\n## B) Match with CV\n"
                      "| Requirement | Importance | Match | JD signal | Evidence / gap |\n"
                      "| --- | --- | --- | --- | --- |\n"
                      f'| Python APIs | high (stated) | Strong | "Build Python APIs" | "{cv_quote}" |\n'
                      "## Score Evidence\n"
                      "| CV match | supported | Python APIs | Verify depth |\n"
                      "| North Star alignment | partial | Role aligns | Confirm scope |\n"
                      "| Compensation | unknown | Pay absent | Ask salary |\n"
                      "| Cultural signals | unknown | Culture absent | Ask team |\n"
                      "| Red flags | partial | Posting age unknown | Check liveness |\n"
                      f"<!-- career-source-artifact: {source_ref} -->")
            response = client.post(f"/career-workspace/applications/{role['id']}/artifacts",
                                   headers=owner, json={"kind": "evaluation", "source": "career-ops",
                                                        "content_markdown": report})
            assert response.status_code == 200, response.text
            return response.json()["id"]

        wrong_quote = add_report(source_id, "Invented achievement")
        path = f"/career-workspace/applications/{role['id']}/evaluation-score"
        assert client.post(path, headers=owner, json={"artifact_id": wrong_quote,
                                                     "source_artifact_id": source_id}).status_code == 400
        valid_report = add_report(source_id)
        assert client.post(path, headers=owner, json={"artifact_id": valid_report}).status_code == 400
        assert client.post(path, headers=owner, json={"artifact_id": valid_report,
                                                     "source_artifact_id": other_source_id}).status_code == 400
        wrong_role_report = add_report(other_source_id)
        assert client.post(path, headers=owner, json={"artifact_id": wrong_role_report,
                                                     "source_artifact_id": other_source_id}).status_code == 404
        invalid_snapshot = dict(snapshot, job_sha256="0" * 64)
        invalid_source_id = add_source(role["id"], invalid_snapshot)
        invalid_report = add_report(invalid_source_id)
        assert client.post(path, headers=owner, json={"artifact_id": invalid_report,
                                                     "source_artifact_id": invalid_source_id}).status_code == 400
        bound = client.post(path, headers=owner, json={"artifact_id": valid_report,
                                                      "source_artifact_id": source_id})
        assert bound.status_code == 200, bound.text
        assert bound.json()["evaluation_source_artifact_id"] == source_id
        comparison = client.post("/career-workspace/comparisons", headers=owner, json={
            "application_ids": [role["id"], other["id"]],
        })
        assert comparison.status_code == 200, comparison.text
        assert comparison.json()["entries"][0]["source_provenance"] == "snapshot_verified"


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


def test_reviewed_research_is_versioned_and_does_not_change_application():
    with TestClient(app) as client:
        application = client.post("/career-workspace/applications", headers=headers(61), json={
            "company": "Research Co", "role": "Engineer", "stage": "saved",
        }).json()
        endpoint = f"/career-workspace/applications/{application['id']}/artifacts"
        body = {"kind": "research", "source": "hanggent", "content_markdown": "# Reviewed job research\nSource: company website"}
        assert client.post(endpoint, headers=headers(62), json=body).status_code == 404
        first = client.post(endpoint, headers=headers(61), json=body)
        second = client.post(endpoint, headers=headers(61), json={
            **body, "content_markdown": "# Corrected research\nSource: company website",
        })
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert (first.json()["version"], second.json()["version"]) == (1, 2)
        activity = client.get(f"{endpoint[:-10]}/activity", headers=headers(61)).json()
        assert sorted(artifact["version"] for artifact in activity["artifacts"] if artifact["kind"] == "research") == [1, 2]
        assert activity["submission_attempts"] == []
        unchanged = client.get(f"{endpoint[:-10]}", headers=headers(61)).json()
        assert unchanged["stage"] == "saved"
        assert unchanged["fit_score"] is None


def test_research_task_link_metadata_is_owner_scoped_and_versioned():
    with TestClient(app) as client:
        application = client.post("/career-workspace/applications", headers=headers(63), json={
            "company": "Task Link Co", "role": "Engineer", "stage": "saved",
        }).json()
        endpoint = f"/career-workspace/applications/{application['id']}/artifacts"
        body = {"kind": "research_task_link", "source": "hanggent",
                "content_markdown": '{"project_id":"project-1","task_id":"task-1"}'}
        assert client.post(endpoint, headers=headers(64), json=body).status_code == 404
        response = client.post(endpoint, headers=headers(63), json=body)
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 1
        activity = client.get(f"{endpoint[:-10]}/activity", headers=headers(63)).json()
        assert activity["artifacts"][0]["content_markdown"] == body["content_markdown"]
        assert activity["submission_attempts"] == []
        assert client.get(f"{endpoint[:-10]}", headers=headers(63)).json()["stage"] == "saved"


def test_source_observation_is_versioned_without_claiming_listing_liveness():
    with TestClient(app) as client:
        application = client.post("/career-workspace/applications", headers=headers(65), json={
            "company": "Source Co", "role": "Engineer", "stage": "saved",
        }).json()
        endpoint = f"/career-workspace/applications/{application['id']}/artifacts"
        body = {"kind": "source_observation", "source": "career-compass",
                "content_markdown": '{"site":"indeed","observed_at":"2026-10-18T00:00:00+00:00","liveness":"unchecked"}'}
        assert client.post(endpoint, headers=headers(66), json=body).status_code == 404
        response = client.post(endpoint, headers=headers(65), json=body)
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 1
        assert client.get(f"{endpoint[:-10]}", headers=headers(65)).json()["stage"] == "saved"


def test_liveness_evidence_does_not_change_stage_or_score():
    with TestClient(app) as client:
        application = client.post("/career-workspace/applications", headers=headers(67), json={
            "company": "ATS Co", "role": "Engineer", "stage": "saved",
        }).json()
        endpoint = f"/career-workspace/applications/{application['id']}/artifacts"
        body = {"kind": "liveness_check", "source": "hanggent",
                "content_markdown": '{"status":"expired","method":"greenhouse_public_api"}'}
        assert client.post(endpoint, headers=headers(67), json=body).status_code == 200
        unchanged = client.get(f"{endpoint[:-10]}", headers=headers(67)).json()
        assert unchanged["stage"] == "saved"
        assert unchanged["fit_score"] is None


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
        assert client.get(
            f"/career-workspace/applications/{case['id']}", headers=headers(41)
        ).json()["company"] == "Example"
        assert client.get(
            f"/career-workspace/applications/{case['id']}", headers=headers(42)
        ).status_code == 404
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
        drafted = client.post(
            f"/career-workspace/applications/{case['id']}/artifacts",
            headers=headers(41), json={
                "kind": "followup", "source": "hanggent", "content_markdown": "Dear recruiter, ...",
            },
        )
        assert drafted.status_code == 200, drafted.text


def test_terminal_outcomes_are_candidate_labelled_and_reversible():
    with TestClient(app) as client:
        case = client.post("/career-workspace/applications", headers=headers(56), json={
            "company": "Outcome Co", "role": "Engineer", "stage": "applied",
        }).json()
        endpoint = f"/career-workspace/applications/{case['id']}/feedback"
        assert client.post(endpoint, headers=headers(57), json={
            "stage": "closed", "outcome_kind": "rejected", "note": "Employer sent a rejection",
        }).status_code == 404
        assert client.post(endpoint, headers=headers(56), json={
            "stage": "screening", "outcome_kind": "rejected", "note": "Wrong stage",
        }).status_code == 400
        assert client.post(endpoint, headers=headers(56), json={
            "stage": "accepted", "outcome_kind": "rejected", "note": "Wrong reason",
        }).status_code == 400
        changed = client.post(endpoint, headers=headers(56), json={
            "stage": "closed", "outcome_kind": "rejected", "note": "Employer sent a rejection",
        })
        assert changed.status_code == 200, changed.text
        assert changed.json()["application"]["outcome_kind"] == "rejected"
        assert client.get(
            f"/career-workspace/applications/{case['id']}/activity", headers=headers(56),
        ).json()["feedback"][0]["outcome_kind"] == "rejected"
        reopened = client.post(endpoint, headers=headers(56), json={
            "stage": "interview", "note": "Corrected an incorrect closure",
        })
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["application"]["outcome_kind"] is None
        accepted = client.post(endpoint, headers=headers(56), json={
            "stage": "accepted", "outcome_kind": "accepted", "note": "I accepted the offer",
        })
        assert accepted.status_code == 200, accepted.text
        assert client.get("/career-workspace/applications", headers=headers(56)).json()[0]["outcome_kind"] == "accepted"


def test_outreach_draft_is_versioned_and_does_not_record_employer_contact():
    with TestClient(app) as client:
        case = client.post("/career-workspace/applications", headers=headers(52), json={
            "company": "Outreach Co", "role": "Analyst", "stage": "saved",
        }).json()
        endpoint = f"/career-workspace/applications/{case['id']}/artifacts"
        for version in (1, 2):
            response = client.post(endpoint, headers=headers(52), json={
                "kind": "outreach", "source": "hanggent",
                "content_markdown": f"Candidate-review draft {version}; not sent",
            })
            assert response.status_code == 200, response.text
            assert response.json()["version"] == version
        activity = client.get(
            f"/career-workspace/applications/{case['id']}/activity", headers=headers(52),
        ).json()
        assert [item["kind"] for item in activity["artifacts"]] == ["outreach", "outreach"]
        assert activity["contacts"] == []
        assert activity["submission_attempts"] == []
        assert client.post(
            f"/career-workspace/applications/{case['id']}/contacts", headers=headers(52), json={
                "channel": "email", "note": "Sent the reviewed first-contact message",
                "contacted_at": "2026-10-17T10:00:00Z", "confirmed_contact": False,
            },
        ).status_code == 400
        contact = client.post(
            f"/career-workspace/applications/{case['id']}/contacts", headers=headers(52), json={
                "channel": "email", "note": "Sent the reviewed first-contact message",
                "contacted_at": "2026-10-17T10:00:00Z", "confirmed_contact": True,
                "next_followup_at": "2026-10-20T10:00:00Z",
            },
        )
        assert contact.status_code == 200, contact.text
        assert client.get(
            f"/career-workspace/applications/{case['id']}/activity", headers=headers(52),
        ).json()["contacts"][0]["note"] == "Sent the reviewed first-contact message"
        assert client.get(
            f"/career-workspace/applications/{case['id']}", headers=headers(52),
        ).json()["stage"] == "saved"
        assert client.post(endpoint, headers=headers(53), json={
            "kind": "outreach", "source": "hanggent", "content_markdown": "Not mine",
        }).status_code == 404


def test_confirmed_contact_is_separate_from_draft_and_drives_reminder():
    with TestClient(app) as client:
        case = client.post("/career-workspace/applications", headers=headers(50), json={
            "company": "Contact Co", "role": "Analyst", "stage": "applied",
        }).json()
        endpoint = f"/career-workspace/applications/{case['id']}/contacts"
        payload = {
            "channel": "email", "note": "Sent recruiter a follow-up email",
            "contacted_at": "2026-10-15T10:00:00Z",
            "next_followup_at": "2026-10-20T10:00:00Z", "confirmed_contact": True,
        }
        assert client.post(endpoint, headers=headers(51), json=payload).status_code == 404
        assert client.post(endpoint, headers=headers(50), json={
            **payload, "confirmed_contact": False,
        }).status_code == 400
        assert client.post(endpoint, headers=headers(50), json={
            **payload, "next_followup_at": "2026-10-14T10:00:00Z",
        }).status_code == 400
        drafted = client.post(
            f"/career-workspace/applications/{case['id']}/artifacts", headers=headers(50),
            json={"kind": "followup", "source": "hanggent", "content_markdown": "Draft only"},
        )
        assert drafted.status_code == 200, drafted.text
        assert client.get(f"{endpoint.removesuffix('/contacts')}/activity", headers=headers(50)).json()["contacts"] == []
        response = client.post(endpoint, headers=headers(50), json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["source"] == "candidate_confirmed"
        listed = client.get("/career-workspace/applications", headers=headers(50)).json()
        assert listed[0]["last_contact_at"].startswith("2026-10-15")
        assert listed[0]["next_followup_at"].startswith("2026-10-20")
        detail = client.get(f"/career-workspace/applications/{case['id']}", headers=headers(50)).json()
        assert detail["next_followup_at"] == listed[0]["next_followup_at"]
        activity = client.get(f"/career-workspace/applications/{case['id']}/activity", headers=headers(50)).json()
        assert len(activity["contacts"]) == 1
        assert activity["contacts"][0]["note"] == payload["note"]
        assert client.get(f"/career-workspace/applications/{case['id']}/activity", headers=headers(51)).status_code == 404


def test_candidate_reported_reply_is_owner_scoped_and_does_not_advance_stage():
    with TestClient(app) as client:
        case = client.post("/career-workspace/applications", headers=headers(54), json={
            "company": "Reply Co", "role": "Analyst", "stage": "saved",
        }).json()
        endpoint = f"/career-workspace/applications/{case['id']}/replies"
        payload = {
            "channel": "email", "note": "Recruiter asked for availability",
            "received_at": "2026-10-17T10:00:00Z", "confirmed_reply": True,
        }
        contact_endpoint = f"/career-workspace/applications/{case['id']}/contacts"
        assert client.post(contact_endpoint, headers=headers(54), json={
            "channel": "email", "note": "Sent first contact",
            "contacted_at": "2026-10-16T10:00:00Z",
            "next_followup_at": "2026-10-20T10:00:00Z", "confirmed_contact": True,
        }).status_code == 200
        assert client.get(
            f"/career-workspace/applications/{case['id']}", headers=headers(54),
        ).json()["next_followup_at"].startswith("2026-10-20")
        assert client.post(endpoint, headers=headers(55), json=payload).status_code == 404
        assert client.post(endpoint, headers=headers(54), json={
            **payload, "confirmed_reply": False,
        }).status_code == 400
        assert client.post(endpoint, headers=headers(54), json={
            **payload, "received_at": "2030-10-17T10:00:00Z",
        }).status_code == 400
        response = client.post(endpoint, headers=headers(54), json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["source"] == "candidate_reported"
        assert response.json()["external_verification"] == "none"
        activity = client.get(
            f"/career-workspace/applications/{case['id']}/activity", headers=headers(54),
        ).json()
        assert len(activity["replies"]) == 1
        assert activity["replies"][0]["note"] == payload["note"]
        assert len(activity["contacts"]) == 1
        assert activity["submission_attempts"] == []
        detail = client.get(
            f"/career-workspace/applications/{case['id']}", headers=headers(54),
        ).json()
        assert detail["stage"] == "saved"
        assert detail["last_reply_at"].startswith("2026-10-17")
        assert detail["next_followup_at"] is None
        assert client.get("/career-workspace/applications", headers=headers(54)).json()[0]["next_followup_at"] is None
        assert client.post(contact_endpoint, headers=headers(54), json={
            "channel": "email", "note": "Answered the availability question",
            "contacted_at": "2026-10-17T12:00:00Z",
            "next_followup_at": "2026-10-21T10:00:00Z", "confirmed_contact": True,
        }).status_code == 200
        assert client.get(
            f"/career-workspace/applications/{case['id']}", headers=headers(54),
        ).json()["next_followup_at"].startswith("2026-10-21")
        assert client.get(
            f"/career-workspace/applications/{case['id']}/activity", headers=headers(55),
        ).status_code == 404


def test_legacy_scores_are_unverified_after_schema_upgrade():
    async def check():
        old_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with old_engine.begin() as connection:
                await connection.exec_driver_sql(
                    "CREATE TABLE career_applications (id VARCHAR PRIMARY KEY, fit_score FLOAT)"
                )
                await connection.exec_driver_sql(
                    "INSERT INTO career_applications (id, fit_score) VALUES ('legacy', 95)"
                )
                await ensure_career_score_kind(connection)
                await ensure_career_score_kind(connection)
                row = (await connection.exec_driver_sql(
                    "SELECT fit_score, score_kind FROM career_applications WHERE id = 'legacy'"
                )).one()
                assert row == (95, "unverified")
        finally:
            await old_engine.dispose()
    asyncio.run(check())


def test_legacy_terminal_records_remain_unclassified_after_schema_upgrade():
    async def check():
        old_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with old_engine.begin() as connection:
                await connection.exec_driver_sql(
                    "CREATE TABLE career_applications (id VARCHAR PRIMARY KEY, stage VARCHAR)"
                )
                await connection.exec_driver_sql(
                    "CREATE TABLE career_stage_events (id VARCHAR PRIMARY KEY, to_stage VARCHAR)"
                )
                await connection.exec_driver_sql(
                    "INSERT INTO career_applications (id, stage) VALUES ('legacy', 'closed')"
                )
                await ensure_career_outcome_kind(connection)
                await ensure_career_outcome_kind(connection)
                row = (await connection.exec_driver_sql(
                    "SELECT outcome_kind FROM career_applications WHERE id = 'legacy'"
                )).one()
                assert row[0] is None
        finally:
            await old_engine.dispose()

    asyncio.run(check())


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
        endpoint = f"/career-workspace/applications/{allowed['id']}/submission-attempts"
        assert client.post(endpoint, json={
            "authorization_id": policy["id"], "idempotency_key": "preliminary-score-999",
        }, headers=headers(3)).status_code == 403
        imported = client.post("/career-workspace/applications", json={
            "company": "North", "role": "Engineer", "fit_score": 99,
            "score_kind": "career_ops_import",
            "job_url": "https://www.linkedin.com/jobs/view/999", "sources": ["career-ops"],
        }, headers=headers(3)).json()
        assert imported["fit_score"] == 99
        assert client.post(endpoint, json={
            "authorization_id": policy["id"], "idempotency_key": "imported-score-999",
        }, headers=headers(3)).status_code == 403
        evaluated = client.post("/career-workspace/applications", json={
            "company": "North", "role": "Engineer", "fit_score": 70,
            "score_kind": "career_ops_evaluation",
            "job_url": "https://www.linkedin.com/jobs/view/999", "sources": ["career-ops"],
        }, headers=headers(3)).json()
        assert evaluated["id"] == allowed["id"]
        assert evaluated["fit_score"] == 70
        preliminary_resync = client.post("/career-workspace/applications", json={
            "company": "North", "role": "Engineer", "fit_score": 100,
            "score_kind": "resume_overlap",
            "job_url": "https://www.linkedin.com/jobs/view/999", "sources": ["career-compass"],
        }, headers=headers(3)).json()
        assert preliminary_resync["fit_score"] == 70
        assert preliminary_resync["score_kind"] == "career_ops_evaluation"
        assert client.post(endpoint, json={
            "authorization_id": policy["id"], "idempotency_key": "low-evaluation-999",
        }, headers=headers(3)).status_code == 403
        evaluated = client.post("/career-workspace/applications", json={
            "company": "North", "role": "Engineer", "fit_score": 95,
            "score_kind": "career_ops_evaluation",
            "job_url": "https://www.linkedin.com/jobs/view/999", "sources": ["career-ops"],
        }, headers=headers(3)).json()
        assert evaluated["fit_score"] == 95
        assert client.post(endpoint, json={
            "authorization_id": policy["id"], "idempotency_key": "unbound-evaluation-999",
        }, headers=headers(3)).status_code == 403
        report = client.post(f"/career-workspace/applications/{allowed['id']}/artifacts",
                             headers=headers(3), json={"kind": "evaluation", "source": "career-ops",
                                                      "content_markdown": "**Score:** 4.75/5\n"
                                                      "## B) Match with CV\n"
                                                      "| Requirement | Importance | Match | JD signal | Evidence / gap |\n"
                                                      "| --- | --- | --- | --- | --- |\n"
                                                      "| Python services | high (structural) | ✅ Strong | Responsibilities: Python services | CV: Built Python APIs |\n"
                                                      "## Score Evidence\n"
                                                      "| CV match | supported | Resume facts | Confirm skills |\n"
                                                      "| North Star alignment | partial | Target role | Confirm scope |\n"
                                                      "| Compensation | unknown | Pay absent | Ask salary |\n"
                                                      "| Cultural signals | unknown | Team absent | Ask team |\n"
                                                      "| Red flags | partial | Date absent | Check date |"}).json()
        bound = client.post(f"/career-workspace/applications/{allowed['id']}/evaluation-score",
                            headers=headers(3), json={"artifact_id": report["id"]})
        assert bound.status_code == 200, bound.text
        assert bound.json()["fit_score"] == 95
        attempt = client.post(
            endpoint,
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
