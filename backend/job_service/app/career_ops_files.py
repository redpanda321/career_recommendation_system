"""Private career-ops user layer. Markdown is authoritative for tracker state.

The mounted directory is user data, never the vendored career-ops code tree.
Database rows remain an index and hold authorization/worker lease state.
"""

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

HEADER = "| # | Date | Company | Role | Score | Status | PDF | Report | Notes | URL |"
SEPARATOR = (
    "|---|------|---------|------|-------|--------|-----|--------|-------|-----|"
)
MARKER = re.compile(r"\s*\[hanggent-id:([0-9a-f-]{36})\]")
STAGES = {
    "saved": "Evaluated",
    "applied": "Applied",
    "screening": "Responded",
    "assessment": "Assessment",
    "interview": "Interview",
    "offer": "Offer",
    "accepted": "Hired",
    "closed": "Discarded",
}


def configured_root() -> Path | None:
    value = os.environ.get("CAREER_OPS_DATA_ROOT", "").strip()
    if not value:
        return None
    configured = Path(value)
    if not configured.is_absolute():
        raise RuntimeError("CAREER_OPS_DATA_ROOT must be absolute")
    if configured.is_symlink():
        raise RuntimeError("CAREER_OPS_DATA_ROOT may not be a symlink")
    root = configured.resolve()
    if root == Path(root.anchor):
        raise RuntimeError("CAREER_OPS_DATA_ROOT must be a dedicated directory")
    return root


def workspace(user_id: str) -> Path:
    root = configured_root()
    if root is None:
        raise RuntimeError("CAREER_OPS_DATA_ROOT is not configured")
    # The ID never becomes a path component or appears in filenames.
    path = root / "users" / hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    for ancestor in (root, root / "users", path):
        if ancestor.is_symlink():
            raise RuntimeError("Career workspace may not contain symlinks")
    return path


def _safe_path(base: Path, relative: str) -> Path:
    path = base / relative
    if not path.resolve().is_relative_to(base.resolve()):
        raise RuntimeError("Career workspace path escapes user directory")
    for ancestor in (path, *path.parents):
        if ancestor == base.parent:
            break
        if ancestor.is_symlink():
            raise RuntimeError("Career workspace may not contain symlinks")
    return path


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError("Refusing to overwrite a symlink")
    fd, name = tempfile.mkstemp(prefix=".career-ops-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(base: Path):
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _safe_path(base, ".hanggent.lock")
    if lock.is_symlink():
        raise RuntimeError("Refusing a symlink lock file")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    start = time.monotonic()
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
        else:
            import fcntl
        while True:
            try:
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() - start > 5:
                    raise TimeoutError("Career workspace is busy")
                time.sleep(0.05)
        yield
    finally:
        if acquired and os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        elif acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _cell(value: object) -> str:
    return str(value or "—").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _parts(line: str) -> list[str]:
    return [
        cell.strip().replace("\\|", "|")
        for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
    ]


def _tracker_path(base: Path) -> Path:
    return _safe_path(base, "data/applications.md")


def migration_complete(user_id: str) -> bool:
    return _safe_path(workspace(user_id), "data/.hanggent-migration-complete").exists()


def mark_migration_complete(user_id: str) -> None:
    base = workspace(user_id)
    atomic_write(_safe_path(base, "data/.hanggent-migration-complete"), "v1\n")


def read_tracker(user_id: str) -> list[dict]:
    path = _tracker_path(workspace(user_id))
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    header = next(
        (
            (_parts(line), index)
            for index, line in enumerate(lines)
            if line.startswith("|")
            and "Company" in _parts(line)
            and "Status" in _parts(line)
        ),
        None,
    )
    if header is None:
        raise ValueError("career-ops tracker has no recognized header")
    columns, index = header
    result = []
    for line in lines[index + 1 :]:
        if not line.startswith("|"):
            continue
        parts = _parts(line)
        if len(parts) != len(columns) or not parts[0].isdigit():
            continue
        row = dict(zip(columns, parts))
        match = MARKER.search(row.get("Notes", ""))
        if match:
            row["id"] = match.group(1)
            row["Notes"] = MARKER.sub("", row["Notes"]).strip()
        else:
            row["id"] = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"career-ops:{user_id}:{row['#']}")
            )
        result.append(row)
    return result


def write_application(user_id: str, item: dict) -> None:
    """Upsert a Hanggent row while preserving all other career-ops rows and columns."""
    base = workspace(user_id)
    path = _tracker_path(base)
    with locked(base):
        lines = (
            path.read_text(encoding="utf-8").splitlines()
            if path.exists()
            else [
                "# Applications Tracker",
                "",
                HEADER,
                SEPARATOR,
            ]
        )
        header_index = next(
            (
                i
                for i, line in enumerate(lines)
                if line.startswith("|")
                and "Company" in _parts(line)
                and "Status" in _parts(line)
            ),
            None,
        )
        if header_index is None:
            raise ValueError("career-ops tracker has no recognized header")
        columns = _parts(lines[header_index])
        if not set(
            ("#", "Date", "Company", "Role", "Score", "Status", "Notes")
        ).issubset(columns):
            raise ValueError("career-ops tracker is missing required columns")
        target = next(
            (
                i
                for i, line in enumerate(lines)
                if f"[hanggent-id:{item['id']}]" in line
            ),
            None,
        )
        if target is None:
            target = next(
                (
                    i
                    for i, line in enumerate(lines)
                    if line.startswith("|")
                    and _parts(line)
                    and _parts(line)[0].isdigit()
                    and str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"career-ops:{user_id}:{_parts(line)[0]}",
                        )
                    )
                    == item["id"]
                ),
                None,
            )
        numbers = [
            int(_parts(line)[0])
            for line in lines
            if line.startswith("|") and _parts(line) and _parts(line)[0].isdigit()
        ]
        old = dict(zip(columns, _parts(lines[target]))) if target is not None else {}
        notes = "; ".join(str(n) for n in item.get("notes", []) if n)
        today = datetime.now(timezone.utc).date().isoformat()
        score = item.get("fit_score")
        cells = {
            **old,
            "#": old.get("#") or str(max(numbers, default=0) + 1),
            "Date": old.get("Date") or today,
            "Company": item["company"],
            "Role": item["role"],
            "Score": f"{score / 20:.4f}".rstrip("0").rstrip(".") + "/5"
            if score is not None
            else "—",
            "Status": STAGES[item["stage"]],
            "PDF": old.get("PDF") or "❌",
            "Report": old.get("Report") or "—",
            "Notes": f"{notes} [hanggent-id:{item['id']}]".strip(),
        }
        if "URL" in columns:
            cells["URL"] = item.get("job_url") or "—"
        rendered = (
            "| " + " | ".join(_cell(cells.get(column)) for column in columns) + " |"
        )
        if target is None:
            lines.append(rendered)
        else:
            lines[target] = rendered
        # Metadata not represented by the upstream tracker is a separate user file.
        meta = _safe_path(base, f"data/hanggent/{item['id']}.json")
        extra = {
            key: item.get(key)
            for key in (
                "location",
                "job_url",
                "fit_score",
                "score_kind",
                "sources",
                "canonical_key",
            )
        }
        atomic_write(meta, json.dumps(extra, ensure_ascii=False, indent=2) + "\n")
        atomic_write(path, "\n".join(lines).rstrip() + "\n")


def read_application(user_id: str, application_id: str) -> dict | None:
    row = next(
        (row for row in read_tracker(user_id) if row.get("id") == application_id), None
    )
    if row is None:
        return None
    base = workspace(user_id)
    meta_path = _safe_path(base, f"data/hanggent/{application_id}.json")
    meta = (
        json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    )
    score_match = re.fullmatch(r"(\d+(?:\.\d+)?)/5", row.get("Score", ""))
    score = float(score_match.group(1)) * 20 if score_match else None
    score_kind = meta.get("score_kind") or "career_ops_import"
    if score_kind == "career_ops_evaluation" and (
        score is None
        or meta.get("fit_score") is None
        or abs(score - meta["fit_score"]) > 0.002
    ):
        # A hand-edited score is still visible, but no longer proves that the
        # original bounded evaluation qualified it for automatic submission.
        score_kind = "career_ops_import"
    url = meta.get("job_url") or (
        row.get("URL") if row.get("URL") not in (None, "—") else None
    )
    return {
        "id": application_id,
        "company": row["Company"],
        "role": row["Role"],
        "stage": next(
            (
                key
                for key, value in STAGES.items()
                if value.casefold() == row["Status"].casefold()
            ),
            "saved",
        ),
        "fit_score": score,
        "notes": [
            n.strip() for n in row["Notes"].split("; ") if n.strip() and n != "—"
        ],
        "job_url": url,
        "location": meta.get("location"),
        "score_kind": score_kind,
        "sources": meta.get("sources") or ["career-ops"],
        "canonical_key": meta.get("canonical_key")
        or url
        or "::".join((row["Company"].casefold(), row["Role"].casefold(), "")),
    }


def write_profile(user_id: str, profile: dict) -> None:
    base = workspace(user_id)
    with locked(base):
        profile_path = _safe_path(base, "config/profile.yml")
        document = (
            yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            if profile_path.exists()
            else {}
        )
        if not isinstance(document, dict):
            raise ValueError("career-ops profile must be a YAML mapping")
        document.setdefault("candidate", {})["full_name"] = profile.get("full_name", "")
        document["candidate"]["location"] = profile.get("location", "")
        document.setdefault("narrative", {})["headline"] = profile.get("headline", "")
        document.setdefault("target_roles", {})["primary"] = profile.get(
            "target_roles", []
        )
        document.setdefault("hanggent", {})["skills"] = profile.get("skills", [])
        atomic_write(_safe_path(base, "cv.md"), profile.get("resume_markdown", ""))
        atomic_write(
            profile_path, yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        )


def read_profile(user_id: str) -> dict | None:
    base = workspace(user_id)
    profile_path = _safe_path(base, "config/profile.yml")
    if not profile_path.exists():
        return None
    document = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("career-ops profile must be a YAML mapping")
    candidate = document.get("candidate") or {}
    narrative = document.get("narrative") or {}
    roles = document.get("target_roles") or {}
    hanggent = document.get("hanggent") or {}
    cv_path = _safe_path(base, "cv.md")
    return {
        "full_name": candidate.get("full_name", ""),
        "headline": narrative.get("headline", ""),
        "location": candidate.get("location", ""),
        "target_roles": roles.get("primary", []),
        "skills": hanggent.get("skills", []),
        "resume_markdown": cv_path.read_text(encoding="utf-8")
        if cv_path.exists()
        else "",
    }


def write_artifact(
    user_id: str, application_id: str, artifact_id: str, kind: str, content: str
) -> None:
    base = workspace(user_id)
    name = f"reports/{artifact_id}.md"
    with locked(base):
        atomic_write(_safe_path(base, name), content)


def read_artifact(user_id: str, artifact_id: str) -> str | None:
    path = _safe_path(workspace(user_id), f"reports/{artifact_id}.md")
    return path.read_text(encoding="utf-8") if path.exists() else None


def link_evaluation_report(user_id: str, application_id: str, artifact_id: str) -> None:
    """Make an evaluation reachable from the upstream tracker Report column."""
    base = workspace(user_id)
    path = _tracker_path(base)
    with locked(base):
        if not path.exists():
            raise ValueError("career-ops tracker is missing")
        lines = path.read_text(encoding="utf-8").splitlines()
        header = next(
            (
                line
                for line in lines
                if line.startswith("|")
                and "Company" in _parts(line)
                and "Report" in _parts(line)
            ),
            None,
        )
        if header is None:
            raise ValueError("career-ops tracker has no Report column")
        columns = _parts(header)
        for index, line in enumerate(lines):
            if f"[hanggent-id:{application_id}]" not in line:
                continue
            parts = _parts(line)
            if len(parts) != len(columns):
                raise ValueError("career-ops tracker row is malformed")
            number = parts[columns.index("#")]
            parts[columns.index("Report")] = f"[{number}](../reports/{artifact_id}.md)"
            lines[index] = "| " + " | ".join(_cell(cell) for cell in parts) + " |"
            atomic_write(path, "\n".join(lines).rstrip() + "\n")
            return
        raise ValueError("Application is missing from career-ops tracker")


def read_pipeline(user_id: str) -> list[dict]:
    path = _safe_path(workspace(user_id), "data/pipeline.md")
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^- \[([ x])\] (https?://\S+)(?:\s*\|\s*(.*))?$", line)
        if match:
            details = (match.group(3) or "").split("|")
            entries.append(
                {
                    "job_url": match.group(2),
                    "status": "pending" if match.group(1) == " " else "processed",
                    "company": details[0].strip() if details else "",
                    "role": details[1].strip() if len(details) > 1 else "",
                }
            )
    return entries


def write_pipeline(
    user_id: str,
    job_url: str,
    company: str = "",
    role: str = "",
    *,
    processed: bool = False,
) -> None:
    base = workspace(user_id)
    path = _safe_path(base, "data/pipeline.md")
    with locked(base):
        lines = (
            path.read_text(encoding="utf-8").splitlines()
            if path.exists()
            else ["# Pipeline", "", "## Pending"]
        )
        marker = "x" if processed else " "
        item = f"- [{marker}] {job_url} | {_cell(company)} | {_cell(role)}"
        found = next(
            (
                i
                for i, line in enumerate(lines)
                if re.match(r"^- \[[ x]\] " + re.escape(job_url) + r"(?:\s|$)", line)
            ),
            None,
        )
        if found is None:
            lines.append(item)
        elif processed:
            lines[found] = re.sub(r"^- \[[ x]\]", "- [x]", lines[found], count=1)
        atomic_write(path, "\n".join(lines).rstrip() + "\n")


def append_event(user_id: str, name: str, line: str) -> None:
    base = workspace(user_id)
    path = _safe_path(base, name)
    with locked(base):
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        atomic_write(path, existing + line.rstrip("\n") + "\n")
