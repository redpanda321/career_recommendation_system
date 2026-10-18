"""Bound, immutable input snapshots for career evaluation reports.

Literal quotation checks prove only that a cited excerpt was present in the
saved input; they do not establish that the candidate fact is true or relevant.
"""

from hashlib import sha256
from html import unescape
import json
import re
from uuid import UUID


SOURCE_MARKER = re.compile(r"^<!-- career-source-artifact: ([0-9a-f-]{36}) -->$", re.MULTILINE)


def parse_source_snapshot(content: str) -> dict[str, str] | None:
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return None
    resume = data.get("resume_markdown")
    job = data.get("job_description")
    if (not isinstance(resume, str) or not 40 <= len(resume) <= 12000
            or not isinstance(job, str) or not 40 <= len(job) <= 24000):
        return None
    if data.get("resume_sha256") != sha256(resume.encode("utf-8")).hexdigest():
        return None
    if data.get("job_sha256") != sha256(job.encode("utf-8")).hexdigest():
        return None
    return {"resume_markdown": resume, "job_description": job,
            "resume_sha256": data["resume_sha256"], "job_sha256": data["job_sha256"]}


def report_source_id(report: str) -> str | None:
    markers = SOURCE_MARKER.findall(report)
    if len(markers) != 1:
        return None
    try:
        return str(UUID(markers[0]))
    except ValueError:
        return None


def _source_text(value: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split()).casefold()


def _quoted_source_excerpt(cell: str, source: str) -> bool:
    normalized = _source_text(source)
    for match in re.finditer(r'"([^"\n]+)"|“([^”\n]+)”', cell):
        excerpt = " ".join((match.group(1) or match.group(2)).split())
        if 4 <= len(excerpt) <= 200 and _source_text(excerpt) in normalized:
            return True
    return False


def verify_requirement_quotes(rows: list[dict[str, str]], *, resume: str, job: str) -> bool:
    for row in rows:
        if row["importance_evidence"] == "stated" and not _quoted_source_excerpt(row["jd_signal"], job):
            return False
        if row["match"] in {"Strong", "Partial"} and not _quoted_source_excerpt(
            row["candidate_evidence"], resume
        ):
            return False
    return True
