# Data-Source Licensing & Usage Gates

This engine ingests third-party data (PSX market/fundamentals, multiple news APIs).
Each source carries its own terms. **These are legal gates, independent of the code or
where it runs — settle them before any commercial / production use.** This document is
an engineering checklist, not legal advice; confirm current terms with each provider and
your counsel.

Status legend: 🟥 blocker for commercial use · 🟧 conditional · 🟩 permissive (verify).

## PSX (Pakistan Stock Exchange) — `dps.psx.com.pk`, `www.psx.com.pk`
- **Adapters:** `symbols`, `company_overview`, `company_payouts`, `announcements`,
  `analysis_reports`, market/sector summaries, `psx` quote.
- 🟥 PSX pages/datasets carry an **"Unauthorized Use of PSX Data"** notice. Scraping and
  redistribution for a commercial product is **not** covered by personal/dev access.
- **Action:** obtain a PSX data license / market-data agreement before commercial launch.
  Until then, restrict to internal evaluation. Also note the technical risk: PSX may block
  datacenter IPs — verify reachability from the deploy environment
  (`scripts/psx_adapters_smoke.py`).

## News providers (failover order in `news_providers.PROVIDERS`)
Free-tier keys live in env vars (`NEWS_*_KEY`); each tier has its own commercial terms,
rate limits, and attribution rules. Verify per provider — these change.

| Provider | Env var | Note |
|---|---|---|
| marketaux | `NEWS_MARKETAUX_KEY` | 🟧 free tier limited; commercial needs a paid plan — verify |
| finnhub | `NEWS_FINNHUB_KEY` | 🟧 free = personal/non-commercial; commercial plan for production |
| alphavantage | `NEWS_ALPHAVANTAGE_KEY` | 🟧 free tier rate-limited; check commercial terms |
| newsdata.io | `NEWS_NEWSDATA_IO_KEY` | 🟧 attribution + plan limits |
| newsapi.ai (Event Registry) | `NEWS_NEWSAPI_AI_KEY` | 🟧 commercial requires paid plan |
| worldnewsapi | `NEWS_WORLDNEWSAPI_KEY` | 🟧 plan-gated |
| gnews.io | `NEWS_GNEWS_IO_KEY` | 🟧 free = dev; paid for production volume |
| **newsapi.org** | `NEWS_NEWSAPI_ORG_KEY` | 🟥 **free tier is development-only / non-commercial**; needs a paid plan for a deployed app — or drop it and rely on the rest of the failover chain |

- 🟥 **newsapi.org** is the one hard blocker for a deployed app on the free tier.
- **Action:** for production, either upgrade the providers you depend on to commercial
  plans or disable those whose free terms forbid production use (the failover chain keeps
  working with the remaining providers). Preserve any required source attribution in the
  rendered citations (the engine already keeps `source`/`author`/`link`).

## Article content & the LLM
- Retrieved article text is fed to the LLM only as **sanitized, delimited data** (see
  `security.sanitize_external_text`) and surfaces with per-article citations. Storing or
  redistributing full article bodies may exceed "headline/snippet" license scope — keep
  only what the provider's terms allow.

## OpenAI (LLM)
- `OPENAI_API_KEY`. Standard API terms apply; the engine sends query + workbook-derived
  figures + sanitized external snippets. Confirm data-handling terms meet your
  confidentiality requirements before sending client financials.

## Pre-launch licensing checklist
- [ ] PSX commercial data license in place (or PSX adapters disabled).
- [ ] newsapi.org on a paid plan **or** disabled.
- [ ] Each remaining news provider confirmed for commercial use at expected volume.
- [ ] Source attribution preserved in shipped citations.
- [ ] OpenAI data-handling terms reviewed for client financial data.

See also `backend/SECURITY.md` (deploy/ops checklist).
