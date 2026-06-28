#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""WalrusClient — async Walrus HTTP client."""

import types
from typing import Any, ClassVar, cast

import httpx
from pysui import (
    AsyncClientBase,
    GetDynamicFields,
    PysuiClient,
    SuiCommand,
    SuiRpcResult,
    client_factory,
)

from pytusk.commands.walrus_command import WalrusCommand
from pytusk.config.tusk_config import PytuskConfiguration


class WalrusClient(AsyncClientBase):
    """Async client for Walrus HTTP operations and Sui transactions.

    Wraps an httpx.AsyncClient for Walrus daemon calls and an internal
    pysui async client for Sui-level operations. Dispatches WalrusCommand
    instances over HTTP and SuiCommand instances through the pysui client.

    Use as an async context manager to ensure the underlying httpx client
    is properly opened and closed.

    Args:
        pytusk_config (PytuskConfiguration): pytusk configuration carrying
            the active network endpoints and pysui config path.
    """

    _protocol: ClassVar[str] = "walrus-http"
    _pytusk_config: PytuskConfiguration
    _pysui_client: AsyncClientBase
    _httpx: httpx.AsyncClient

    def __init__(self, *, pytusk_config: PytuskConfiguration) -> None:
        """Initialise WalrusClient from a PytuskConfiguration.

        Args:
            pytusk_config (PytuskConfiguration): Active pytusk configuration.
        """
        self._pytusk_config = pytusk_config
        self._pysui_client = client_factory(pytusk_config.pysui_configuration)
        self._httpx = httpx.AsyncClient()

    # ------------------------------------------------------------------
    # AsyncClientBase abstract methods
    # ------------------------------------------------------------------

    async def execute_for_all(
        self,
        *,
        command: SuiCommand,
        timeout: float | None = None,
        headers: dict | None = None,
    ) -> SuiRpcResult:
        """Execute a paged SuiCommand, automatically fetching all pages.

        Forwards directly to the underlying pysui client's execute_for_all.
        In a future release this may also support paged Walrus commands.

        Args:
            command: A SuiCommand instance that supports pagination.
            timeout (float | None): Request timeout in seconds.
            headers (dict | None): Optional headers passed to the transport.

        Returns:
            SuiRpcResult: Aggregated result across all pages.
        """
        return await self._pysui_client.execute_for_all(
            command=command, timeout=timeout, headers=headers
        )

    async def execute(
        self,
        *,
        command: WalrusCommand | SuiCommand,
        timeout: float | None = None,
        headers: dict | None = None,
    ) -> SuiRpcResult:
        """Execute a WalrusCommand or SuiCommand.

        WalrusCommand instances are dispatched via httpx to the Walrus
        daemon. SuiCommand instances are forwarded to the internal pysui
        client.

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
        return cast(PysuiClient, self._pysui_client)

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

        All requests are routed to the configured Walrus daemon URL.

        Args:
            command (WalrusCommand): Command to dispatch.
            timeout (float | None): Request timeout in seconds.
            headers (dict[str, str] | None): Additional HTTP headers.

        Returns:
            SuiRpcResult: Parsed result from command.parse_response().
        """
        method = command.http_method()
        base_url = self._pytusk_config.network.walrus_url
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


async def get_walrus_epoch(client: WalrusClient) -> int:
    """Return the current Walrus epoch from the StakingInnerV1 dynamic field.

    Args:
        client (WalrusClient): Active pytusk client.

    Returns:
        int: Current Walrus epoch.

    Raises:
        RuntimeError: If the staking dynamic fields cannot be fetched.
    """
    df_result = await client.execute(
        command=GetDynamicFields(object_id=client.config.network.staking_object)
    )
    if not df_result.is_ok():
        raise RuntimeError(
            f"Cannot get staking dynamic fields: {df_result.result_string}"
        )
    dynamic_fields = df_result.result_data.dynamic_fields
    if not dynamic_fields:
        raise RuntimeError("Staking object has no dynamic fields")
    return int(
        dynamic_fields[0].field_object.json.struct_value.fields["value"]
        .struct_value.fields["epoch"].number_value
    )
