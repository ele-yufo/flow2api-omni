"""Documentation contracts for browser-backed account lifecycle operations."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
README = PROJECT_ROOT / "README.md"
ARCHITECTURE = PROJECT_ROOT / "docs" / "architecture.md"
RUNBOOK = PROJECT_ROOT / "docs" / "operations" / "browser-keepalive.md"


def test_readme_describes_current_browser_backed_lifecycle_and_runbook():
    source = README.read_text(encoding="utf-8")

    for term in (
        "token_lifecycle",
        "persistent",
        "warm",
        "XRDP",
        "browser-keepalive.md",
        "membership_expired",
    ):
        assert term in source
    assert "纯 HTTP" not in source or "无限续期" not in source


def test_architecture_documents_sidecar_transactions_and_onboarding():
    source = ARCHITECTURE.read_text(encoding="utf-8")

    for term in (
        "services/keepalive",
        "OnboardingService",
        "token_lifecycle",
        "onboarding_jobs",
        "BEGIN IMMEDIATE",
        "Xvfb",
        "XRDP",
    ):
        assert term in source


def test_browser_keepalive_runbook_covers_required_operations_and_security():
    assert RUNBOOK.exists()
    source = RUNBOOK.read_text(encoding="utf-8")

    for term in (
        "persistent",
        "warm",
        "1200",
        "43200",
        ":10",
        ":11",
        "--preflight",
        "--once --token-id",
        "journalctl",
        "archive_and_replace",
        "membership_expired",
        "ST_REVOKED",
        "GRANT_EXPIRED",
        "429_rate_limit",
        "FLOW2API_CORS_ALLOWED_ORIGINS",
        "BROWSER_EXECUTABLE_PATH",
        "回滚",
    ):
        assert term in source
    assert "KEEPALIVE_TASK.md" not in source
