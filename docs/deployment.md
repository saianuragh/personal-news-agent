# Deployment model

The supported unattended cloud scheduler is GitHub Actions; the local Windows Task Scheduler task is an optional fallback. See [cloud deployment](cloud-deployment.md) or [Windows scheduling](free-local-deployment.md).

Cloud Run, Cloud SQL, Neon, and other database-backed deployment recipes are not part of the current production path. The daily workflow installs and runs the Python package directly on a hosted GitHub runner. No container image, database service, or cloud database credential is needed.
