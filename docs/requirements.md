# Personal News Intelligence Agent — Requirements and V1 Scope

This document distinguishes implemented behavior from product goals that remain deferred. The code and configuration are authoritative for current behavior.

## Purpose

The application produces a personal news briefing from configured feeds. It normalizes feed entries, removes exact-signal duplicates, categorizes/ranks/selects stories, optionally enriches selected stories with an LLM, renders HTML and text, persists run/delivery metadata, and can optionally send email. The production workflow is GitHub Actions + Neon PostgreSQL; the local Python + SQLite + Windows Task Scheduler setup remains a fallback.

## Implemented V1

- Python CLI with `config-check`, `database-init`, `runs`, `preview`, and explicit `send` modes.
- Generic RSS/Atom source adapter; the checked-in configuration enables BBC News World and The Guardian World.
- Adapter output is `RawProviderEntry`; normalization validates untrusted provider data and creates canonical `Article` records.
- URL and content-hash exact-signal deduplication into in-memory `Story` values. Fuzzy/semantic title matching is not implemented.
- The content hash includes source ID and publisher, so reports from different publishers generally do not hash-match; cross-publisher merging requires an exact/normalized canonical URL match. Similar titles alone are not enough.
- Deterministic keyword/source-tag categorization. Unmatched stories remain unclassified and are excluded from category selection.
- Deterministic configurable ranking with freshness, source quality, category relevance, and corroboration signals. Freshness affects score; it is not an article-age exclusion filter.
- Category-aware selection with `max_total_stories: 24` and `max_per_category: 5` in current configuration.
- Optional structured LLM enrichment for selected stories, bounded provider attempts, response validation, and source-description fallback or omission.
- Escaped HTML and plain-text newsletter rendering to local preview files.
- SQLite run history and delivery-claim persistence. Articles, stories, model outputs, and newsletter bodies are not stored in the database.
- Optional email adapters. Preview never sends; absent email configuration does not prevent local generation.
- Structured, sanitized lifecycle logging; Windows PowerShell wrapper and Task Scheduler instructions.

## Explicit deferrals and limits

- Only RSS/Atom is implemented; no news APIs, full-text scraping, or source discovery.
- Two feeds are currently enabled. Coverage remains limited to their published entries and the feeds can fail independently.
- No near-duplicate/fuzzy matching, article lookback filter, retention cleanup of database rows, or article/story persistence.
- No LLM categorization. Categories come from configured tags and keyword rules.
- No guarantee that all eight categories have stories; unclassified or ineligible items can be omitted.
- LLM output can be unavailable or incorrect. It is not a fact-checker; source links remain the evidence.
- Exactly-once external email delivery cannot be guaranteed after ambiguous provider acceptance. The local claim ledger guards repeat attempts.
- A GitHub Actions production workflow is defined for daily 07:00 Asia/Kolkata delivery using Neon PostgreSQL, but it is not deployed/active until committed to a GitHub default branch and platform secrets are configured.
- Windows Task Scheduler remains registered as a local fallback for daily 07:00 India Standard Time SMTP delivery. The task uses the interactive user principal so no Windows password is stored; the account must be signed in. Its SQLite ledger is separate from cloud PostgreSQL.

## Current category set

The category configuration includes India, World, AI, Technology, Business & Economy, Science & Space, Sports, and Entertainment. Categories and keywords are data in `config/categories.yaml`.

## Architecture and operating assumptions

This is a modular monolith. The scheduled path is:

```text
Task Scheduler → PowerShell wrapper → CLI/config → PipelineRunner
→ source adapters → RawProviderEntry → normalization/validation
→ Article → deduplication/Story → categorization → ranking → selection
→ optional LLM enrichment/fallback → newsletter rendering
→ preview or optional delivery → run metadata persistence
```

The feed endpoint and source metadata are configured in `config/sources.yaml`; ranking and selection policy live in their YAML files. Runtime secrets are supplied through the environment or ignored local `.env`. SQLite is the default; selecting PostgreSQL is explicit and does not silently fall back.

## Quality requirements

- Keep provider-specific feed data behind the adapter/normalizer boundary.
- Reject unusable titles and unsafe URLs, including URLs with embedded credentials; isolate malformed entries.
- Treat feed and model text as untrusted and escape it in HTML.
- Keep source fetching, LLM calls, and persistence bounded and failures observable.
- Never expose secrets in tracked files, logs, or generated newsletters.
- Preserve source links and available publication times in rendered output.
- Keep preview safe: it must not contact an email provider.
- Maintain deterministic processing decisions for a fixed input, configuration, and `as_of` time.

## Deferred product goals

These are possible future decisions, not current behavior: configurable publication lookback and retention policies; conservative near-duplicate detection; persistent article/story history; additional reviewed feeds/APIs; LLM-assisted categorization; broader coverage evaluation; and any separately funded cloud deployment. Implement only after requirements and tests are agreed. Daily scheduling, recipient policy, provider selection, and source coverage should be validated through local previews before unattended operation.
