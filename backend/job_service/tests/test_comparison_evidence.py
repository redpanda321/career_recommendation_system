from app.comparison_evidence import parse_requirement_evidence, parse_score_evidence


REPORT = """## Score Evidence
| Dimension | Status | Observation | Open question |
| --- | --- | --- | --- |
| CV match | supported | Python on resume | Confirm depth |
| North Star alignment | partial | Target role aligns | Confirm scope |
| Compensation | unknown | Pay absent | Ask salary |
| Cultural signals | unknown | No team data | Ask manager |
| Red flags | partial | Posting date absent | Check liveness |
## H) Other
"""


def test_complete_evidence_is_bounded_and_in_canonical_order():
    rows = parse_score_evidence(REPORT)
    assert [row["dimension"] for row in rows] == [
        "CV match", "North Star alignment", "Compensation", "Cultural signals", "Red flags",
    ]
    assert rows[0]["observation"] == "Python on resume"


def test_missing_duplicate_or_oversized_evidence_is_unavailable():
    assert parse_score_evidence(REPORT.replace("| Compensation | unknown | Pay absent | Ask salary |\n", "")) is None
    assert parse_score_evidence(REPORT.replace("## H) Other", "| CV match | partial | Other | Why? |\n## H) Other")) is None
    assert parse_score_evidence(REPORT.replace("Python on resume", "x" * 301)) is None


REQUIREMENTS = """## B) Match with CV
| Requirement | Importance | Match | JD signal | Evidence / gap |
| --- | --- | --- | --- | --- |
| Python APIs | critical (stated) | ✅ Strong | Must build Python APIs | CV: Built Python APIs |
| SQL | high (structural) | ⚠️ Partial | Listed in responsibilities | CV mentions SQL, depth unclear |
| Onsite location | meaningful (inferred) | ➖ N/A | — | Location needs candidate confirmation |
## C) Level and Strategy
"""


def test_upstream_block_b_requirements_are_extracted_once_with_bounded_fields():
    rows = parse_requirement_evidence(REQUIREMENTS)
    assert len(rows) == 3
    assert rows[0]["importance"] == "critical"
    assert rows[0]["importance_evidence"] == "stated"
    assert rows[1]["match"] == "Partial"
    assert rows[2]["match"] == "N/A"


def test_malformed_duplicate_or_inferred_high_requirements_are_unavailable():
    assert parse_requirement_evidence(REQUIREMENTS.replace("high (structural)", "high (inferred)")) is None
    assert parse_requirement_evidence(REQUIREMENTS.replace("## C) Level and Strategy",
        "| Python APIs | high (structural) | Strong | Responsibilities | CV: APIs |\n## C) Level and Strategy")) is None
    assert parse_requirement_evidence(REQUIREMENTS.replace("CV: Built Python APIs", "x" * 301)) is None
    assert parse_requirement_evidence(REQUIREMENTS.replace("✅ Strong", "❌ Strong")) is None
    assert parse_requirement_evidence(REQUIREMENTS.replace("## C) Level and Strategy",
        "| Requirement | Importance | Match | JD signal | Evidence / gap |\n## C) Level and Strategy")) is None
