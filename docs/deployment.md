# Optional Future Paid Cloud Deployment Architecture

> **Status:** This is retained as an optional paid alternative. The selected production path is GitHub Actions + Neon PostgreSQL, described in [`cloud-deployment.md`](cloud-deployment.md). Do not create Cloud Run, Cloud Scheduler, Cloud SQL, or Artifact Registry resources unless intentionally choosing and funding this alternative.

## Optional cloud design

If a future budget explicitly permits managed cloud infrastructure, one possible architecture is **Google Cloud Run Jobs + Cloud Scheduler**, with **Secret Manager**, **Artifact Registry**, **Cloud Logging**, and **Cloud SQL for PostgreSQL**.

The application retains a PostgreSQL run repository, selected with `DATABASE_BACKEND=postgres` and `DATABASE_URL`, for this alternative. The current cloud workflow also uses PostgreSQL through Neon. SQLite inside a replaceable Cloud Run container is not durable unless persistent storage is mounted; do not treat the writable container layer or an object-store-mounted SQLite file as a production database. A Cloud Run job would need Cloud SQL PostgreSQL or another durable database before unattended production operation.

Cloud Run Jobs match a finite process that starts, executes one CLI command, and exits. Jobs can be run once or on a schedule, and support task timeouts/retries and execution logs. Cloud Scheduler can invoke the job through an authenticated Cloud Run Jobs API request. [Cloud Run Jobs](https://cloud.google.com/run/docs/create-jobs), [schedule a Cloud Run job](https://docs.cloud.google.com/run/docs/triggering/using-scheduler)

## Architecture diagram

```text
                         ┌──────────────────────┐
                         │ Cloud Scheduler      │
                         │ daily, Asia/Kolkata │
                         └──────────┬───────────┘
                                    │ authenticated jobs.run request
                                    ▼
┌───────────────────┐     ┌────────────────────────────┐
│ Secret Manager    ├────►│ Cloud Run Job              │
│ LLM/email secrets │     │ personal-news-agent send   │
└───────────────────┘     │ one task, then exit        │
                          └───────┬─────────────┬──────┘
                                  │             │
                     SQL over     │             │ stdout/stderr
                     TLS/connector│             ▼
                                  ▼       ┌─────────────────┐
                          ┌────────────┐  │ Cloud Logging   │
                          │ Cloud SQL  │  │ job/run records │
                          │ PostgreSQL│  └─────────────────┘
                          └────────────┘
                                  ▲
                                  │ image pull
                          ┌───────┴────────────┐
                          │ Artifact Registry  │
                          │ versioned image    │
                          └────────────────────┘

Cloud Run Job ──HTTPS──► RSS sources / OpenRouter / transactional email API
```

## Components and responsibilities

| Component | Responsibility |
|---|---|
| Cloud Scheduler | Keeps the daily calendar and time zone; authenticates and requests one job execution. It does not run application code. |
| Cloud Run Job | Starts the existing container with `personal-news-agent send`, executes one pipeline run, returns a success/failure process exit code, and stops. Configure one task and parallelism one. |
| Artifact Registry | Stores immutable, versioned container images. Deploy a digest or immutable tag, not `latest`. |
| Secret Manager | Stores LLM, email, and any future source API credentials. Runtime service identity receives access only to required secrets. Cloud Run supports Secret Manager references for job settings. [Configure job secrets](https://docs.cloud.google.com/run/docs/configuring/jobs/secrets) |
| Cloud SQL for PostgreSQL | Durable production pipeline run history. Keep it in the same region as the job. Current persistence stores run state only; broader article/delivery persistence remains outside the current implementation. |
| Cloud Logging | Collects container stdout/stderr and Cloud Run execution records. The application should emit concise structured logs with `run_id`, mode, status, counts, and sanitized stage diagnostics. Job execution logs are available in Cloud Logging. [Execute Cloud Run jobs](https://docs.cloud.google.com/run/docs/execute/jobs) |
| Service accounts and IAM | Scheduler's identity may invoke only the target job. The job identity may read only the named secrets and connect to the database. No user credentials or downloaded service-account key files belong in the image. |

## Execution flow

1. Cloud Scheduler evaluates its daily schedule in `Asia/Kolkata` and calls the Cloud Run Jobs `jobs.run` API with an authenticated service account.
2. Cloud Run pulls the selected image digest from Artifact Registry, injects non-secret environment settings and Secret Manager values, and starts one container task.
3. The job's container entry point runs `personal-news-agent send`. The existing runner fetches sources, normalizes, deduplicates, categorizes, ranks/selects, summarizes selected stories, renders the newsletter, persists run state, and calls the transactional email adapter.
4. The process exits with its CLI result code. Cloud Run records the execution outcome; application logs go to Cloud Logging.
5. A failed execution is visible in Cloud Run and its logs. A human inspects run/delivery state before manually rerunning an ambiguous delivery.

The default image command is `preview`, which is intentionally safe. The deployed job must explicitly override the command arguments with `send`; validate this in a one-off preview job before enabling the schedule.

## Scheduler and container/job responsibilities

Use a single Scheduler job, for example `0 7 * * *` with the `Asia/Kolkata` time zone, targeting the Cloud Run Jobs execution endpoint. Set the job to one task and parallelism one. The scheduler owns *when* execution is requested; the container owns *what one run does*. No scheduler loop or cloud SDK belongs in the Python application.

Cloud Scheduler may retry failed target invocations depending on its retry configuration, and a request retry could result in another job execution. Cloud Run task retries can also repeat the entire command. Initially set task retries to zero and use a conservative scheduler retry policy; inspect delivery outcome before manually retrying any ambiguous send. Before enabling automatic retries, the production database must durably enforce one daily delivery/idempotency key across executions. The email provider's idempotency behavior alone does not make SQLite run history durable or guarantee exactly-once delivery.

## Secret management

Store each credential as a distinct Secret Manager secret and inject it as an environment variable only at runtime. Expected secret values include:

- `LLM_API_KEY` for OpenRouter.
- `RESEND_API_KEY` and `EMAIL_FROM` for the transactional email adapter (the sender address is configuration, not a secret, but should still be managed with the job's configuration).
- `NEWS_PROVIDER_API_KEY` only if a future enabled source needs it.

Use a dedicated runtime service account and grant it access only to the specific secret versions and database. Grant Scheduler's service account job invocation only. Never pass secret values as scheduler payload, image build arguments, Docker `ENV`, source files, or log fields. Pin secret versions for controlled rollouts or explicitly document use of a moving `latest` alias and its rotation behavior. Cloud Run recommends Secret Manager for sensitive job settings. [Cloud Run job secrets](https://docs.cloud.google.com/run/docs/configuring/jobs/secrets)

## Database and persistence strategy

### Application state and cloud-only persistence guidance

`DATABASE_BACKEND` defaults to `sqlite`. In local development, `DATABASE_PATH` selects the SQLite file and defaults to `data/pipeline_runs.sqlite3`. For PostgreSQL, set `DATABASE_BACKEND=postgres` and a valid `DATABASE_URL`. The application will not silently fall back from PostgreSQL to SQLite. Cloud Run's writable container file system is ephemeral; replacement loses a SQLite file and `PREVIEW_DIRECTORY` output. Persistent storage must be mounted for SQLite durability. Cloud Run documents the container filesystem as disposable and recommends external storage for persistent files. [Cloud Run overview](https://docs.cloud.google.com/run/docs/overview/what-is-cloud-run)

For this optional replaceable-container architecture, SQLite would not be durable without a persistent filesystem. The selected production workflow uses Neon PostgreSQL; the local Windows fallback uses SQLite on the user's own machine. If the application is moved to Cloud Run, do not put SQLite on Cloud Storage or assume object storage provides SQLite locking/transaction semantics; use a durable database such as Cloud SQL PostgreSQL.

### PostgreSQL schema initialization and operations

PostgreSQL schema creation is explicit and repeatable. After provisioning the database and injecting `DATABASE_BACKEND=postgres` and `DATABASE_URL`, run `personal-news-agent database-init` once with the deployment identity before pipeline executions. It executes only `CREATE TABLE IF NOT EXISTS`; it does not migrate, delete, alter, or rewrite existing data. Review any future schema evolution as a separately controlled migration. If the expected schema is missing or incompatible, the pipeline must fail before processing rather than creating unreviewed schema or switching to SQLite.

Cloud SQL for PostgreSQL compatibility uses the standard PostgreSQL protocol and the Psycopg 3 driver. The deployment must provide a Cloud SQL connection path (for example, Cloud Run's attached Cloud SQL Unix socket, a Cloud SQL connector/proxy, or supported private-IP connectivity), TLS/authentication, a narrowly scoped database identity, and an appropriate connection timeout. `DATABASE_URL` supports a standard PostgreSQL host URL or a URL with a `host=/cloudsql/...` query parameter for a Unix socket; its credentials must be URL-encoded and the whole URL treated as a secret. Current execution opens short-lived connections rather than a pool, which is appropriate for one job invocation; reassess only if connection patterns change.

The current table records pipeline run lifecycle/counts, sanitized warnings/errors, and delivery outcome. It does not persist articles, full newsletter artifacts, or a separate durable email-delivery ledger. Do not claim those records are present in Cloud SQL.

## Logging strategy

Write operational logs to stdout/stderr; Cloud Run forwards job logs to Cloud Logging. Emit one start and one completion/failure record with `run_id`, mode, timestamps, status, source/pipeline counts, delivery status, and sanitized error category. Keep application stage diagnostics concise and secret-scrubbed. Never log authorization headers, API keys, raw request/response bodies, or full source descriptions. Do not log full newsletter content. Set a practical retention policy for logs and use execution history as the first diagnostic path. This design specifies logs only; it does not add monitoring or alerting.

## Failure and retry behavior

- Source, LLM, and email adapters retain their existing bounded internal retry policies.
- A permanent application/configuration/database error exits nonzero; Cloud Run marks the execution failed.
- One source failure continues through the existing partial-run behavior. All-source failure remains a failed run.
- Database connection or persistence failure stops before email delivery, matching the approved architecture.
- Set Cloud Run task retry count to zero initially. A whole-task retry repeats all pipeline stages and could repeat a send.
- Keep scheduler retries limited. A retry of the `jobs.run` request can create an additional run; do not assume invocation is exactly once.
- On an ambiguous email timeout, inspect persisted delivery state and provider status before rerunning. Do not automatically replay an unknown outcome until durable daily idempotency/reconciliation is implemented.
- Use the CLI's nonzero exit for failed runs and Cloud Run's execution status/logs for manual diagnosis. No new alerting service is part of this design.

## Time zone and daylight saving

Set the Scheduler time zone to the intended user time zone and set `NEWSLETTER_TIMEZONE` to the same IANA value so schedule, subject, and rendered publication times agree. For the current user's `Asia/Kolkata`, a `07:00` schedule has no seasonal daylight-saving transition. Cloud Scheduler supports IANA/tz database time zones but warns that wall-clock scheduling can behave unexpectedly for repeated or skipped DST times; UTC avoids DST ambiguity. [Cloud Scheduler time zones](https://docs.cloud.google.com/scheduler/docs/configuring/cron-job-schedules)

If the owner later chooses a DST-observing zone, explicitly choose between a fixed UTC instant and a fixed local wall-clock time, and test the spring-forward and fall-back dates. Store run timestamps in UTC as the application already does.

## Expected production environment

Non-secret values configured on the Cloud Run Job:

| Variable | Production value/purpose |
|---|---|
| `APP_CONFIG_DIR` | `/app/config`, unless source configuration is deliberately externalized later. |
| `NEWSLETTER_TIMEZONE` | `Asia/Kolkata` (IANA zone, aligned with Scheduler). |
| `NEWSLETTER_RECIPIENT` | The single intended recipient address. |
| `LLM_MODEL` | The reviewed OpenRouter model/routing value, currently `openrouter/free`. |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1`. |
| `LLM_TIMEOUT_SECONDS` | Bounded provider timeout from the reviewed production setting. |
| `LLM_MAX_ATTEMPTS` | Bounded attempt count. |
| `EMAIL_FROM` | Verified transactional sender address. |
| `EMAIL_TIMEOUT_SECONDS` | Bounded email-provider timeout. |
| `EMAIL_MAX_ATTEMPTS` | Bounded attempt count. |
| `DATABASE_BACKEND` | `postgres` for the production job; omit or set `sqlite` for local development. |
| `DATABASE_URL` | PostgreSQL connection URL, injected as a secret or assembled from secret components; required only with the PostgreSQL backend. |

Run `personal-news-agent config-check` before enabling the schedule to validate required send settings without contacting PostgreSQL, OpenRouter, or Resend. See [`configuration.md`](configuration.md) for the complete environment contract and local-safe behavior.

Secret Manager values injected at runtime:

| Variable | Secret |
|---|---|
| `LLM_API_KEY` | OpenRouter credential. |
| `RESEND_API_KEY` | Email provider credential. |
| `NEWS_PROVIDER_API_KEY` | Only if a future configured source requires one. |

`DATABASE_PATH` and `PREVIEW_DIRECTORY` are local/container-test paths when SQLite is selected. Do not mistake `/data` in Cloud Run's writable layer for a persistent volume. Preview output should remain local-only; for cloud preview, inspect execution counts/logs or design a separate artifact destination only if needed. Do not store a newsletter copy in logs.

## What remains local-only

- SQLite file at `data/pipeline_runs.sqlite3` and local preview artifacts under `data/previews/`; SQLite is not the production backend.
- Developer `.env` files and local exported environment values.
- `personal-news-agent preview` and `preview --with-ai` manual review workflows.
- SQLite files and preview artifacts remain local/container-test outputs; PostgreSQL run history is implemented and selected explicitly for production preparation.
- Docker image build/run on a developer workstation. The cloud design does not assume Docker tooling exists on the runtime host.

## Brief alternatives evaluation

| Option | Fit and trade-offs |
|---|---|
| Google Cloud Run Jobs + Cloud Scheduler (optional; not selected for ₹0) | Direct fit for a finite CLI process. Scheduler can trigger a job in an IANA time zone; Cloud Run handles job execution and logs; Secret Manager integrates with job environment settings. Cloud SQL is a recurring-cost component and requires billing-enabled infrastructure. |
| **AWS EventBridge Scheduler + ECS Fargate task** | Equally valid one-shot task model with EventBridge schedules, Fargate, Secrets Manager, IAM, and CloudWatch. More AWS-specific resources and task/role wiring for this single-user workload; good choice if the project were already on AWS. EventBridge Scheduler supports named time zones. [Schedule ECS tasks](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/tasks-scheduled-eventbridge-scheduler.html), [Scheduler time zones](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html) |
| **Azure Container Apps scheduled Job** | A close managed-job equivalent with cron schedules, Key Vault references, managed identity, and Log Analytics. Scheduled cron is evaluated in UTC, so local-time DST semantics require explicit UTC conversion or schedule changes. It is a sound option for an Azure-oriented portfolio, but adds no benefit over the selected GCP path here. [Container Apps Jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs), [Key Vault secret references](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets) |

GitHub Actions scheduled workflows can be an inexpensive experiment but are not selected as production scheduling: this workload should have cloud-native job history, service identity, secret access, and durable database integration independent of a repository workflow.

## Deployment prerequisites

Before a first production send:

1. Create a Google Cloud project with billing and choose one region for Cloud Run, Artifact Registry, Cloud Scheduler, and Cloud SQL.
2. Enable the required Cloud Run, Cloud Scheduler, Secret Manager, Artifact Registry, and Cloud SQL APIs.
3. Provision Cloud SQL for PostgreSQL; set `DATABASE_BACKEND=postgres` and `DATABASE_URL`, then run the explicit `personal-news-agent database-init` command and verify backup/restore.
4. Build and publish the existing Docker image to Artifact Registry, then deploy an immutable image digest.
5. Create least-privilege job and Scheduler service accounts; grant the job access to its exact secrets and database, and the Scheduler identity invocation permission only.
6. Add required secret versions and non-secret environment settings. Verify the sender domain and recipient with the transactional provider.
7. Configure one Cloud Run task, parallelism one, explicit task timeout, zero task retries initially, and command argument `send` only after preview validation.
8. Configure Scheduler's authenticated target, daily cron, and IANA time zone. Keep retry behavior conservative until daily delivery idempotency is persisted in PostgreSQL.
9. Run a manual preview execution, inspect logs/artifacts through the supported workflow, then run a controlled send to the owner before enabling daily unattended execution.

These are prerequisites only; this feature creates no resources and performs no deployment.

## Prepared command sequence (not executed)

The following Bash templates use placeholders only. They are not scripts run by this repository. Replace each placeholder after choosing the Google Cloud project, region, resource sizing, and verified sender. Creating Cloud SQL, Scheduler, and other resources can incur charges. Google Cloud CLI 587.0.0 is installed in the current user's local Programs folder and its Cloud Run/Scheduler help commands work, but no account is authenticated and no project is selected, so these commands have not been executed. Docker is unavailable and WSL is not installed, so local image build/smoke testing remains blocked on system-level container runtime setup.

```bash
export PROJECT_ID="<your-project-id>"
export REGION="<your-region>"
export PROJECT_NUMBER="<your-project-number>"
export JOB_NAME="personal-news-agent"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/news-agent/app:<immutable-tag>"
export RUNTIME_SA="news-agent-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
export SCHEDULER_SA="news-agent-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
export CLOUD_SQL_INSTANCE="${PROJECT_ID}:${REGION}:<instance-name>"

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com sqladmin.googleapis.com \
  secretmanager.googleapis.com

gcloud artifacts repositories create news-agent \
  --location "$REGION" --repository-format docker
gcloud builds submit --tag "$IMAGE" .
```

Provision a Cloud SQL PostgreSQL instance and database in the selected region, then create the runtime and scheduler service accounts. Grant the runtime identity `roles/cloudsql.client` and `roles/secretmanager.secretAccessor` only on the specific secret resources. Create Secret Manager versions for `database-url`, `openrouter-api-key`, and `resend-api-key` through a secure input path; never put their values in shell history, command arguments, or this document. `database-url` must contain the PostgreSQL URL and credentials, using the attached Cloud SQL Unix socket path (for example `host=/cloudsql/<project>:<region>:<instance>`). Enable the Cloud Run service identity's Cloud SQL access using `--set-cloudsql-instances`.

```bash
gcloud iam service-accounts create news-agent-runtime \
  --display-name "News agent runtime"
gcloud iam service-accounts create news-agent-scheduler \
  --display-name "News agent scheduler invoker"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" --role=roles/cloudsql.client
for SECRET_NAME in database-url openrouter-api-key resend-api-key; do
  gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/secretmanager.secretAccessor
done
```

Set these non-secret values for the job before scheduling it. Keep `EMAIL_FROM` and `NEWSLETTER_RECIPIENT` aligned with the provider's verified sender/allowed recipient.

```bash
gcloud run jobs deploy "$JOB_NAME" \
  --image "$IMAGE" --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --set-cloudsql-instances "$CLOUD_SQL_INSTANCE" \
  --tasks 1 --parallelism 1 --max-retries 0 --task-timeout 900s \
  --set-env-vars "APP_CONFIG_DIR=/app/config,DATABASE_BACKEND=postgres,NEWSLETTER_TIMEZONE=Asia/Kolkata,NEWSLETTER_RECIPIENT=<recipient>,LLM_MODEL=openrouter/free,LLM_BASE_URL=https://openrouter.ai/api/v1,EMAIL_FROM=<verified-sender>" \
  --set-secrets "DATABASE_URL=database-url:1,LLM_API_KEY=openrouter-api-key:1,RESEND_API_KEY=resend-api-key:1" \
  --args=send

# Initialize schema explicitly, then validate config and run a no-email preview.
gcloud run jobs execute "$JOB_NAME" --region "$REGION" --args=database-init --wait
gcloud run jobs execute "$JOB_NAME" --region "$REGION" --args=config-check --wait
gcloud run jobs execute "$JOB_NAME" --region "$REGION" --args=preview --wait
```

Inspect the configuration-check output, run history, Cloud Run execution result, and structured logs before considering a controlled send. The initial `send` argument is the production job's scheduled behavior; manual preview uses the one-execution `--args=preview` override shown above. Do not create the scheduler until the owner has reviewed a preview and separately authorized a real send.

```bash
gcloud run jobs add-iam-policy-binding "$JOB_NAME" --region "$REGION" \
  --member="serviceAccount:${SCHEDULER_SA}" --role=roles/run.invoker

gcloud scheduler jobs create http news-agent-morning \
  --location "$REGION" --schedule="0 7 * * *" --time-zone="Asia/Kolkata" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB_NAME}:run" \
  --http-method=POST \
  --oauth-service-account-email "$SCHEDULER_SA" \
  --max-retry-attempts=0
```

The scheduler targets the Google API, so it uses an OAuth token. Grant the Scheduler service agent the ability to mint tokens for the chosen scheduler identity if Google Cloud requires it for the project, and keep `roles/run.invoker` scoped to this one job. The documented job target and required OAuth mode follow Google's current [Cloud Run Job scheduling guide](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule) and [Cloud Run Job secrets guide](https://docs.cloud.google.com/run/docs/configuring/jobs/secrets). Verify current command syntax and IAM guidance with the selected `gcloud` version before applying.

## CI status

`.github/workflows/ci.yml` runs on pushes, pull requests, and manual dispatch. It installs the project with development dependencies, runs the full pytest suite, and runs Ruff. Those same checks pass locally, but this workflow has not been executed remotely: the local Git repository has no commits or remote configured. It deliberately does not deploy: a production deployment workflow should be added only after cloud identity federation, protected environments, approvals, and rollback controls are configured. No CI workflow receives production secrets today.

## Operational cost considerations

Costs vary by region, execution duration, database sizing, network egress, log retention, and current pricing. Cloud Run Jobs charge for task instance runtime (with a one-minute minimum); published Cloud Run pricing lists monthly vCPU and memory free-tier allowances, subject to account and regional terms. At one brief daily run, compute and one Cloud Scheduler job are likely small and may fit current free allowances. Cloud Scheduler currently includes three jobs per billing account free, then lists `$0.10` per job per 31 days. [Cloud Run pricing](https://cloud.google.com/run/pricing), [Cloud Scheduler pricing](https://cloud.google.com/scheduler/pricing)

Managed PostgreSQL is the likely baseline cost, not the once-daily container. For scale context, Google's pricing page currently lists the smallest shared-core Cloud SQL instance at `$0.0105/hour` in `us-central1` (about `$7.67` for a 730-hour month before storage, backups, networking, and region differences); this shared-core tier is not covered by the Cloud SQL SLA. Prices change and vary by region. A resilient production tier costs more. [Cloud SQL pricing](https://cloud.google.com/sql/pricing)

The OpenRouter model and transactional email provider may also charge or impose free-tier limits independent of Google Cloud. Set a billing budget, review the region-specific calculator, and check current provider pricing before deployment. “Low cost” does not mean guaranteed zero cost; Cloud SQL makes the durable design a paid service outside trial/free allowances.

## Security considerations

- Use dedicated service identities and least-privilege IAM; no service-account key files in the image.
- Inject versioned secrets at runtime and limit who can view, update, or execute the secret-bearing job.
- Use TLS/Cloud SQL connector for database transport; require database authentication and restrict network access.
- Keep the image non-root and free of `.env`, SQLite files, preview output, test data, and credentials.
- Pin image digests; scan/rebuild dependencies through a reviewed process. Current GitHub Actions checks tests and Ruff only; it does not build or deploy production images.
- Treat RSS text and model output as untrusted. Do not put article bodies, prompts, recipient data, or secrets in logs.
- Use least-privilege email sender and recipient configuration; preview command remains the safe default outside the explicitly configured production job.
- Keep database backups encrypted and access-controlled; test restore before relying on the history.

## Rollback strategy

1. Stop or pause the Cloud Scheduler job to prevent new executions.
2. Repoint the Cloud Run Job to the last known-good immutable image digest and restore its reviewed environment/secret version configuration.
3. If the new binary is compatible with the current schema, rerun only after inspecting the last run and delivery state. If a migration changed schema, use a backward-compatible expand/contract migration and restore from a verified backup only under an explicit recovery decision.
4. For a failed or ambiguous email delivery, do not trigger another send until the provider result and durable daily idempotency record are reconciled.
5. Resume Scheduler only after a manual preview and controlled send validate the rollback.

This application has no dashboard or automated rollback system. The rollback is a reviewed operations procedure using Cloud Run job revision/configuration, Scheduler pause/resume, image digests, and database backups.
