# Roadmap

## Current version

The project is a scheduled personal newspaper built on RSS/Atom, deterministic processing, optional LLM enrichment, HTML/plain-text rendering, and SMTP delivery. GitHub Actions is configured for 07:00 IST; Windows Task Scheduler remains an optional local fallback.

## Near-term improvements

1. Monitor feed availability and remove endpoints that become unreliable.
2. Improve India and AI coverage as reliable free feeds become available.
3. Add a market-data source only if it can provide verifiable values without inventing or scraping unsupported data.
4. Consider local preview retention and operational status summaries without adding a cloud database dependency.
5. Add a durable idempotency service only if delivery volume or duplicate risk justifies the operational complexity.

## Explicitly out of scope

The daily newspaper does not require Neon, PostgreSQL, SQLite, Docker, paid news APIs, a dashboard, or cloud run-history storage.
