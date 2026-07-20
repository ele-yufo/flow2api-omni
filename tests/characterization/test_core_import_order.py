"""Core modules must import in a fresh interpreter without service-layer cycles."""

import subprocess
import sys
from pathlib import Path


def test_database_import_has_no_service_layer_cycle():
    project_root = Path(__file__).parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.core.database import Database; print(Database.__name__)",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Database"
