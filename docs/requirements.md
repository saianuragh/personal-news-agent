# Requirements and current behavior

## Implemented

- RSS/Atom ingestion from editable, free feed endpoints.
- Per-source timeouts and failure isolation.
- Article validation, URL canonicalization, conservative deduplication, deterministic eight-section categorization, explainable ranking, and balanced selection (24 overall, five per category).
- Optional source-grounded LLM enrichment with strict response validation and source-description fallback.
- Editorial HTML and plain-text output, including Top Stories, Important Today, AI Watch, and an attributed source excerpt for Fact of the Day.
- Optional SMTP email delivery with bounded provider attempts and safe operational output.
- Local preview generation without email or LLM requirements.
- GitHub Actions daily workflow at 07:00 IST and a Windows scheduled fallback.
- Pytest and Ruff CI.

## Deliberate limits

- Only feeds configured in `config/sources.yaml` are used. Coverage and populated categories depend on current feed content.
- There is no paid news API, market-data provider, or market snapshot. The section is omitted rather than fabricated.
- There is no database requirement in the daily workflow. Run history and delivery claims are not persisted by production.
- SMTP does not provide guaranteed cross-run idempotency. An ambiguous email result must be reconciled before rerunning.
- LLM output is not independent verification. Source links remain the evidence trail.
- GitHub Actions schedule timing is best-effort.

## Runtime requirements

Python 3.12+, network access to configured feeds and (for send) SMTP, and the four email settings/secrets. LLM settings are optional. Docker and a database service are not required.
