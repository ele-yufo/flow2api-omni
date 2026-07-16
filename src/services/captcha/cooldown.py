"""Per-project reCAPTCHA rejection streak + exponential-backoff cooldown tracker.

Extracted from FlowClient (P5). When Google rejects reCAPTCHA, we back off per project
(10s, 20s, 40s, 80s, 120s cap). `clock` is injectable for deterministic tests.
Behavior locked by tests/characterization/test_captcha_cooldown.py.
"""
import time
from typing import Callable, Dict, Optional

from ...shared.telemetry import debug_logger


class CaptchaCooldownTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._streaks: Dict[str, int] = {}
        self._cooldowns_until: Dict[str, float] = {}

    def key(self, project_id: Optional[str]) -> str:
        return str(project_id or "").strip() or "_global"

    def record_rejection(self, project_id: Optional[str]) -> float:
        key = self.key(project_id)
        streak = int(self._streaks.get(key, 0) or 0) + 1
        self._streaks[key] = streak
        delay = min(120.0, 10.0 * (2 ** (streak - 1)))
        self._cooldowns_until[key] = self._clock() + delay
        debug_logger.log_warning(
            f"[reCAPTCHA] upstream rejection streak={streak}, cooldown={delay:.0f}s, project_id={project_id}"
        )
        return delay

    def get_cooldown_delay(self, project_id: Optional[str]) -> float:
        key = self.key(project_id)
        until = float(self._cooldowns_until.get(key, 0.0) or 0.0)
        remaining = until - self._clock()
        return remaining if remaining > 0 else 0.0

    def clear(self, project_id: Optional[str]):
        key = self.key(project_id)
        self._streaks.pop(key, None)
        self._cooldowns_until.pop(key, None)
