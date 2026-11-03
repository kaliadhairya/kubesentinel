import os
import time
import tempfile
import pytest
from app.services.runbooks import RunbookService


def test_runbook_service_loading():
    """Validates loading default markdown runbooks."""
    service = RunbookService()
    runbooks = service.get_all_runbooks()
    assert len(runbooks) >= 3

    slugs = [r["slug"] for r in runbooks]
    assert "oom_killed" in slugs
    assert "crash_loop" in slugs
    assert "high_latency" in slugs


def test_runbook_search():
    """Validates symptom matching logic in runbooks."""
    service = RunbookService()

    # Search OOMKilled
    match = service.search_runbook("exit code 137 OOM")
    assert match is not None
    assert "oom_killed" in match["slug"]

    # Search 5xx latency
    match = service.search_runbook("5xx errors and high latency")
    assert match is not None
    assert "high_latency" in match["slug"]


def test_runbook_hot_reloading():
    """Validates mtime hot-reloading without service restarts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        service = RunbookService(runbooks_dir=tmpdir)
        assert len(service.get_all_runbooks()) == 0

        # Create new runbook
        file1 = os.path.join(tmpdir, "db_deadlock.md")
        with open(file1, "w") as f:
            f.write("# Runbook: PostgreSQL Lock Deadlock\n\nKill blocking queries.")

        # Should load immediately
        assert len(service.get_all_runbooks()) == 1
        found = service.search_runbook("PostgreSQL deadlock")
        assert found is not None
        assert "Deadlock" in found["title"]

        # Modify file
        time.sleep(0.05)
        with open(file1, "w") as f:
            f.write("# Runbook: PostgreSQL Critical Lock Saturation\n\nScale pool.")

        found = service.search_runbook("Saturation")
        assert found is not None
        assert "Critical" in found["title"]
