# Career Recommendations integration

CareerCompassAI owns the candidate-scoped application, artifact, authorization, and
submission-attempt records. Hanggent authenticates the candidate and exchanges its
session for a 60-second, audience-limited `career_bridge` token; the API gateway and
job service independently validate that token. The gateway only proxies explicitly
registered career routes to the private job service.

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

Both modes currently create **queued attempts only**. They do not submit to a
third-party site. A browser worker must subsequently claim an attempt, confirm
the authorization is still live, use an owned candidate browser session, pause on
login/CAPTCHA/identity/legal attestations, and record a verifiable site receipt
before changing status to `submitted`. A worker and receipt-validation endpoint
must be added before either mode is exposed as automated submission in the UI.
Passwords and browser cookies must remain outside the canonical database.
