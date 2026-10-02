# GitHub Actions daily newspaper

GitHub Actions checks out the default branch, installs the package, validates non-interactive email/source configuration, and runs `personal-news-agent send` on a hosted Ubuntu runner. The daily schedule is `30 1 * * *` UTC, or **07:00 Asia/Kolkata**. `workflow_dispatch` invokes the same real-send path.

The workflow does not use the laptop, local `.env`, Windows Credential Manager, Docker, SQLite, or a database service. Run history and delivery claims are not persisted. Workflow concurrency prevents overlapping jobs, but it is not cross-run delivery idempotency. If an email result is ambiguous, inspect the provider result and do not rerun blindly.

## Repository secrets

Create these repository Actions secrets under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
|---|---|
| `NEWSLETTER_RECIPIENT` | Newsletter destination |
| `SMTP_USERNAME` | Gmail SMTP account |
| `SMTP_PASSWORD` | Gmail app password |
| `EMAIL_FROM` | Sender address; for Gmail SMTP it must match the username |
| `LLM_API_KEY` | Optional summary enrichment |

No database secret is used. Secret values are never read back from GitHub by this application.

## Workflow behavior

1. Check out the default branch and install the Python package.
2. Run `personal-news-agent config-check`; this validates sources, timezone, and available email settings without sending.
3. Run `personal-news-agent send`; it fetches RSS/Atom feeds, processes and ranks stories, uses the LLM when configured, falls back to source descriptions if enrichment fails, renders HTML/plain text, and sends through SMTP.

The feed pipeline tolerates individual source failures and records sanitized failure counts in the run output. If every enabled feed fails or configuration is invalid, no email is sent. A model outage does not block source-backed newsletter generation.

## Safe rollout

1. Verify the four required email secret names and optionally `LLM_API_KEY` without revealing values.
2. Confirm CI passed on the default branch.
3. Use one manual workflow dispatch only when you intend to send a real email.
4. Inspect that run and confirm the provider accepted the email. Do not rerun an ambiguous result.
5. Disable, but do not delete, the Windows fallback task after cloud delivery is verified.

GitHub scheduled events are best-effort and can start later than their scheduled minute. Keep the local task enabled until the first cloud send is verified, and then ensure only one production scheduler remains enabled.
