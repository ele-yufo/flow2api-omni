#!/usr/bin/env python3
"""Compatibility diagnostic for the production browser keepalive one-shot path."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
PRODUCTION_ENTRYPOINT = SCRIPT_DIR / "keepalive_browser.py"


def _canonical_positive_int(raw_value: str) -> int:
    value = str(raw_value)
    if not value.isascii() or not value.isdigit() or value.startswith("0"):
        raise argparse.ArgumentTypeError("must use canonical positive decimal form")
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the production keepalive one-shot compatibility diagnostic"
    )
    parser.add_argument(
        "--token-id",
        required=True,
        type=_canonical_positive_int,
        help="canonical positive token ID to validate",
    )
    return parser.parse_args(argv)


def _load_production_entrypoint() -> Callable[[list[str]], int]:
    spec = importlib.util.spec_from_file_location(
        "flow2api_keepalive_browser_entrypoint",
        PRODUCTION_ENTRYPOINT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("production keepalive entrypoint could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def run_gate(
    token_id: int,
    *,
    entrypoint: Callable[[list[str]], int] | None = None,
) -> int:
    runner = entrypoint or _load_production_entrypoint()
    return runner(["--once", "--token-id", str(token_id)])


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    return run_gate(args.token_id)


if __name__ == "__main__":
    raise SystemExit(main())
