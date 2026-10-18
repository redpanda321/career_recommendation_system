"""Conservative extraction of the five career-ops score-evidence dimensions.

Only a complete, bounded table is snapshotted. The rows are model-generated
claims for candidate review, not independently verified facts.
"""

import re


DIMENSIONS = (
    "CV match", "North Star alignment", "Compensation", "Cultural signals", "Red flags",
)
STATUSES = {"supported", "partial", "unknown"}


def parse_requirement_evidence(report: str) -> list[dict[str, str]] | None:
    """Extract the single upstream Block B matrix, not a second generated list."""
    match = re.search(r"^## B\) Match with CV\s*$", report, flags=re.MULTILINE)
    if not match:
        return None
    section = re.split(r"^##\s+", report[match.end():], maxsplit=1, flags=re.MULTILINE)[0]
    if len(section) > 16000:
        return None
    lines = section.splitlines()
    header = "| Requirement | Importance | Match | JD signal | Evidence / gap |"
    if sum(line.strip() == header for line in lines) != 1:
        return None
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        return None
    if start + 1 >= len(lines):
        return None
    separators = [cell.strip() for cell in lines[start + 1].strip().strip("|").split("|")]
    if len(separators) != 5 or any(not re.fullmatch(r":?-{3,}:?", cell) for cell in separators):
        return None
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in lines[start + 2:]:
        if not line.strip().startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not all(cells):
            return None
        importance = re.fullmatch(
            r"(critical|high|meaningful|preferred|low_signal) \((stated|structural|inferred)\)",
            cells[1], flags=re.IGNORECASE,
        )
        matched = re.fullmatch(r"(?:(✅|⚠️|❌|➖)\s*)?(Strong|Partial|Missing|N/A)",
                               cells[2], flags=re.IGNORECASE)
        if not importance or not matched:
            return None
        if matched.group(1) and matched.group(1) != {
            "strong": "✅", "partial": "⚠️", "missing": "❌", "n/a": "➖",
        }[matched.group(2).casefold()]:
            return None
        band, tier = (part.casefold() for part in importance.groups())
        if tier == "inferred" and band in {"critical", "high"}:
            return None
        if tier == "stated" and cells[3] in {"—", "-"}:
            return None
        requirement = cells[0]
        if (requirement.casefold() in seen or len(requirement) > 200
                or any(len(value) > 300 or any(ord(char) < 32 for char in value)
                       for value in cells[3:])):
            return None
        seen.add(requirement.casefold())
        normalized_match = matched.group(2)
        rows.append({"requirement": requirement, "importance": band,
                     "importance_evidence": tier, "match": "N/A" if normalized_match.casefold() == "n/a"
                     else normalized_match.capitalize(),
                     "jd_signal": cells[3], "candidate_evidence": cells[4]})
        if len(rows) > 20:
            return None
    return rows or None


def parse_score_evidence(report: str) -> list[dict[str, str]] | None:
    match = re.search(r"^## Score Evidence\s*$", report, flags=re.MULTILINE)
    if not match:
        return None
    section = re.split(r"^##\s+", report[match.end():], maxsplit=1, flags=re.MULTILINE)[0]
    if len(section) > 12000:
        return None
    found: dict[str, dict[str, str]] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4:
            continue
        dimension = next((value for value in DIMENSIONS if value.casefold() == cells[0].casefold()), None)
        if dimension is None:
            continue
        status = cells[1].casefold()
        if dimension in found or status not in STATUSES or not all(cells[2:]):
            return None
        if any(len(cell) > 300 or any(ord(char) < 32 for char in cell) for cell in cells[2:]):
            return None
        found[dimension] = {"dimension": dimension, "status": status,
                            "observation": cells[2], "open_question": cells[3]}
    if len(found) != len(DIMENSIONS):
        return None
    return [found[dimension] for dimension in DIMENSIONS]
