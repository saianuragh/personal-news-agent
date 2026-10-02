# Configuration

Runtime settings come from the process environment and, for local runs, the ignored project `.env`. The loader does not overwrite variables already provided by the process. `.env.example` contains names and safe defaults only.

## Sources and editorial policy

`config/sources.yaml` lists enabled free RSS/Atom feeds. Each source has a stable ID, display name, endpoint, categories, quality weight, timeout, and language. Sources fail independently; an unavailable feed is recorded as a source failure while the remaining feeds continue. The project uses only RSS/Atom and does not require a paid news API.

`config/categories.yaml` contains deterministic source IDs, feed tags, and token/phrase signals for India, World, AI, Technology, Business & Economy, Science & Space, Sports, and Entertainment. A story can match more than one category. Unknown stories remain ineligible for the edition instead of being assigned an arbitrary section.

`config/ranking.yaml` controls source quality, freshness, category evidence, and corroboration scoring. `config/selection.yaml` controls section balance, with a 24-story overall limit and at most five stories per category.

## Email

| Variable | Meaning | Default |
|---|---|---|
| `EMAIL_PROVIDER` | Delivery provider | `smtp` |
| `SMTP_HOST` | SMTP server | `smtp.gmail.com` |
| `SMTP_PORT` | STARTTLS port | `587` |
| `SMTP_USERNAME` | Sender account | Required for send |
| `EMAIL_FROM` | Sender address | Defaults to username |
| `NEWSLETTER_RECIPIENT` | Destination | Required for send |
| `SMTP_PASSWORD` | SMTP app password in hosted workflow | Required there |
| `EMAIL_TIMEOUT_SECONDS` | Connection timeout | `20` |
| `EMAIL_MAX_ATTEMPTS` | Bounded provider attempts | `2` |

On Windows, SMTP credentials are stored in Credential Manager through `personal-news-agent email-credential-set`, rather than saved in `.env`. In GitHub Actions, the password is supplied as a repository secret. Gmail SMTP requires `EMAIL_FROM` to match `SMTP_USERNAME`.

## Optional LLM enrichment

| Variable | Meaning | Default |
|---|---|---|
| `LLM_API_KEY` | Provider key | Unset disables enrichment |
| `LLM_MODEL` | OpenAI-compatible model name | `openrouter/free` in hosted workflow |
| `LLM_BASE_URL` | Provider API root | Set by deployment configuration |
| `LLM_TIMEOUT_SECONDS` | Request timeout | `20` |
| `LLM_MAX_ATTEMPTS` | Bounded retries | `2` |

When the key is absent or the provider fails, source descriptions provide fallback summaries. No LLM facts are added to the Fact of the Day; that section quotes a short source-provided excerpt with its link.

## Time and output

`NEWSLETTER_TIMEZONE` is an IANA timezone and defaults to UTC. Production and local scheduling use `Asia/Kolkata`. `APP_CONFIG_DIR` and `PREVIEW_DIRECTORY` can override the configuration and preview paths. `PYTHON_EXE` is used by the Windows scheduler wrapper.

The scheduled newspaper does not require SQLite, PostgreSQL, Neon, or Docker. Preview artifacts are local files and are not uploaded by GitHub Actions.
