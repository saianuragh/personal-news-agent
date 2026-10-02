from pathlib import Path

import yaml


def test_cloud_workflow_schedules_ist_and_supports_manual_production_runs() -> None:
    workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "daily-newsletter.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert workflow["on"]["schedule"][0]["cron"] == "30 1 * * *"
    assert "workflow_dispatch" in workflow["on"]
    job = workflow["jobs"]["send-newsletter"]
    assert job["env"]["DATABASE_BACKEND"] == "postgres"
    assert "${{ secrets.NEWS_AGENT_DATABASE_URL }}" == job["env"]["DATABASE_URL"]
    assert "${{ secrets.SMTP_PASSWORD }}" == job["env"]["SMTP_PASSWORD"]
    commands = [step["run"] for step in job["steps"] if "run" in step]
    assert commands == [
        "python -m pip install .",
        "personal-news-agent config-check",
        "personal-news-agent database-init",
        "personal-news-agent send",
    ]
    assert "default_branch" in job["if"]
    assert "github.event_name == 'schedule'" in job["if"]
