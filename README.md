# Personal News Intelligence Agent

A small Python application that turns free RSS/Atom feeds into a concise, ranked morning newspaper. It fetches and normalizes entries, deduplicates stories, categorizes and ranks them, optionally enriches them with an LLM, renders HTML and plain text, and can deliver both formats by SMTP. The LLM is optional; source-description summaries keep the newspaper usable when model configuration or service is unavailable.

## Daily newspaper

The editorial sections are India, World, AI, Technology, Business & Economy, Science & Space, Sports, and Entertainment. The selection policy limits the edition to 24 stories and no more than five per category. The renderer derives Top Stories, Important Today, AI Watch, and a source-attributed Fact of the Day from selected feed data. Market Snapshot is omitted because no market-data feed is configured.

## Architecture

```text
GitHub Actions or Windows Task Scheduler
  → CLI → source configuration → RSS/Atom adapters
  → normalization → deduplication → categorization
  → ranking → selection → optional LLM enrichment/fallback
  → HTML + plain-text newspaper → SMTP
```

The stages have small boundaries: source adapters handle network/feed errors, processing is deterministic, LLM enrichment is replaceable and optional, the renderer owns presentation, and email providers own delivery. The scheduled workflow needs no database, Docker, or paid news API. It does not persist run history or delivery claims; avoid rerunning an ambiguous send because the email provider may have accepted it even when the response was lost.

## Setup

Use Python 3.12 or newer. In PowerShell from the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m app.cli config-check
```

The ignored `.env` contains local settings. Store the Gmail app password in Windows Credential Manager with `personal-news-agent email-credential-set`; do not put it in `.env`, command arguments, or source code. An LLM key is optional.

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

The GitHub Actions workflow runs at `01:30 UTC`, equivalent to **07:00 IST**. It uses the repository secrets `NEWSLETTER_RECIPIENT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `EMAIL_FROM`; `LLM_API_KEY` is optional. `workflow_dispatch` also runs the real send path. Do not rerun a production workflow with an uncertain email outcome.

The existing Windows Task Scheduler task is a local fallback. Keep it enabled until the cloud workflow has been validated; disable it before treating GitHub Actions as the sole scheduler. Details are in [cloud deployment](docs/cloud-deployment.md) and [local scheduling](docs/free-local-deployment.md).

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
```

See [architecture](docs/architecture.md), [configuration](docs/configuration.md), [requirements](docs/requirements.md), and [roadmap](docs/roadmap.md) for implementation details and current limitations.
