# Career Recommendations integration

CareerCompassAI's database is the system of record for candidate profiles,
job/application rows, pipeline URLs, feedback, generated materials, authorizations,
and submission attempts. Hanggent authenticates the candidate and exchanges its
session for a 60-second, audience-limited `career_bridge` token; the API gateway and
job service independently validate that token. The gateway only proxies explicitly
registered career routes to the private job service.

Career-ops remains an adapter and interchange format, not a second live database. The
`GET/POST /career-workspace/pipeline` inbox is stored in `career_pipeline_entries`
with per-user URL deduplication; an evaluated or applied application marks the
matching URL processed. Existing career-ops Markdown files are read only during
an explicit user-initiated import. They never overwrite database records during
ordinary reads, and no shared server-side career-ops CLI writes user files.

The current adapters preserve their origins:

| Source | Contribution |
| --- | --- |
| CareerCompassAI | Job discovery, matching and canonical pipeline |
| Career Ops | Evaluation reports and legacy application tracker |
| AI Job Search | Tailored application packages |
| Proficiently | Existing job/application folders and candidate materials |

Hanggent's explicit sync copies the signed-in user's Career Ops and Proficiently
records. It never reads another user's workspace and never deletes the originals.
Recommendation runs also upsert discovered roles and save generated package versions.
Rows are deduplicated by normalized public job URL, or by company/role/location when
no URL is available. A URL-bearing record and a URL-less record are not assumed to
be identical.

Submission modes share one durable policy model:

- `confirm_each` authorizes exactly one application and one allowed site.
- `auto` authorizes a bounded campaign (site allowlist, minimum fit score, maximum
  10 applications, expiry within 24 hours, and immediate revocation).

Hanggent's browser worker claims a queued attempt with a 10-minute lease, opens
one new tab in the candidate's connected browser, and rechecks authorization
before clicking a final submit control. It fills only basic identity fields and
an optional transient PDF resume. Login, CAPTCHA, unfamiliar required fields,
and legal attestations require candidate action; no passwords or cookies enter
the canonical database. The worker records `submitted` only when it observes a
known confirmation phrase on the authorized site. A click without such a receipt
is `uncertain` and cannot be retried until the candidate checks the employer site
and explicitly resolves it. Expired worker leases also become `uncertain`.

This is a guarded best-effort adapter, not a guarantee that every employer form
is supported. Its page selectors have not been validated against live logged-in
Indeed, LinkedIn, Greenhouse, Lever, and Workday accounts. The API receipt is
candidate-scoped but is not a cryptographic attestation from the employer site.
Run an end-to-end test with a permitted test listing before enabling an automatic
campaign on staging or production. Web mode additionally requires verified
Hanggent Brain identity auth and an owner-scoped browser resource; Electron mode
requires an available embedded-browser CDP target.
