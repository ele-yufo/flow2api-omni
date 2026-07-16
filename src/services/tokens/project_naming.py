"""Pooled project naming (base normalization + "<base> P<n>" formatting).

Extracted from TokenManager. Pure given a clock (empty base falls back to a timestamp).
Locked by tests/characterization/test_project_naming.py.
"""
from datetime import datetime
from typing import Optional


def normalize_project_name_base(project_name: Optional[str] = None,
                                now: Optional[datetime] = None) -> str:
    """Normalize a project base name for pooled creation."""
    raw_name = (project_name or "").strip()
    if raw_name:
        parts = raw_name.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].startswith("P") and parts[1][1:].isdigit():
            return parts[0]
        return raw_name
    if now is None:
        now = datetime.now()
    return now.strftime("%b %d - %H:%M")


def build_project_name(pool_index: int, base_name: Optional[str] = None,
                       now: Optional[datetime] = None) -> str:
    """Build a project name for the pool."""
    normalized_base = normalize_project_name_base(base_name, now)
    return f"{normalized_base} P{pool_index}"
