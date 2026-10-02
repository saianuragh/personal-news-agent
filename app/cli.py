"""Command-line entry point for previewing or sending the daily briefing."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from app.database.factory import create_run_repository, repository_backend
from app.email.base import newsletter_idempotency_key
from app.email.credentials import (
    keyring_dependency_available,
    prompt_and_store_smtp_credential,
)
from app.env_file import load_local_env
from app.pipeline.runner import PipelineDependencies, PipelineRunner
from app.runtime_config import validate_send_configuration


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested pipeline mode and print a concise JSON result."""
    try:
        load_local_env()
    except ValueError as error:
        print(json.dumps({"status": "configuration_failed", "error": str(error)}))
        return 2
    parser = argparse.ArgumentParser(prog="personal-news-agent")
    parser.add_argument(
        "mode",
        choices=(
            "preview",
            "send",
            "database-init",
            "runs",
            "config-check",
            "email-credential-set",
            "delivery-reset",
        ),
    )
    parser.add_argument("--config-dir", type=Path, default=None)
    parser.add_argument("--preview-dir", type=Path, default=None)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of recent pipeline runs to show (runs mode only; 1-100).",
    )
    parser.add_argument(
        "--as-of",
        help="Timezone-aware ISO-8601 reference instant (defaults to current UTC time).",
    )
    parser.add_argument(
        "--date",
        help="Explicit local briefing date in YYYY-MM-DD format (delivery-reset only).",
    )
    parser.add_argument(
        "--with-ai",
        action="store_true",
        help="Call the configured LLM during preview; preview otherwise uses source text only.",
    )
    args = parser.parse_args(argv)
    if args.with_ai and args.mode != "preview":
        parser.error("--with-ai is only valid with preview mode")
    if args.limit is not None and args.mode != "runs":
        parser.error("--limit is only valid with runs mode")
    if args.mode == "delivery-reset" and args.date is None:
        parser.error("delivery-reset requires --date YYYY-MM-DD")
    if args.date is not None and args.mode != "delivery-reset":
        parser.error("--date is only valid with delivery-reset mode")
    try:
        if args.mode == "email-credential-set":
            if os.environ.get("EMAIL_PROVIDER", "smtp").strip().casefold() != "smtp":
                raise ValueError("Set EMAIL_PROVIDER=smtp before storing an SMTP credential")
            username = os.environ.get("SMTP_USERNAME", "").strip()
            if not username:
                raise ValueError("SMTP_USERNAME must be configured before storing a credential")
            if not keyring_dependency_available():
                raise RuntimeError(
                    "Install the optional windows-email extra with the Python 3.12 interpreter "
                    "used for Task Scheduler before setting an SMTP credential."
                )
            prompt_and_store_smtp_credential(username, prompt=getpass.getpass)
            print(json.dumps({"status": "smtp_credential_stored", "secret_displayed": False}))
            return 0
        if args.mode == "config-check":
            summary = validate_send_configuration(
                config_directory=args.config_dir or _default_config_directory()
            )
            print(
                json.dumps(
                    {"status": "configuration_valid", **asdict(summary)},
                    sort_keys=True,
                )
            )
            return 0
        repository = create_run_repository()
        if args.mode == "delivery-reset":
            try:
                if len(args.date) != 10 or args.date[4] != "-" or args.date[7] != "-":
                    raise ValueError
                briefing_date = date.fromisoformat(args.date)
                if briefing_date.isoformat() != args.date:
                    raise ValueError
            except ValueError:
                parser.error("--date must be a valid date in YYYY-MM-DD format")
            recipient = os.environ.get("NEWSLETTER_RECIPIENT", "").strip()
            if not recipient:
                raise ValueError("NEWSLETTER_RECIPIENT must be configured for delivery-reset")
            delivery_key = newsletter_idempotency_key(recipient, briefing_date)
            try:
                removed = repository.reset_delivery_claim(delivery_key)
            except Exception:
                print(
                    json.dumps(
                        {
                            "mode": "delivery-reset",
                            "date": args.date,
                            "status": "delivery_reset_failed",
                            "error": "The delivery claim could not be reset safely.",
                        },
                        sort_keys=True,
                    )
                )
                return 1
            print(
                json.dumps(
                    {
                        "mode": "delivery-reset",
                        "date": args.date,
                        "status": "delivery_claim_reset" if removed else "no_delivery_claim",
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.mode == "database-init":
            try:
                repository.initialize()
            except Exception:
                print(
                    json.dumps(
                        {
                            "status": "database_initialization_failed",
                            "backend": repository_backend(repository),
                            "error": (
                                "Database initialization failed; credentials were not displayed."
                            ),
                        }
                    )
                )
                return 1
            print(
                json.dumps(
                    {"status": "database_initialized", "backend": repository_backend(repository)}
                )
            )
            return 0
        if args.mode == "runs":
            limit = args.limit if args.limit is not None else 10
            try:
                records = repository.list_runs(limit=limit)
            except ValueError as error:
                print(json.dumps({"status": "invalid_run_history_limit", "error": str(error)}))
                return 2
            except Exception:
                print(
                    json.dumps(
                        {
                            "status": "run_history_unavailable",
                            "error": "Pipeline run history could not be read safely.",
                        }
                    )
                )
                return 1
            print(
                json.dumps(
                    {
                        "status": "runs_listed",
                        "count": len(records),
                        "runs": [_run_history_summary(record) for record in records],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
        # Keep provider request details out of routine logs; pipeline emits safe JSON events.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
        result = PipelineRunner(PipelineDependencies(run_repository=repository)).run(
            args.mode,
            as_of=as_of,
            config_directory=args.config_dir
            if args.config_dir is not None
            else _default_config_directory(),
            preview_directory=args.preview_dir
            if args.preview_dir is not None
            else _default_preview_directory(),
            ai_in_preview=args.with_ai,
        )
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"status": "configuration_failed", "error": str(error)}))
        return 2
    print(json.dumps(_result_summary(result), ensure_ascii=False, sort_keys=True))
    return result.exit_code


def _result_summary(result) -> dict[str, object]:
    delivery = result.delivery
    return {
        "run_id": str(result.run_id),
        "mode": result.mode,
        "as_of": result.as_of.isoformat(),
        "status": result.status,
        "counts": {
            name: getattr(result.counts, name) for name in result.counts.__dataclass_fields__
        },
        "source_failures": [
            {
                "stage": failure.stage,
                "source_id": failure.source_id,
                "error_type": failure.error_type,
                "message": failure.message,
            }
            for failure in result.source_failures
        ],
        "stage_failures": [
            {
                "stage": failure.stage,
                "error_type": failure.error_type,
                "message": failure.message,
                "category": failure.category,
                "http_status": failure.http_status,
                "provider_error_type": failure.provider_error_type,
                "provider_error_code": failure.provider_error_code,
            }
            for failure in result.stage_failures
        ],
        "newsletter": {
            "included_story_ids": [str(item) for item in result.newsletter.included_story_ids],
            "omitted_story_ids": [str(item) for item in result.newsletter.omitted_story_ids],
            "preview_html_path": str(delivery.preview_html_path)
            if delivery and delivery.preview_html_path
            else None,
            "preview_text_path": str(delivery.preview_text_path)
            if delivery and delivery.preview_text_path
            else None,
        }
        if result.newsletter
        else None,
        "delivery": {
            "status": delivery.status,
            "success": delivery.success,
            "retryable": delivery.retryable,
            "provider": delivery.provider,
            "provider_message_id": delivery.provider_message_id,
            "error": delivery.error,
        }
        if delivery
        else None,
        "error": result.error,
    }


def _run_history_summary(record: dict[str, object]) -> dict[str, object]:
    """Expose useful run facts while omitting persisted diagnostic text."""
    fields = (
        "run_id",
        "started_at",
        "completed_at",
        "as_of",
        "status",
        "mode",
        "sources_attempted",
        "sources_succeeded",
        "sources_failed",
        "raw_entries",
        "article_count",
        "duplicate_count",
        "story_count",
        "categorized_count",
        "ranked_count",
        "selected_count",
        "summarized_count",
        "fallback_count",
        "summary_failure_count",
        "omitted_count",
        "delivery_outcome",
    )
    return {name: record.get(name) for name in fields}


def _default_config_directory() -> Path:
    return Path(
        os.environ.get(
            "APP_CONFIG_DIR", str(Path(__file__).resolve().parents[1] / "config")
        ).strip()
        or Path(__file__).resolve().parents[1] / "config"
    )


def _default_preview_directory() -> Path:
    return Path(
        os.environ.get(
            "PREVIEW_DIRECTORY",
            str(Path(__file__).resolve().parents[1] / "data" / "previews"),
        ).strip()
        or Path(__file__).resolve().parents[1] / "data" / "previews"
    )


if __name__ == "__main__":
    sys.exit(main())
