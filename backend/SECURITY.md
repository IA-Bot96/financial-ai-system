# Security & Deployment Hardening

What is implemented in code, and the operator checklist for a production (e.g. Heroku)
deploy. Auth (per-user identity / RBAC) is **not** yet implemented — see "Deferred".

## Implemented (Tier A — in code)

| Concern | Where | Notes |
|---|---|---|
| Prompt injection | `app/core/security.sanitize_external_text`, `synthesis.build_graph`, `_SYS` | Retrieved news/filing text is sanitized (role-impersonation & "ignore previous" lines, fences, control chars stripped; length-capped) and fed as **data**; system prompt forbids following instructions inside quoted text. Still backed by `safety.py` numeric guard + "no citation, no claim". |
| Query / body size | `routes/fie.AnswerRequest`, `BodySizeLimitMiddleware` | Query capped at `FIE_MAX_QUERY_CHARS` (default 256); JSON body capped at `MAX_REQUEST_BYTES` (413). |
| External-call budget | `ApiClient.begin_request` / per-request thread-local | Hard cap (`MAX_EXTERNAL_CALLS_PER_REQUEST`) on adapter fan-out per query; fails closed. |
| LLM token/cost cap | `llm.OpenAILLM` | Input truncated to `LLM_MAX_INPUT_CHARS`; completion capped at `LLM_MAX_OUTPUT_TOKENS`. |
| Request timeout | `TimeoutMiddleware` | 504 after `REQUEST_TIMEOUT_SECONDS`. |
| File-upload safety | `security.assert_safe_upload`, `routes/extraction` | Size cap (`MAX_UPLOAD_BYTES`, 150 MB), extension+magic-byte check, macro-format reject, xlsx zip-bomb (uncompressed size + ratio) and VBA-project reject — **before** parse. |
| SSRF / identifier | `security.url_safe_param`, `ApiClient.call` | Any value templated into a URL path is validated (no separators/traversal) or the fetch is refused. Tickers/years validated via `validate_ticker` / `validate_year`. |
| Rate limiting | `RateLimitMiddleware` + `TokenBucket` | Per-IP burst `RATE_LIMIT_CAPACITY` (3), refill 1 / `RATE_LIMIT_REFILL_SECONDS` (1s) → 429. Stopgap until per-user auth; in-memory (single-process). |
| CORS | `main.py` from `CORS_ALLOW_ORIGINS` | Pinned origins; methods limited to GET/POST. |
| Security headers | `SecurityHeadersMiddleware` | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`; HSTS when `FORCE_HTTPS=true`. |
| Error sanitization | `main._unhandled` | Unhandled errors → generic 500 + `request_id`; full detail logged server-side only. |
| Secret redaction | `security.RedactingFilter` | API-key/token-shaped substrings scrubbed from logs. |
| Per-provider quota | `apis/news._quota` + `DailyQuota` | Daily call ceiling per news provider (`NEWS_DAILY_CALL_CAP_PER_PROVIDER`) to cap free-tier/paid spend. |
| Contract integrity | `bootcheck.assert_contracts` | Startup aborts on a mis-wired formula/authority/taxonomy/citation contract. |

## Operator checklist (before deploy)

1. **Secrets** → set every key as a Heroku config var (`heroku config:set OPENAI_API_KEY=… NEWS_*_KEY=…`). Never commit `.env` (already git-ignored). `.env.example` documents all vars.
2. **HTTPS** → `FORCE_HTTPS=true` (emits HSTS). Heroku terminates TLS at the router; rely on that + HSTS.
3. **CORS** → `CORS_ALLOW_ORIGINS=https://your-frontend` (no `*`).
4. **Rate limit** → set `REDIS_URL` for a shared limiter across dynos (auto-selected; falls back to in-process if unset/unreachable). Tune `RATE_LIMIT_CAPACITY` / `RATE_LIMIT_REFILL_SECONDS`.
5. **Dependency scan** → run `scripts/security_audit` (pip-audit) in CI; patch findings. Pin versions in `requirements.txt`.
6. **PSX connectivity** → run `scripts/psx_adapters_smoke.py` (all PSX adapters) and `scripts/analysis_reports_smoke.py` *from a dyno* (datacenter IPs may be blocked); add a static-IP proxy add-on if a provider needs allowlisting.
7. **Deploy** → `Procfile` runs `uvicorn app.main:app`. Health/ops probes: `/liveness`, `/readiness` (workbooks + secret presence + limiter backend), `/metrics` (Prometheus text).

### Operability endpoints
- `GET /liveness` — process up.
- `GET /readiness` — 200 when a workbook is present + boot contracts passed; reports `secrets` presence (booleans, never values) and the active `rate_limiter` backend; 503 otherwise.
- `GET /metrics` — Prometheus text: `fie_queries_total{intent,confidence}`, `fie_answer_seconds_{sum,count}`, `fie_degraded_total`, `fie_claims_dropped_total`, `fie_insufficient_total`, `app_unhandled_errors_total`.

## Licensing gates (legal, not code)

Full detail + a pre-launch checklist in [`../docs/LICENSING.md`](../docs/LICENSING.md). Summary:

- **PSX data** (`dps.psx.com.pk`) carries an "Unauthorized Use of PSX Data" notice — settle a license before commercial use, regardless of host.
- **newsapi.org** free tier is **development-only / non-commercial** — needs a paid plan for a deployed app, or rely on the other providers in the failover chain.
- Each of the other 7 news providers has its own free-tier commercial terms — verify per provider.

## Deferred (needs auth)

Per-user rate limit/quota, tenant isolation, RBAC on `/api/extraction` (file ops),
audit-by-user. Until then, per-IP rate limiting is the stopgap.
