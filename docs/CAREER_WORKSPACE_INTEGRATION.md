# Career workspace integration

CareerCompassAI can now combine two local, opt-in exports with `POST
/career-workspace/preview`:

- **AI Job Search** supplies search and application-tracker entries, including
  fit ratings, deadlines, contacts, and sourcing information.
- **Career Ops** supplies pipeline progress and its assessment score.

The endpoint returns a normalized `saved → applied → screening → assessment →
interview → offer → accepted/closed` view.  Equal company-and-role records are
deduplicated; the furthest pipeline stage and strongest available fit score are
retained, while both source labels remain visible.

## Privacy and control

This is intentionally an import-preview API. It reads neither local project,
profile, tracker, nor application-material files. A user or client must export
the selected records and send them in the request body. The API does not persist
data, update either source tracker, or submit any application to an employer.

## Example

```json
{
  "ai_job_search": [
    {
      "company": "Example Labs",
      "role": "ML Engineer",
      "status": "applied",
      "fit_rating": 82,
      "deadline": "2026-11-01",
      "job_url": "https://jobs.example.com/ml-engineer"
    }
  ],
  "career_ops": [
    {
      "company": "Example Labs",
      "role": "ML Engineer",
      "status": "interview",
      "score": 4.5
    }
  ]
}
```

`score` values from 0–5 are converted to percentages. Scores above 5 are treated
as percentages and are clamped to 0–100.
