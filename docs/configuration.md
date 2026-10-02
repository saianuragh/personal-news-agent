# Runtime Configuration and Secrets

The CLI reads process environment variables first, then fills in missing values from the project-root `.env` file. Existing process values always win. The parser supports simple `KEY=value` lines and quoted values; it does not expand variables. `.env` is ignored by Git. Keep credentials out of YAML, source files, command arguments, Docker images, and logs.

## Free-first local settings

| Variable | Local default | Purpose |
|---|---|---|
| `DATABASE_BACKEND` | `sqlite` | Select `sqlite` or optional `postgres`; there is no backend fallback. |
| `DATABASE_PATH` | `data/pipeline_runs.sqlite3` | SQLite pipeline run and delivery-claim ledger. |
| `DATABASE_URL` | unset | PostgreSQL URL, used only when the optional backend is selected. |
| `NEWSLETTER_TIMEZONE` | `Asia/Kolkata` in the sample | Local newsletter date and publication-time display. |
| `APP_CONFIG_DIR` | `config` | Sources, categories, ranking, and selection YAML. |
| `PREVIEW_DIRECTORY` | `data/previews` | Generated HTML and plain-text files. |
| `PYTHON_EXE` | unset | Full Python executable path used in Task Scheduler setup documentation. |
| `LLM_API_KEY` | unset | Secret OpenRouter API key. |
| `LLM_MODEL` | `openrouter/free` in sample | Explicit free-model router; AI enrichment falls back to source text on provider failure. |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` in sample | OpenRouter-compatible API base URL. |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_ATTEMPTS` | bounded defaults | LLM timeout and retry controls. |

OpenRouter's free router is optional and can be unavailable or rate-limited. The application does not fall back to a paid model; failed AI enrichment uses the existing source-description fallback. OpenRouter currently lists limits and free-tier terms that can change, so the operator must keep the model set to `openrouter/free` and must not add credits or enable a paid route when the project must stay at ₹0. [OpenRouter free router](https://openrouter.ai/openrouter/free/apps), [OpenRouter pricing](https://openrouter.ai/pricing)

## Email settings

| Variable | Meaning |
|---|---|
| `EMAIL_PROVIDER` | `smtp` (free-first default) or `resend` (retained optional provider). |
| `SMTP_HOST`, `SMTP_PORT` | SMTP endpoint; Gmail-compatible defaults are `smtp.gmail.com` and `587` (STARTTLS). |
| `SMTP_USERNAME` | Account address used for SMTP authentication. |
| `EMAIL_FROM` | Sender address; for a personal Gmail account, use the same Gmail address. If blank, the SMTP username is used. |
| `NEWSLETTER_RECIPIENT` | One recipient. |
| `EMAIL_TIMEOUT_SECONDS`, `EMAIL_MAX_ATTEMPTS` | Bounded SMTP/API timeouts and retries. Uncertain delivery is never retried automatically. |
| `RESEND_API_KEY` | Secret used only when `EMAIL_PROVIDER=resend`; that provider is optional and may require paid infrastructure. |

Locally, SMTP passwords are stored in the Windows user's Credential Manager through `personal-news-agent email-credential-set`; they are not stored in `.env`. In cloud, inject `SMTP_PASSWORD` from the platform's secret store; the environment secret takes precedence over keyring. Install the optional local integration using the same Python 3.12 interpreter that Task Scheduler will invoke: `& $PythonExe -m pip install -e ".[windows-email]"`. For Google's account requirements and SMTP endpoint details, see [Google App Passwords](https://support.google.com/accounts/answer/185833) and [Gmail SMTP settings](https://support.google.com/mail/answer/7104828).

## Validation behavior

`personal-news-agent config-check` validates local configuration without connecting to RSS sources, SMTP, OpenRouter, Resend, or the database. It never returns secret values. Its `email_status` is `configured` only when a provider and required credential are available; otherwise it reports `not_configured_optional`. The `preview` command never sends email. Without `--with-ai`, it also does not call the LLM; with `--with-ai`, missing LLM configuration or provider failure uses the source-description fallback. A `send` run with no email credential generates and persists the newsletter locally, reports a preview delivery outcome, and does not attempt email.

Run `personal-news-agent database-init` to create the selected schema safely and repeatedly. SQLite is used locally; the cloud production workflow explicitly selects PostgreSQL and fails rather than falling back if its URL or database is unavailable. See [cloud deployment](cloud-deployment.md).
