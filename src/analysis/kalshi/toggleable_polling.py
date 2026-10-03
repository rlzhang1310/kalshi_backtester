"""Pause or resume REST polling without replacing the current chart session."""

from __future__ import annotations

from threading import Lock

from src.analysis.kalshi.single_game_odds_time_series import game_is_unsettled
from visualize_game import LiveGameSession


class ToggleablePollingSession:
    def __init__(self, session: LiveGameSession, *, allow_updates: bool = True) -> None:
        self.session = session
        self.allow_updates = allow_updates
        self.enabled = False
        self.lock = Lock()

    @property
    def available(self) -> bool:
        return self.allow_updates and game_is_unsettled(self.session.game)

    def set_enabled(self, enabled: bool) -> None:
        with self.lock:
            next_enabled = bool(enabled and self.available)
            if next_enabled and not self.enabled:
                # Fetch at the next browser check, not after a whole interval.
                self.session.last_poll = 0
            self.enabled = next_enabled

    def snapshot(self, since: int) -> dict:
        with self.lock:
            if self.enabled and self.available:
                result = self.session.snapshot(since)
            else:
                # Preserve the browser's revision while paused so a pending
                # changed figure is still delivered when updates resume.
                result = {"revision": since, "live": False}
            available = self.available
            if not available:
                self.enabled = False
            if self.enabled:
                connection = "polling"
            elif not game_is_unsettled(self.session.game):
                connection = "settled"
            elif not self.allow_updates:
                connection = "static"
            else:
                connection = "paused"
            result.update(
                live=self.enabled and available,
                refreshable=available,
                connection=connection,
            )
            return result

    def close(self) -> None:
        with self.lock:
            self.enabled = False
            self.session.close()
