from app.comparison_evidence import parse_score_evidence


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
