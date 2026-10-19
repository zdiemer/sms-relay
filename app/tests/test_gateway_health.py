"""Monitoring must reflect the handset without breaking kubelet health."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest


@pytest.mark.parametrize("result, expected", [
    (200, 200), (401, 503), (500, 503), (302, 503),
    (httpx.ConnectError("private diagnostic"), 503),
    (TimeoutError("private diagnostic"), 503),
])
def test_gateway_health(client, result, expected):
    settings = SimpleNamespace(provider="android_gateway", gateway_url="http://phone:8080",
                               gateway_user="user", gateway_password="secret")
    request = AsyncMock()
    if isinstance(result, Exception):
        request.side_effect = result
    else:
        request.return_value = httpx.Response(result)
    with patch("sms_relay.routes.health.settings", settings), patch("httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value.get = request
        response = client.get("/api/health/gateway")
        assert response.status_code == expected
        assert response.json() == {"status": "ok" if expected == 200 else "unavailable"}
        request.assert_awaited_once_with("http://phone:8080/")
        assert factory.call_args.kwargs["follow_redirects"] is False
        assert factory.call_args.kwargs["auth"] == ("user", "secret")
    assert client.get("/api/health").status_code == 200


def test_gateway_unconfigured(client):
    with patch("sms_relay.routes.health.settings", SimpleNamespace(provider="dev")):
        assert client.get("/api/health/gateway").status_code == 503
