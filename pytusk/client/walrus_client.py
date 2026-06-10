#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""WalrusClient — async Walrus HTTP client."""

import types
from typing import Any, ClassVar

import httpx
from pysui import (
    AsyncClientBase,
    PysuiClient,
    SuiRpcResult,
    client_factory,
)

from pytusk.commands.walrus_command import WalrusCommand
from pytusk.config.tusk_config import PytuskConfiguration, WalrusNetworkConfig


class WalrusClient(AsyncClientBase):
    """Async client for Walrus HTTP operations and Sui transactions.

    Wraps an httpx.AsyncClient for Walrus aggregator/publisher calls and
    an internal pysui async client for Sui-level operations. Dispatches
    WalrusCommand instances over HTTP and SuiCommand instances through
    the pysui client.

    Use as an async context manager to ensure the underlying httpx client
    is properly opened and closed.

    Args:
        pytusk_config (PytuskConfiguration): pytusk configuration carrying
            the active network endpoints and pysui config path.
    """

    _protocol: ClassVar[str] = "walrus-http"
    _pytusk_config: PytuskConfiguration
    _network: WalrusNetworkConfig
    _pysui_client: AsyncClientBase
    _httpx: httpx.AsyncClient

    def __init__(self, *, pytusk_config: PytuskConfiguration) -> None:
        """Initialise WalrusClient from a PytuskConfiguration.

        Args:
            pytusk_config (PytuskConfiguration): Active pytusk configuration.
        """
        self._pytusk_config = pytusk_config
        network = pytusk_config.active_network_entry
        self._network = network
        self._pysui_client = client_factory(pytusk_config.pysui_configuration)
        self._httpx = httpx.AsyncClient()

    # ------------------------------------------------------------------
    # AsyncClientBase abstract methods
    # ------------------------------------------------------------------

    async def execute_for_all(
        self,
        *,
        command: Any,
        timeout: float | None = None,
    ) -> SuiRpcResult:
        """Execute a paged SuiCommand, automatically fetching all pages.

        Forwards directly to the underlying pysui client's execute_for_all.
        In a future release this may also support paged Walrus commands.

        Args:
            command: A SuiCommand instance that supports pagination.
            timeout (float | None): Request timeout in seconds.

        Returns:
            SuiRpcResult: Aggregated result across all pages.
        """
        return await self._pysui_client.execute_for_all(
            command=command, timeout=timeout
        )

    async def execute(
        self,
        *,
        command: Any,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> SuiRpcResult:
        """Execute a WalrusCommand or SuiCommand.

        WalrusCommand instances are dispatched via httpx to the Walrus
        aggregator or publisher. SuiCommand instances are forwarded to
        the internal pysui client.

        Args:
            command: A WalrusCommand or SuiCommand instance.
            timeout (float | None): Request timeout in seconds.
            headers (dict[str, str] | None): Additional HTTP headers.

        Returns:
            SuiRpcResult: Success result with typed data, or error result
            with result_string set to the error message.
        """
        if isinstance(command, WalrusCommand):
            return await self._dispatch_walrus(
                command, timeout=timeout, headers=headers
            )
        return await self._pysui_client.execute(
            command=command, timeout=timeout, headers=headers
        )

    @property
    def pysui_client(self) -> PysuiClient:
        """The underlying pysui async client."""
        return self._pysui_client  # type: ignore

    @property
    def config(self) -> PytuskConfiguration:
        """The PytuskConfiguration driving this client."""
        return self._pytusk_config

    async def transaction(self, **kwargs: Any) -> Any:
        """Return a new async transaction builder from the pysui client.

        Returns:
            AsyncTransaction: pysui transaction builder for constructing PTBs.
        """
        return await self._pysui_client.transaction(**kwargs)

    async def __aenter__(self) -> "WalrusClient":
        """Open the underlying httpx client."""
        await self._httpx.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Close the underlying httpx client."""
        await self._httpx.__aexit__(exc_type, exc_val, exc_tb)

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    async def _dispatch_walrus(
        self,
        command: WalrusCommand,
        *,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> SuiRpcResult:
        """Dispatch a WalrusCommand over HTTP.

        GET requests are routed to the aggregator; PUT/POST requests are
        routed to the publisher.

        Args:
            command (WalrusCommand): Command to dispatch.
            timeout (float | None): Request timeout in seconds.
            headers (dict[str, str] | None): Additional HTTP headers.

        Returns:
            SuiRpcResult: Parsed result from command.parse_response().
        """
        method = command.http_method()
        if method != "GET" and not self._network.walrus_publisher:
            raise ValueError(
                "No publisher URL is configured for this network. "
                "Mainnet has no public unauthenticated publisher. "
                "Set walrus_publisher to a community publisher endpoint "
                "from https://docs.wal.app/docs/network-reference or run your own."
            )
        base_url = (
            self._network.walrus_aggregator
            if method == "GET"
            else self._network.walrus_publisher
        )
        url = command.url_path(base_url)
        params = command.query_params() or None
        body = command.request_body()
        form = command.form_files()

        try:
            if form:
                httpx_files: dict[str, tuple[str, bytes, str]] = {
                    key: (key, data, "application/octet-stream")
                    for key, data in form.items()
                }
                response = await self._httpx.request(
                    method=method,
                    url=url,
                    params=params,
                    files=httpx_files,
                    timeout=timeout,
                    headers=headers or {},
                )
            elif body is not None:
                response = await self._httpx.request(
                    method=method,
                    url=url,
                    params=params,
                    content=body,
                    timeout=timeout,
                    headers=headers or {},
                )
            else:
                response = await self._httpx.request(
                    method=method,
                    url=url,
                    params=params,
                    timeout=timeout,
                    headers=headers or {},
                )
        except httpx.HTTPError as exc:
            return SuiRpcResult(False, str(exc))

        return command.parse_response(response)
