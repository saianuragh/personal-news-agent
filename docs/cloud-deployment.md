# Cloud production deployment

This document describes the production scheduler. The Windows setup in [free local deployment](free-local-deployment.md) remains available as a local fallback and is not the production scheduler.

## Architecture and schedule

GitHub Actions starts an Ubuntu-hosted runner; it checks out the default branch, installs this Python package, validates non-interactive configuration, initializes the existing PostgreSQL schema, and runs `personal-news-agent send`. The runner fetches feeds, optionally calls the configured OpenAI-compatible LLM, sends through SMTP, and records run/delivery state in Neon PostgreSQL. The workflow does not use this laptop, its `.venv`, `.env`, PowerShell, Task Scheduler, filesystem, or Windows Credential Manager.

The schedule in `.github/workflows/daily-newsletter.yml` is `30 1 * * *` UTC, which is **07:00 Asia/Kolkata** year-round. GitHub Actions can delay or drop a scheduled run during high load. Scheduling at minute 30 avoids the documented high-load top-of-hour window, but GitHub Actions is not a clock-time SLA. Schedule events run the workflow version on the repository's default branch; keep the workflow committed there.

| Dependency | Service | Role |
|---|---|---|
| Cloud scheduler and Python runtime | GitHub Actions | Scheduled and manual job execution with hosted Ubuntu internet access. |
| Persistent database | Neon PostgreSQL | Durable run history and atomic recipient/date delivery claims. |
| Email | Existing SMTP adapter, configured for Gmail by default | Newsletter delivery using a platform secret for the app password. |
| Optional AI enrichment | Existing OpenAI-compatible LLM adapter (OpenRouter by default) | Enriches selected stories when its key is configured; existing fallback remains active on provider failure. |

The runner filesystem is temporary. Production explicitly sets `DATABASE_BACKEND=postgres`, and `DATABASE_URL` is required; the application does not fall back to SQLite. `database-init` uses repeatable `CREATE TABLE IF NOT EXISTS` statements for the current schema. This is not a general migration system: future schema changes need reviewed migrations.

## Required accounts and setup

You need:

1. A **private GitHub repository** containing this project, with GitHub Actions enabled. This checkout currently has no Git remote or GitHub authentication, so the workflow cannot be pushed/deployed from this workspace.
2. A **Neon account and PostgreSQL project**. Create a production database and use its TLS connection URL (pooled URL is suitable for this short-lived job). Keep the project within the free plan's limits and monitor its usage.
3. A **Gmail account** with two-step verification and an app password allowed. Use the Gmail account as both `SMTP_USERNAME` and `EMAIL_FROM`; if using another SMTP provider, configure its host/port and sender policy by reviewing the workflow first.
4. An **OpenRouter account/key only if AI enrichment is wanted**. AI remains optional; without `LLM_API_KEY`, the existing source-text fallback is used.

Push this workflow and application code to the repository's default branch. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Required? | Value |
|---|---|---|
| `NEWS_AGENT_DATABASE_URL` | Yes | Neon PostgreSQL connection URL including its database/user/password and TLS settings. |
| `NEWSLETTER_RECIPIENT` | Yes | The one address that receives this personal newsletter. |
| `SMTP_USERNAME` | Yes | SMTP account address. |
| `SMTP_PASSWORD` | Yes | Gmail app password or SMTP password. |
| `EMAIL_FROM` | Yes | Sender address accepted by the SMTP account (same as `SMTP_USERNAME` for Gmail). |
| `LLM_API_KEY` | Optional | OpenRouter API key. Leave absent to use existing non-AI fallback behavior. |

Do not paste secret values into workflow YAML, repository files, issue comments, logs, or this document. The workflow passes secrets only as process environment variables. `SMTP_PASSWORD` takes precedence over the local OS keyring, so the same SMTP adapter works on the runner while local Credential Manager continues to work on Windows.

The non-secret production settings are in the workflow: `DATABASE_BACKEND=postgres`, `NEWSLETTER_TIMEZONE=Asia/Kolkata`, Gmail SMTP on port 587/STARTTLS, and `LLM_MODEL=openrouter/free` at `https://openrouter.ai/api/v1`. Change them only through a reviewed workflow change. The `workflow_dispatch` job is restricted to the default branch so production secrets are not passed to branch code.

## Initialize, manually run, and inspect

After pushing the workflow to the default branch and adding the required secrets:

1. Open the repository's **Actions** tab and select **Daily Newsletter**.
2. Select **Run workflow**, choose the default branch, and confirm. This invokes the same `config-check → database-init → send` path as the scheduled job.
3. The manual action is a **real send**, not a preview. It can email the configured recipient and creates the normal local-briefing-date delivery claim. If it succeeds for today's date, the 07:00 scheduled invocation on that same date will report `duplicate_send_skipped`.
4. Inspect the Actions run steps and JSON output for `status`, `delivery.status`, source counts, and run ID. Successful provider acceptance is recorded in Neon. You can query the `pipeline_runs` and `newsletter_deliveries` tables using Neon SQL Editor; do not paste database credentials into queries or logs.
5. Confirm the email in the recipient inbox and spam folder. Provider acceptance means the SMTP server accepted the message; mailbox delivery still depends on downstream mail handling.

GitHub Actions retains workflow logs and execution status under the repository's Actions tab. The CLI and provider adapters redact credential values and use safe summaries; logs contain operational data, so restrict repository access. The job timeout is 15 minutes. One unavailable feed follows the existing partial-source-failure behavior; database, configuration, or all-source failures return nonzero and fail the Actions run.

## Idempotency and retries

The PostgreSQL delivery ledger has a unique delivery key derived from recipient plus the local briefing date. A claim is inserted atomically before sending. If two invocations overlap or GitHub retries/dispatches the same date, only the first claimant can send; later invocations report `duplicate_send_skipped`. The workflow concurrency group additionally serializes its own runs, while the database claim remains the authoritative cross-run guard.

An ambiguous SMTP network failure is not automatically retried by the adapter because the provider may already have accepted the message. The durable claim remains, preventing a second send. Check the Actions run, database record, and mail provider/inbox before any operator recovery. Do not run `delivery-reset` in the cloud workflow or delete claims to force retries casually.

## Laptop-off behavior

Once the workflow is committed on the default branch and the hosted services/secrets are configured:

- Laptop powered off: **YES, the cloud job can run**.
- Laptop disconnected from the internet: **YES**.
- Windows user logged out or Task Scheduler unavailable: **YES**.
- Local `.venv`, PowerShell wrapper, and Windows Credential Manager: **not used by the cloud job**.
- Cloud runner without internet: **NO**; feeds, optional LLM calls, SMTP, and database access need network connectivity.

The local Task Scheduler setup may be retained as a manual fallback. Before enabling both schedules, disable the local scheduled task to avoid parallel runs; its local SQLite delivery ledger is separate from Neon and therefore cannot coordinate with cloud claims. Re-enable it only as an intentional fallback when cloud scheduling is paused.

## Cost and service limits

This is a free-first option, not a promise of permanent zero cost or exact-minute execution. GitHub-hosted runner usage is subject to the repository owner's included Actions minutes and billing policy. Neon currently documents a Free plan with bounded per-project compute and storage; its quotas and terms can change. A low-volume daily newsletter should be small, but monitor consumption and keep the database compact. The LLM/email providers have their own limits and may change availability or pricing. GitHub documents that scheduled runs can be delayed or dropped under high load. If a delivery time SLA, guaranteed run, or stronger operational alerting becomes necessary, select a paid scheduler/runtime/database and add monitoring.

## Troubleshooting

| Symptom | Check |
|---|---|
| Workflow does not appear or schedule never starts | Confirm the workflow exists on the default branch, Actions are enabled, and the repository has not had its schedule disabled. Check the Actions tab. |
| `config-check` fails | Verify all required GitHub secret names and sender/recipient values. `LLM_API_KEY` is optional. No secret value is printed by the check. |
| PostgreSQL connection/initialization fails | Check the Neon project is active, `DATABASE_URL` is the correct TLS-enabled URL, and the project is within plan limits. Confirm `DATABASE_BACKEND=postgres`; never switch cloud to SQLite. |
| SMTP authentication rejected | Regenerate/verify the app password and two-step verification, then update only the GitHub secret. Confirm `SMTP_USERNAME` and `EMAIL_FROM` are the Gmail account. |
| LLM enrichment absent | Check whether `LLM_API_KEY` is configured and whether OpenRouter/model limits permit requests. The pipeline may fall back without failing delivery. |
| Run says `duplicate_send_skipped` | A claim already exists for this recipient and local date. Check prior Actions runs and `newsletter_deliveries`; this protects against duplicate email. |
| Run was late | GitHub Actions scheduled execution is best-effort. The cron targets 01:30 UTC (07:00 IST), but the platform does not guarantee exact-minute start. |

## Cloud service documentation

- [GitHub scheduled workflow events and limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [GitHub manually running a workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Neon Free plan resource limits](https://neon.com/blog/neon-backend-is-ga)
