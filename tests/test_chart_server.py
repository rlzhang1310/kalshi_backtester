from __future__ import annotations

import json
from urllib.request import urlopen

from src.analysis.kalshi.chart_server import ChartServer


def test_chart_server_serves_chart_and_versioned_snapshot() -> None:
    class Session:
        def snapshot(self, since):
            return {"revision": since + 1, "live": True}

    server = ChartServer(b"<html>chart</html>", Session())
    try:
        with urlopen(server.url) as response:
            assert response.read() == b"<html>chart</html>"
        with urlopen(server.url + "?fullscreen=1") as response:
            assert response.read() == b"<html>chart</html>"
        with urlopen(server.url + "snapshot?since=3") as response:
            assert json.load(response) == {"revision": 4, "live": True}
    finally:
        server.close()
