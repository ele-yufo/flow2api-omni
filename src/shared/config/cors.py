"""Explicit CORS-origin configuration for browser API clients."""

import os
from typing import Any
from urllib.parse import urlsplit


class CorsConfigMixin:
    """Parse same-origin exceptions for web consoles and Chrome extensions."""

    _config: dict[str, Any]

    @property
    def server_cors_allowed_origins(self) -> list[str]:
        """Return normalized explicit web/extension origins for browser API calls."""
        environment_value = os.environ.get("FLOW2API_CORS_ALLOWED_ORIGINS")
        raw_origins = (
            environment_value.split(",")
            if environment_value is not None
            else self._config.get("server", {}).get("cors_allowed_origins", [])
        )
        if isinstance(raw_origins, str):
            raw_origins = raw_origins.split(",")
        if not isinstance(raw_origins, (list, tuple)):
            raise ValueError("CORS origins must be a TOML array or comma-separated string")

        allowed_origins: list[str] = []
        for raw_origin in raw_origins:
            origin = str(raw_origin or "").strip().rstrip("/")
            if not origin:
                continue
            parsed = urlsplit(origin)
            valid_scheme = parsed.scheme in {"http", "https", "chrome-extension"}
            if (
                origin == "*"
                or not valid_scheme
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"Invalid explicit CORS origin: {origin!r}")
            if origin not in allowed_origins:
                allowed_origins.append(origin)
        return allowed_origins
