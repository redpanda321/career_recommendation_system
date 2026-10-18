"""Conservative extraction of the five career-ops score-evidence dimensions.

Only a complete, bounded table is snapshotted. The rows are model-generated
claims for candidate review, not independently verified facts.
"""

import re


DIMENSIONS = (
    "CV match", "North Star alignment", "Compensation", "Cultural signals", "Red flags",
)
STATUSES = {"supported", "partial", "unknown"}


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
