# Career Recommendations integration

CareerCompassAI owns the candidate-scoped career-ops user files, application query
index, authorization, and submission-attempt records. Hanggent authenticates the candidate and exchanges its
session for a 60-second, audience-limited `career_bridge` token; the API gateway and
job service independently validate that token. The gateway only proxies explicitly
registered career routes to the private job service.

## File-first user workspaces

When `CAREER_OPS_DATA_ROOT` is configured, the job service stores each candidate's
files under `<root>/users/<sha256(user-id)>/`. The directory is a dedicated retained
PVC in Contabo staging; it is never inside the vendored career-ops checkout. The
single job-service replica uses a cross-process file lock and atomic replacement.
The deployment uses `Recreate` so two pods do not mount the same RWO volume during
rollout. Back up the PVC independently from PostgreSQL.

The career-ops user-layer paths are `cv.md`, `config/profile.yml`,
`data/applications.md`, `data/pipeline.md`, `data/status-log.tsv`,
`data/follow-ups.md`, and `reports/`.
`applications.md` uses upstream's canonical columns and states; Hanggent's stable
UUID is carried in a Notes receipt. A small `data/hanggent/<id>.json` sidecar holds
fields that upstream's tracker does not represent. Existing DB rows are copied into
the tracker once; the migration completion marker prevents deleted file rows from
being resurrected on later reads. Native career-ops tracker rows are indexed with
deterministic IDs. Reads reindex changed tracker rows, so direct file edits to
company, role, score, or stage take precedence over the database. Application
deletion from the tracker hides the row from API reads and blocks new mutations or
submission attempts; historical authorization and audit rows remain in SQL.

Reports are written to `reports/` before their SQL metadata is committed, and
activity reads the report file when present. Stage changes and confirmed employer
contacts are also recorded in career-ops' user files. The SQL database still holds
submission authorizations, browser-worker leases, and immutable attempt receipts.
The pipeline inbox has `GET /career-workspace/pipeline` and
`POST /career-workspace/pipeline` endpoints; an unreviewed discovered URL is
pending, while a career-ops evaluation marks the URL processed.
These operational records are not interchangeable with the application tracker.
Do not run career-ops CLI write commands against the shared service process or
pass a candidate workspace path to another candidate's session.

The file migration is intentionally scoped: the current bridge does not yet expose
career-ops' scanner, TUI, plugins, salary/contacts
analysis, or every specialized mode. A separate worker and reviewable UI are needed
for those capabilities; this API does not claim to execute them.

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
