from pathlib import Path

import yaml


def test_cloud_workflow_schedules_ist_and_supports_manual_production_runs() -> None:
    workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "daily-newsletter.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert workflow["on"]["schedule"][0]["cron"] == "30 1 * * *"
    assert "workflow_dispatch" in workflow["on"]
    job = workflow["jobs"]["send-newsletter"]
    assert "${{ secrets.SMTP_PASSWORD }}" == job["env"]["SMTP_PASSWORD"]
    assert "${{ secrets.NEWSLETTER_RECIPIENT }}" == job["env"]["NEWSLETTER_RECIPIENT"]
    assert "${{ secrets.SMTP_USERNAME }}" == job["env"]["SMTP_USERNAME"]
    assert "${{ secrets.EMAIL_FROM }}" == job["env"]["EMAIL_FROM"]
    assert not any("DATABASE" in name for name in job["env"])
    assert (
        "${{ secrets.NEWS_AGENT_DATABASE_URL }}"
        not in workflow["jobs"]["send-newsletter"]["env"].values()
    )
    commands = [step["run"] for step in job["steps"] if "run" in step]
    assert commands == [
        "python -m pip install .",
        "personal-news-agent config-check",
        "personal-news-agent send",
    ]
    assert "default_branch" in job["if"]
    assert "github.event_name == 'schedule'" in job["if"]
