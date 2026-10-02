# Roadmap — Free-First Cloud Production

The target is a free-first remote daily run, with Windows retained for local fallback. The implemented pipeline, SQLite and PostgreSQL run/delivery metadata, provider-neutral boundaries, preview and SMTP send modes, structured logs, tests, Ruff, and CLI are present.

## Remaining work

1. **Cloud workflow configuration — implemented in repository** — GitHub Actions runs on hosted Ubuntu at 01:30 UTC, selects PostgreSQL explicitly, and supports SMTP credentials from environment secrets. It still requires a GitHub default-branch push, Neon database, and platform secrets before activation.
2. **Windows Task Scheduler integration — implemented and registered** — the wrapper defaults to `send`; the reproducible registration script verifies configuration, registers the enabled daily 07:00 IST task, and verifies its action and trigger. The task runs as the signed-in user without a stored account password.
3. **Safe email delivery — implemented** — SMTP delivery and secure local credential setup are configured. The per-recipient/date delivery claim continues to prevent a second same-day send.
4. **Final automated run — verified** — the wrapper ran in `send` mode and the exact registered task was manually triggered. Both returned `duplicate_send_skipped` due to the existing same-day claim; no additional email was sent.
5. **Documentation — updated** — the cloud runbook documents setup, manual send, logs, idempotency, cost limits, and laptop-off behavior. The Windows runbook documents the fallback and its sign-in/PC requirements.

## Pending activation

Create the Neon project, push the repository to a private GitHub default branch, add the required GitHub Actions secrets, run the manual production workflow once, and disable the local Task Scheduler schedule when cloud delivery is verified. GitHub Actions and Neon free-tier quotas apply. Cloud Run/Cloud SQL in `deployment.md` remains an optional alternate that may incur charges.


