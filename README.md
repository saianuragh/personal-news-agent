# Personal News Intelligence Agent

A Python application that turns configured RSS/Atom entries into a ranked personal briefing. It validates and normalizes feed data, conservatively deduplicates articles, assigns deterministic categories, ranks and selects stories, optionally asks an OpenAI-compatible provider to summarize selected stories, and writes an HTML and plain-text newsletter. SQLite is used locally; the cloud production workflow uses persistent PostgreSQL for run and delivery-claim metadata.

## Architecture

```text
GitHub Actions (daily at 07:00 IST) → app.cli send
  → PostgreSQL delivery ledger

Windows Task Scheduler → scripts/run_newsletter.ps1 → app.cli (local fallback)
  → configuration / PipelineRunner → RSS/Atom adapters
  → normalize → deduplicate → categorize → rank → select
  → optional LLM enrichment/fallback → HTML + text renderer
  → local preview or optional email → persistent run metadata
```

This is a modular monolith, not a collection of services. Source adapters emit `RawProviderEntry`; normalization owns validation and creates `Article`. Core processing is deterministic and provider independent. The enabled RSS sources are BBC News World and The Guardian World (`config/sources.yaml`); both use the same adapter.

## Prerequisites and setup

Use Python 3.12 or newer. In PowerShell from the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m app.cli config-check
.\.venv\Scripts\python.exe -m app.cli database-init
```

The `.env` file is local and ignored by Git. Review [configuration](docs/configuration.md); do not put credentials in command arguments. Core preview does not require email or LLM credentials.

## Run a preview

```powershell
.\.venv\Scripts\python.exe -m app.cli preview
```

This fetches enabled feeds, runs the pipeline, persists run metadata, and writes `data/previews/newsletter-YYYY-MM-DD.html` and `.txt`. It does not send email or call an LLM. `preview --with-ai` enables optional enrichment for selected stories; provider failures use the existing source-description fallback or omit stories without usable text. AI output is editorial assistance, not verification or source of truth.

Useful commands:

```powershell
.\.venv\Scripts\personal-news-agent.exe --help
.\.venv\Scripts\personal-news-agent.exe config-check
.\.venv\Scripts\personal-news-agent.exe database-init
.\.venv\Scripts\personal-news-agent.exe runs --limit 10
.\.venv\Scripts\personal-news-agent.exe preview
.\.venv\Scripts\personal-news-agent.exe delivery-reset --date 2026-10-02
```

`delivery-reset` is an explicit development/testing utility for removing the configured recipient's delivery claim for one supplied local briefing date. It never sends mail or runs the news pipeline. Use it only when you intentionally need to repeat a test delivery; do not add it to the daily scheduler. The reset utility does not change the scheduler's configured mode. Whenever `send` is used, its normal recipient/date idempotency protection remains in force.

`personal-news-agent send` is an explicit delivery mode and should only be used after intentionally configuring and validating an email provider. No email service is required for local newsletter generation.

## Production Daily Automation

GitHub Actions runs the production `send` pipeline remotely every day at **07:00 Asia/Kolkata** (`01:30 UTC`) using a hosted Ubuntu runner and Neon PostgreSQL. The laptop can be powered off, disconnected, or logged out; it is not part of production execution. Add the required GitHub Actions secrets and follow [cloud deployment](docs/cloud-deployment.md) to configure services, test the same production job manually, and inspect run logs. Scheduled execution is best-effort and may be delayed by GitHub Actions.

## Local fallback

The Windows wrapper resolves the project root, invokes `.venv\Scripts\personal-news-agent.exe send` by default, writes per-run logs under `data/logs/`, and propagates the CLI exit code. The **Personal News Agent - Daily Newsletter** Task Scheduler task is registered for 07:00 India Standard Time on this Windows PC. Recreate or verify it with the steps in [free local deployment](docs/free-local-deployment.md). The PC must be powered on, connected, and the Dell account signed in at run time; a terminal does not need to be open.

## Development and verification

Install development tools and run checks:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
```

CI runs pytest and Ruff on Python 3.12 and needs no application secrets. See [architecture](docs/architecture.md), [requirements and current scope](docs/requirements.md), [cloud deployment](docs/cloud-deployment.md), and [roadmap](docs/roadmap.md).

## Current limits and roadmap

V1 currently enables two RSS feeds: BBC News World and The Guardian World. Deduplication uses exact canonical/normalized URL and content-hash signals; fuzzy title matching is not implemented. Stories and newsletter bodies are not persisted, only run metadata and delivery claims. There is no configurable article lookback filter, LLM categorization, or guarantee that every category is populated. RSS and optional model availability affect coverage and enrichment. See [requirements](docs/requirements.md) for explicit implemented behavior and deferrals.

The cloud deployment is free-first and usage-limited; it requires GitHub/Neon setup and secrets before remote automation is active. The local SQLite/Windows setup remains available as a fallback. [Cloud Run deployment notes](docs/deployment.md) describe an optional paid alternative, not the selected production path.

