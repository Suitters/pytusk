#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for ``pytusk.client.walrus_client.WalrusClient``.

Scope, deliberately narrow: this module covers ``_send``'s transport-level
exception handling only (FIX 1 -- the ``except`` clause broadened from
``except httpx.HTTPError`` to ``except (httpx.HTTPError, ssl.SSLError,
OSError)``). Request-BUILDING and base-URL-resolution logic (everything
before ``_send``'s own try/except) is already covered by
``TestBaseUrlResolution`` in ``test_node_commands.py``, which this module
follows for its own real-``WalrusClient``-plus-monkeypatched-transport
pattern: constructing a real ``WalrusClient`` only exercises pysui's
config-driven ``client_factory`` (no network I/O at construction time), so a
real client is used here too, with only ``client._httpx.request``
monkeypatched to raise the transport exception under test.
"""

import asyncio
import ssl
import tempfile
from typing import Any

import httpx
import pytest

from pytusk.client.walrus_client import WalrusClient
from pytusk.commands.node_commands import PutSliver
from pytusk.config.tusk_config import PytuskConfiguration

BASE_URL = "https://node-1.example.com:9185"


def _put_sliver_command() -> PutSliver:
    """A well-formed ``PutSliver`` command used only to drive ``_send``;
    its URL/body shape is not itself under test here -- see
    ``test_node_commands.py`` for that."""
    return PutSliver(
        blob_id=b"x" * 32, sliver_pair_index=0, sliver_type="primary", data=b"payload"
    )


@pytest.fixture
def client() -> Any:
    """A real ``WalrusClient`` built from an empty temp-dir config.

    Constructing a real ``WalrusClient`` only exercises pysui's
    config-driven ``client_factory`` (no network I/O at construction time),
    matching ``TestBaseUrlResolution``'s fixture in ``test_node_commands.py``.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg = PytuskConfiguration(from_cfg_path=tmp_dir)
        yield WalrusClient(pytusk_config=cfg)


class TestSendTransportExceptionHandling:
    """FIX 1: ``_send``'s transport ``except`` clause was broadened from
    ``except httpx.HTTPError`` to ``except (httpx.HTTPError, ssl.SSLError,
    OSError)``, so a raw ``ssl.SSLError`` -- observed escaping the transport
    layer entirely on a live 1 GB upload and aborting the whole run -- now
    becomes a structured ``SuiRpcResult`` failure instead of propagating.
    This class pins that conversion for all three caught exception types.
    """

    async def test_ssl_error_becomes_failed_result(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raw ``ssl.SSLError`` raised by the transport must not
        propagate; ``_send`` must convert it into a failed ``SuiRpcResult``
        whose message carries the exception type name, the request URL,
        and the original detail text."""

        async def fake_request(**kwargs: Any) -> httpx.Response:
            raise ssl.SSLError("[SSL: SSLV3_ALERT_BAD_RECORD_MAC] bad record mac")

        monkeypatch.setattr(client._httpx, "request", fake_request)
        cmd = _put_sliver_command()
        expected_url = cmd.url_path(BASE_URL)

        result = await client._send(
            command=cmd, base_url=BASE_URL, timeout=None, headers=None
        )

        assert result.is_ok() is False
        assert "SSLError" in result.result_string
        assert expected_url in result.result_string
        assert "bad record mac" in result.result_string

    async def test_os_error_becomes_failed_result(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raw ``OSError`` (a socket-level failure not wrapped by httpx)
        must likewise become a failed ``SuiRpcResult`` rather than
        propagate."""

        async def fake_request(**kwargs: Any) -> httpx.Response:
            raise OSError("Network is unreachable")

        monkeypatch.setattr(client._httpx, "request", fake_request)
        cmd = _put_sliver_command()
        expected_url = cmd.url_path(BASE_URL)

        result = await client._send(
            command=cmd, base_url=BASE_URL, timeout=None, headers=None
        )

        assert result.is_ok() is False
        assert "OSError" in result.result_string
        assert expected_url in result.result_string
        assert "Network is unreachable" in result.result_string

    async def test_httpx_http_error_becomes_failed_result(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-existing branch -- an ``httpx.HTTPError`` subclass --
        must continue to become a failed ``SuiRpcResult``, unaffected by
        the broadened ``except`` clause."""

        async def fake_request(**kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        monkeypatch.setattr(client._httpx, "request", fake_request)
        cmd = _put_sliver_command()
        expected_url = cmd.url_path(BASE_URL)

        result = await client._send(
            command=cmd, base_url=BASE_URL, timeout=None, headers=None
        )

        assert result.is_ok() is False
        assert "ConnectError" in result.result_string
        assert expected_url in result.result_string
        assert "Connection refused" in result.result_string


class TestSendCancelledErrorPropagates:
    """Companion to ``TestSendTransportExceptionHandling``:
    ``asyncio.CancelledError`` is a ``BaseException``, not an
    ``Exception``/``OSError`` subclass, so it is NOT matched by ``_send``'s
    ``except (httpx.HTTPError, ssl.SSLError, OSError)`` clause and must
    continue to propagate out of ``_send`` uncaught. This guards against a
    future well-meaning broadening of that clause to ``except Exception``,
    which WOULD swallow it."""

    async def test_cancelled_error_is_not_swallowed(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``CancelledError`` raised by the mocked transport must still
        propagate out of ``_send`` rather than being converted into a
        failed ``SuiRpcResult``."""

        async def fake_request(**kwargs: Any) -> httpx.Response:
            raise asyncio.CancelledError()

        monkeypatch.setattr(client._httpx, "request", fake_request)
        cmd = _put_sliver_command()

        with pytest.raises(asyncio.CancelledError):
            await client._send(
                command=cmd, base_url=BASE_URL, timeout=None, headers=None
            )
