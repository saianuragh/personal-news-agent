# Personal News Intelligence Agent

A small Python application that turns free RSS/Atom feeds into a concise, ranked morning newspaper. It fetches and normalizes entries, deduplicates stories, categorizes and ranks them, optionally e[...]

## Daily newspaper

The editorial sections are India, World, AI, Technology, Business & Economy, Science & Space, Sports, and Entertainment. The selection policy limits the edition to 24 stories and no more than five [...]

## Architecture

```text
GitHub Actions or Windows Task Scheduler
  → CLI → source configuration → RSS/Atom adapters
  → normalization → deduplication → categorization
  → ranking → selection → optional LLM enrichment/fallback
  → HTML + plain-text newspaper → SMTP
```

The stages have small boundaries: source adapters handle network/feed errors, processing is deterministic, LLM enrichment is replaceable and optional, the renderer owns presentation, and email pro[...]

## Setup

Use Python 3.12 or newer. In PowerShell from the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m app.cli config-check
```

The ignored `.env` contains local settings. Store the Gmail app password in Windows Credential Manager with `personal-news-agent email-credential-set`; do not put it in `.env`, command arguments, [...]

## Preview and send

```powershell
.\.venv\Scripts\personal-news-agent.exe preview
```

Preview fetches and processes the feeds, writes HTML and plain-text files under `data/previews/`, and never sends email. To deliver intentionally:

```powershell
.\.venv\Scripts\personal-news-agent.exe send
```

`send` is a real email action. The Windows wrapper and GitHub Actions invoke this same CLI mode.

## Scheduling

The GitHub Actions workflow runs at `01:30 UTC`, equivalent to **07:00 IST**. It uses the repository secrets `NEWSLETTER_RECIPIENT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `EMAIL_FROM`; `LLM_API_KE[...]

Manual dispatch is available with a `send` input (default: false). When false, only config validation runs and no email is sent. When true, the newsletter sends immediately.

### Preventing schedule disablement

GitHub automatically disables scheduled workflows after 60 days of repository inactivity. To prevent this:

- **Cloud option**: A separate keepalive workflow runs monthly and re-enables the daily-newsletter schedule via the GitHub Actions API.
- **Private repository option**: Making the repository private also avoids the 60-day inactivity rule entirely.

The existing Windows Task Scheduler task is a local fallback. Keep it enabled until the cloud workflow has been validated; disable it before treating GitHub Actions as the sole scheduler. Details [...]

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
```

See [architecture](docs/architecture.md), [configuration](docs/configuration.md), [requirements](docs/requirements.md), and [roadmap](docs/roadmap.md) for implementation details and current limita[...]
