"""POST /api/shutdown must stop the app without a console (Windows launcher, pythonw)."""

import asyncio
import types
from unittest.mock import MagicMock, patch

import pytest

from screenmind.api import dependencies
from screenmind.api.routes.settings import shutdown_server


def _request(host="127.0.0.1"):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=host))


def test_uses_main_shutdown_path_not_a_signal(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(dependencies, "request_shutdown", called)

    async def run():
        with patch("os.kill") as kill:
            result = await shutdown_server(_request())
            await asyncio.sleep(0.7)  # the route waits 0.5 s before shutting down
            return result, kill

    result, kill = asyncio.run(run())
    assert result["ok"] is True
    called.assert_called_once()
    kill.assert_not_called()  # CTRL_C_EVENT fails without a console (WinError 233)


def test_remote_clients_are_refused(monkeypatch):
    monkeypatch.setattr(dependencies, "request_shutdown", MagicMock())
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(shutdown_server(_request("192.168.1.20")))
    dependencies.request_shutdown.assert_not_called()
