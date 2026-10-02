# Architecture

The project is a modular Python application with one normal send path. Each boundary converts data into a stable internal representation and keeps network, editorial, rendering, and delivery concerns separate.

```text
Scheduler → PowerShell wrapper (Windows only) or GitHub Actions
          → app.cli
          → PipelineRunner
          → RSS/Atom sources → normalization → deduplication
          → categorization → ranking → selection
          → optional LLM summaries / source-text fallback
          → newspaper renderer → SMTP provider
```

## Runtime path

1. **Scheduling** — `.github/workflows/daily-newsletter.yml` runs at 01:30 UTC (07:00 IST) and supports manual dispatch. The Windows task can invoke the same CLI through `scripts/run_newsletter.ps1`.
2. **CLI and configuration** — `app/cli.py` loads the local `.env` when present, parses `preview`, `send`, and `config-check`, and creates a `PipelineRunner`. Preview and send do not construct a database repository.
3. **Source adapters** — `app/sources/rss_atom.py` fetches configured endpoints through the shared source protocol in `app/sources/base.py`. A source failure is isolated so other feeds can proceed.
4. **Normalization** — `app/processing/normalize.py` validates provider fields and produces immutable `Article` values with stable canonical URL/content identity, source provenance, timestamps, and sanitized descriptions.
5. **Editorial processing** — `deduplicate.py` groups repeated coverage into `Story`; `categorize.py` applies configured deterministic signals; `rank.py` computes explainable scores; `select.py` enforces category and total limits.
6. **Enrichment** — `app/llm` sends bounded source-grounded requests when a key is configured. Validation failures, timeouts, and absent credentials fall back to source descriptions. The feed content remains the provenance for article claims.
7. **Newspaper rendering** — `app/newsletter/renderer.py` builds mobile-friendly HTML and a plain-text alternative. It derives Top Stories, Important Today, AI Watch, and Fact of the Day from selected stories. The fact excerpt is attributed and linked to its source. Market Snapshot is absent because there is no configured market-data source.
8. **Delivery** — `app/email` validates settings, creates the SMTP provider, sends both alternatives using STARTTLS, and classifies accepted, rejected, retryable, or ambiguous outcomes. Routine logs contain lifecycle counts, not secrets or article bodies.

## Why these boundaries exist

- Feed formats and network failures stay inside adapters rather than leaking into ranking or rendering.
- Deterministic processing makes ranking and selection testable without RSS or LLM services.
- The LLM interface can be replaced or disabled without changing story models or the email provider.
- Rendering accepts enriched story data and has no network or credential access.
- SMTP configuration and delivery behavior are isolated from the editorial pipeline.
- GitHub Actions and Windows Task Scheduler only decide when to invoke the CLI; neither owns newspaper logic.

## Reliability limits

The pipeline tolerates individual source and LLM failures. If all sources fail or email configuration is invalid, the run does not send. The workflow uses GitHub's hosted scheduler, which is best-effort.

There is intentionally no cloud database, persistent run history, or cross-run delivery ledger. A manual rerun with an ambiguous SMTP result can cause a duplicate. Concurrency prevents overlapping workflow executions but does not make distinct runs idempotent. Inspect the provider outcome before any retry.

## Optional legacy persistence package

`app/database` contains the earlier SQLite/PostgreSQL run-history implementations and their unit tests. The production CLI's preview/send path does not instantiate or import these repositories, and normal installs do not include the PostgreSQL driver. These modules are not part of the daily newspaper architecture and may be removed in a later cleanup after their isolated tests/utilities are retired deliberately.
