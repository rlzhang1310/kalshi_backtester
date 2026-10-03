from __future__ import annotations

from types import SimpleNamespace

from src.analysis.kalshi.toggleable_polling import ToggleablePollingSession


class FakeSession:
    def __init__(self, status: str = "active") -> None:
        self.game = SimpleNamespace(markets=[SimpleNamespace(status=status)])
        self.last_poll = 100
        self.refresh_seconds = 10
        self.closed = False
        self.calls = []

    def snapshot(self, since: int) -> dict:
        self.calls.append(since)
        return {"revision": since + 1, "live": self.game.markets[0].status != "settled"}

    def close(self) -> None:
        self.closed = True


def test_paused_api_chart_does_not_poll_and_resumes_immediately() -> None:
    underlying = FakeSession()
    chart = ToggleablePollingSession(underlying)
    assert chart.snapshot(4) == {
        "revision": 4,
        "live": False,
        "refreshable": True,
        "connection": "paused",
    }
    assert underlying.calls == []
    chart.set_enabled(True)
    assert underlying.last_poll == 0
    assert chart.snapshot(4) == {
        "revision": 5,
        "live": True,
        "refreshable": True,
        "connection": "polling",
    }
    chart.set_enabled(False)
    assert chart.snapshot(5)["revision"] == 5
    assert underlying.calls == [4]
    chart.close()
    assert underlying.closed


def test_settled_and_bounded_api_charts_cannot_enable_updates() -> None:
    for status, allow_updates, connection in (
        ("settled", True, "settled"),
        ("active", False, "static"),
    ):
        underlying = FakeSession(status)
        chart = ToggleablePollingSession(underlying, allow_updates=allow_updates)
        chart.set_enabled(True)
        assert not chart.available
        assert chart.snapshot(0) == {
            "revision": 0,
            "live": False,
            "refreshable": False,
            "connection": connection,
        }
        assert underlying.calls == []
        chart.close()


def test_api_chart_stops_refreshing_after_settlement() -> None:
    underlying = FakeSession()
    chart = ToggleablePollingSession(underlying)
    chart.set_enabled(True)
    underlying.game.markets[0].status = "settled"
    result = chart.snapshot(0)
    assert result["live"] is False
    assert result["refreshable"] is False
    assert result["connection"] == "settled"
    assert underlying.calls == []
    chart.close()
