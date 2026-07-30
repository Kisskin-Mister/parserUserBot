# Vacancy pipeline: n8n → AI review → browser preparation

This subsystem extends `parserUserBot` without replacing its existing Telegram
forwarder. It stores every forwarded event in an auditable intake table, keeps
obvious garbage out of the working list, uses a strict structured AI verdict for
plausible vacancies, and prepares browser applications behind explicit approval
gates.

It does **not** bypass CAPTCHA and final submission is disabled by default.

## Data flow

```text
Telegram user-bot → target channel → n8n Telegram Trigger
  → POST /vacancy-events → vacancy_intake
  → vacancy-worker → rules → structured AI judge → job_vacancies
  → human browser-preparation approval → applications
  → browser-worker → second page verdict → deterministic/AI-assisted field map
  → form read-back + screenshot → exact payload/resume approval
  → optional one-click submit → business confirmation + submission_proofs
```

The raw intake table is the audit log. `GET /job-vacancies` excludes garbage by
default, so spam remains inspectable without polluting the working queue.

## Components

- `vacancy_pipeline/migrations.py` — idempotent PostgreSQL schema and safety triggers.
- `vacancy_pipeline/repository.py` — transactional claims, leases, state transitions,
  exact approvals, events, CAPTCHA checkpoints and submission proof.
- `vacancy_pipeline/ai_judge.py` — strict JSON OpenAI-compatible vacancy evaluator
  with a database-backed cache.
- `vacancy_pipeline/rules.py` — cheap obvious-garbage prefilter and Nazar policy.
- `vacancy_pipeline/api.py` — intake, review, application, approval, CAPTCHA and
  evidence endpoints.
- `scripts/run_vacancy_worker.py` — asynchronous intake evaluator.
- `browser_worker/` — optional browser state machine and Playwright runtime.
- `n8n/telegram-vacancy-ingest.json` — importable n8n workflow.

## Required environment

Start from `.env.example`. At minimum the API and vacancy worker require existing
PostgreSQL settings plus:

```dotenv
ADMIN_TOKEN=replace_with_a_long_random_value
OPENAI_BASE_URL=https://provider.example/v1
OPENAI_API_KEY=replace_me
OPENAI_MODEL=structured-json-capable-model
AI_MAX_CALLS_PER_BATCH=20
VACANCY_WORKER_POLL_SECONDS=3
VACANCY_WORKER_LEASE_SECONDS=300
RESUME_ALLOWLIST_ROOTS=/app/resumes
```

Never put credentials in the n8n JSON. Create credentials inside n8n:

1. Telegram credential for the bot that sees posts in the target channel.
2. HTTP Bearer Auth credential containing the `ADMIN_TOKEN` value only.
3. Set `PARSERUSERBOT_BASE_URL`, for example `http://api:8000` when n8n can
   resolve the Compose network service.

## Import n8n workflow

1. Open n8n → **Workflows** → **Import from file**.
2. Import `n8n/telegram-vacancy-ingest.json`.
3. Select the Telegram credential in **Telegram Trigger**.
4. Select the Bearer credential in **POST Vacancy Event**.
5. Verify `PARSERUSERBOT_BASE_URL`.
6. Activate only after a manual test post.

The Code node normalizes forwarded metadata and creates the canonical key:

```text
telegram:<target_chat_id>:<target_message_id>
```

Repeated delivery returns `duplicate` and does not create a second intake row.

## Run API and vacancy worker

```bash
docker compose up -d --build postgres api vacancy-worker worker
curl http://localhost:${APP_PORT:-8000}/health
```

For a one-batch local diagnostic:

```bash
python -m scripts.run_vacancy_worker --once
```

All mutating pipeline endpoints require:

```text
Authorization: Bearer <ADMIN_TOKEN>
```

## Review flow

Working list, excluding garbage:

```bash
curl http://localhost:8000/job-vacancies
```

Explicit garbage audit:

```bash
curl 'http://localhost:8000/job-vacancies?verdict=garbage'
```

A vacancy must be approved for browser preparation before an application can be
claimed. Browser preparation stores a canonical payload hash and exact resume
SHA-256. Any content change invalidates previous approval.

Exact submit approval uses:

```text
POST /applications/{id}/approve-exact
{
  "payload_hash": "<current form_payload_hash>",
  "resume_sha256": "<current resume_sha256>"
}
```

Approval is tied to the exact application, rendered payload and resume bytes. It
cannot authorize another vacancy or a changed form.

## Browser worker

Browser dependencies are separate from the core service:

```bash
pip install -r requirements-browser.txt
docker compose --profile browser up -d --build browser-worker
```

Candidate PII lives in a local JSON file referenced by
`BROWSER_CANDIDATE_PROFILE_PATH`; do not commit it. Resume files must be mounted
read-only and explicitly allowlisted.

Final click remains disabled unless both are set:

```dotenv
BROWSER_FINAL_SUBMIT_ENABLED=true
BROWSER_FINAL_SUBMIT_ACK=SUBMIT_APPROVED_APPLICATIONS
```

Even then the database requires matching exact approval before entering
`submitting`. The worker clicks at most once. If the result becomes ambiguous
after the click, the application moves to `needs_manual_verification` and is
never automatically retried.

## CAPTCHA

CAPTCHA is a human checkpoint, not an automated solver:

1. Worker records `captcha_required` with URL, screenshot and persistent session key.
2. Human opens the same persistent browser session and solves the challenge.
3. Human resolves or aborts the checkpoint through the authenticated API.
4. Worker reopens/reuses the session and checks CAPTCHA again.
5. CAPTCHA still present → another checkpoint; no submit.

The durable queue is available at:

```text
GET  /captcha-checkpoints?state=waiting_human
POST /captcha-checkpoints/{id}/resolve
```

## Evidence and idempotency

`is_submitted=true` is impossible unless the same PostgreSQL transaction inserts
matching submission proof. Database triggers require:

- application state was `submitting`;
- approved payload hash equals current payload hash;
- approved resume hash equals current resume hash;
- proof contains the same hashes;
- explicit confirmation text or provider application ID exists.

Audit endpoints:

```text
GET /applications/{id}/events
GET /applications/{id}/proof
GET /llm-calls
```

## Verification

```bash
.venv-community/bin/ruff check .
python -m pytest -q
python -m compileall -q .
docker compose config --quiet
python -m json.tool n8n/telegram-vacancy-ingest.json >/dev/null
```

Browser tests use injected fake runtimes and never access a vacancy site or click
a real submit button.
