#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""WalrusClient — async Walrus HTTP client."""

import ssl
import types
from typing import Any, ClassVar, TypedDict, cast

import httpx
from pysui import (
    AsyncClientBase,
    PysuiClient,
    SuiCommand,
    SuiRpcResult,
    client_factory,
)

from pytusk.commands.walrus_command import WalrusCommand
from pytusk.config.tusk_config import PytuskConfiguration
from pytusk.core.committee import WalrusCommittee, fetch_committee, fetch_epoch

_DEFAULT_TIMEOUT: httpx.Timeout = httpx.Timeout(
    connect=5.0, read=300.0, write=300.0, pool=60.0
)
"""Default per-request timeout, mirroring upstream walrus-sdk's reqwest
client: ``connect=5.0`` matches ``DEFAULT_CONNECT_TIMEOUT``, and
``read``/``write=300.0`` (5 minutes) matches ``total_timeout()`` -- a FLAT
figure that deliberately does NOT scale with sliver size (upstream applies
the same 5-minute ceiling to a 1-byte sliver and a multi-megabyte one).
``pool=60.0`` has no direct upstream analogue (reqwest has no separate
pool-checkout timeout); 60s is a generous bound so a request queued behind
the connection pool under heavy fan-out is not aborted prematurely.
"""


class _RequestKwargs(TypedDict, total=False):
    """Optional keyword arguments forwarded to ``httpx.AsyncClient.request``.

    Kept as a narrow TypedDict (rather than ``dict[str, object]``) so mypy
    can verify the ``**request_kwargs`` unpacking against each ``request()``
    overload's typed keyword parameters.
    """

    timeout: float | httpx.Timeout


class WalrusClient(AsyncClientBase):
    """Async client for Walrus HTTP operations and Sui transactions.

    Wraps an httpx.AsyncClient for Walrus aggregator/publisher calls and an
    internal pysui async client for Sui-level operations. Dispatches
    WalrusCommand instances over HTTP and SuiCommand instances through the
    pysui client.

    Use as an async context manager to ensure the underlying httpx client
    is properly opened and closed.

    The underlying httpx client is built with ``http2=True``. This is
    upstream parity, not an incidental choice: upstream walrus-sdk's
    reqwest client forces HTTP/2 via ``.http2_prior_knowledge()``, so its
    per-node concurrency of 10 (~101 nodes in a mainnet-sized committee)
    multiplexes over roughly ONE TCP connection per node. Over HTTP/1.1,
    each concurrent request needs its OWN connection, so the same fan-out
    demands roughly ``n_nodes * per_node_concurrency`` connections (~1010)
    against the pool -- see ``limits`` below and Change 3 in the native
    sliver-upload parity work. ``h2`` is a required dependency for this to
    work (already pulled in transitively via pysui); there is no HTTP/1.1
    fallback path.

    Args:
        pytusk_config (PytuskConfiguration): pytusk configuration carrying
            the active network endpoints and pysui config path.
        limits (httpx.Limits | None): Connection-pool limits for the
            underlying httpx client. Defaults to None, in which case
            ``httpx.Limits(max_connections=1024, max_keepalive_connections=512)``
            is used. This must exceed the HTTP/1.1 worst-case connection
            demand of native-upload sliver PUTs against a ~101-node storage
            committee at a per-node concurrency of 10 (~1010 connections),
            so the pool is never the binding constraint even if HTTP/2
            negotiation somehow falls back to HTTP/1.1. Upstream imposes NO
            transport-level connection cap at all; concurrency there is
            bounded purely by task-level semaphores.
        timeout (httpx.Timeout | None): Default per-request timeout for the
            underlying httpx client. Defaults to :data:`_DEFAULT_TIMEOUT`
            (``connect=5.0, read=300.0, write=300.0, pool=60.0``) when
            None, mirroring upstream's connect/total timeout values.
    """

    _protocol: ClassVar[str] = "walrus-http"
    _pytusk_config: PytuskConfiguration
    _pysui_client: AsyncClientBase
    _httpx: httpx.AsyncClient

    def __init__(
        self,
        *,
        pytusk_config: PytuskConfiguration,
        limits: httpx.Limits | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        """Initialise WalrusClient from a PytuskConfiguration.

        Args:
            pytusk_config (PytuskConfiguration): Active pytusk configuration.
            limits (httpx.Limits | None): Connection-pool limits for the
                underlying httpx client. Defaults to
                ``httpx.Limits(max_connections=1024, max_keepalive_connections=512)``
                when None -- see class docstring for why this must exceed
                the HTTP/1.1 worst-case connection demand.
            timeout (httpx.Timeout | None): Default per-request timeout for
                the underlying httpx client. Defaults to
                :data:`_DEFAULT_TIMEOUT` when None.
        """
        self._pytusk_config = pytusk_config
        self._pysui_client = client_factory(pytusk_config.pysui_configuration)
        resolved_limits = limits or httpx.Limits(
            max_connections=1024, max_keepalive_connections=512
        )
        resolved_timeout = timeout or _DEFAULT_TIMEOUT
        self._httpx = httpx.AsyncClient(
            limits=resolved_limits, timeout=resolved_timeout, http2=True
        )

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
        base_url: str | None = None,
    ) -> SuiRpcResult:
        """Execute a WalrusCommand or SuiCommand.

        WalrusCommand instances are dispatched via httpx to the Walrus
        daemon. SuiCommand instances are forwarded to the internal pysui
        client.

        Args:
            command: A WalrusCommand or SuiCommand instance.
            timeout (float | None): Request timeout in seconds. ``None``
                means use the client's configured default (see
                :data:`_DEFAULT_TIMEOUT`), not "no timeout" -- this call
                never forwards a bare ``None`` to httpx at request scope
                (see :meth:`_send`).
            headers (dict[str, str] | None): Additional HTTP headers.
            base_url (str | None): Explicit base URL to dispatch a
                WalrusCommand against, bypassing the command's configured
                aggregator/publisher role resolution. Pass this for
                ``storage_node``-role commands (e.g. PutSliver,
                GetStorageConfirmation), whose target host is per-call data
                resolved from the committee rather than a fixed configured
                endpoint -- dispatch raises ValueError if a storage-node
                command is executed without one. Meaningless for a
                SuiCommand; passing it in that case raises ValueError
                rather than being silently ignored.

        Returns:
            SuiRpcResult: Success result with typed data, or error result
            with result_string set to the error message.

        Raises:
            ValueError: If ``base_url`` is supplied together with a
                SuiCommand, or if ``command`` requires an explicit
                ``base_url`` (storage-node role) and none was supplied, or
                if the publisher role is requested and no publisher URL is
                configured for the active network.
        """
        if isinstance(command, WalrusCommand):
            return await self._dispatch_walrus(
                command, timeout=timeout, headers=headers, base_url=base_url
            )
        if base_url is not None:
            raise ValueError(
                "base_url is meaningless for a SuiCommand; do not pass it."
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

    async def walrus_epoch(self) -> int:
        """Return the current Walrus epoch.

        Supplies this client as the chain reader and the configured staking
        object. A single on-chain read, materially cheaper than
        :meth:`committee`.

        Returns:
            int: Current Walrus epoch.

        Raises:
            RuntimeError: If the on-chain read fails, the staking object
                exposes no dynamic fields, or none carries the supported
                staking inner type.
            TypeError: If the decoded structure is not as expected.
        """
        return await fetch_epoch(
            reader=self, staking_object=self.config.network.staking_object
        )

    async def committee(self) -> WalrusCommittee:
        """Fetch the active Walrus storage committee.

        Supplies this client as the chain reader and the configured staking
        object, so callers need not know where on chain the committee lives.

        Returns:
            WalrusCommittee: The committee for the current epoch.

        Raises:
            KeyError: If a committee member has no entry in the pools table.
            RuntimeError: If an on-chain read fails, or the pools table returns
                a different number of entries than it declares.
            TypeError: If the decoded structures are not as expected.
            ValueError: If the shard assignment is not a complete cover.
        """
        return await fetch_committee(
            reader=self, staking_object=self.config.network.staking_object
        )

    async def __aenter__(self) -> "WalrusClient":  # noqa: PYI034 -- typing.Self is 3.11+ only (project targets >=3.10.6); typing_extensions.Self would add an undeclared dependency, and a bound TypeVar here immediately trips PYI019 instead
        """Open the underlying httpx client.

        A client instance is SINGLE USE. ``__aexit__`` closes both the httpx
        and the pysui client and neither is rebuilt, so re-entering the same
        instance yields closed transports. Build a new client per operation
        scope.
        """
        await self._httpx.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Close the underlying httpx and pysui clients."""
        await self._httpx.__aexit__(exc_type, exc_val, exc_tb)
        await self.pysui_client.close()

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    async def _dispatch_walrus(
        self,
        command: WalrusCommand,
        *,
        timeout: float | httpx.Timeout | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        """Resolve the base URL for a WalrusCommand, then dispatch it.

        Resolution order:
            1. An explicit ``base_url`` argument, if supplied, always wins.
            2. Else, if the command's role is ``storage_node``, raise --
               storage-node commands have no configured endpoint, since
               their target host is per-call data resolved from the
               committee rather than fixed configuration.
            3. Else, if the role is ``publisher``, resolve the network's
               configured publisher URL.
            4. Else, resolve the network's configured aggregator URL.

        Args:
            command (WalrusCommand): Command to dispatch.
            timeout (float | httpx.Timeout | None): Request timeout in
                seconds, or a structured ``httpx.Timeout``. ``None`` means
                use the client's configured default -- see :meth:`execute`.
            headers (dict[str, str] | None): Additional HTTP headers.
            base_url (str | None): Explicit base URL, required for
                storage-node-role commands and optional otherwise.

        Returns:
            SuiRpcResult: Parsed result from command.parse_response().

        Raises:
            ValueError: If the command requires an explicit ``base_url``
                (storage-node role) and none was supplied, or if the
                command requires the publisher role and no publisher URL
                is configured for the active network.
        """
        network = self._pytusk_config.network
        if base_url is not None:
            resolved_base_url = base_url
        elif command.endpoint_role == "storage_node":
            raise ValueError(
                f"{type(command).__name__} has endpoint_role='storage_node' "
                "and requires an explicit base_url; none was supplied."
            )
        elif command.endpoint_role == "publisher":
            resolved_base_url = network.walrus_publisher_url
            if not resolved_base_url:
                raise ValueError(
                    f"No publisher URL configured for network '{network.network_name}'."
                )
        else:
            resolved_base_url = network.walrus_aggregator_url

        return await self._send(
            command=command,
            base_url=resolved_base_url,
            timeout=timeout,
            headers=headers,
        )

    async def _send(
        self,
        *,
        command: WalrusCommand,
        base_url: str,
        timeout: float | httpx.Timeout | None,
        headers: dict[str, str] | None,
    ) -> SuiRpcResult:
        """Build and send the HTTP request for a resolved WalrusCommand.

        Contains the request-building and transport logic shared by every
        dispatch path (aggregator, publisher, and explicit-base_url
        storage-node); the only concern outside this method is deciding
        what ``base_url`` to pass in.

        Args:
            command (WalrusCommand): Command to send.
            base_url (str): Already-resolved base URL to send the request to.
            timeout (float | httpx.Timeout | None): Request timeout in
                seconds, or a structured ``httpx.Timeout``. ``None`` means
                use the client's configured default: httpx treats an
                EXPLICIT ``timeout=None`` passed at request scope as
                "disable timeouts entirely", overriding the client-level
                default rather than inheriting it -- so when ``timeout`` is
                ``None`` here, the ``timeout`` kwarg is OMITTED from the
                ``self._httpx.request(...)`` call entirely, letting the
                client's own default (see :data:`_DEFAULT_TIMEOUT`) apply.
                Never pass ``timeout=None`` to ``request()`` directly.
            headers (dict[str, str] | None): Additional HTTP headers.

        Returns:
            SuiRpcResult: Parsed result from command.parse_response() on a
            completed request. On a transport-level failure (the request
            never produced an HTTP response -- connection refused, DNS
            failure, timeout, ...), the failure message is
            ``"{exception type} on {method} {url}: {detail}"`` rather than
            the bare exception text, because several httpx/httpcore
            transport exceptions (timeouts in particular) stringify to an
            empty string on their own.

            Raw ``ssl.SSLError``/``OSError`` socket errors are converted to
            this same structured failure result too, not just
            ``httpx.HTTPError``. A live 1 GB native upload observed a single
            HTTP/2 stream raise a bare ``ssl.SSLError:
            [SSL: SSLV3_ALERT_BAD_RECORD_MAC]`` -- which is NOT part of the
            ``httpx.HTTPError`` hierarchy -- escape this method entirely and
            propagate up through the whole sliver fan-out, aborting roughly
            300 MB of otherwise-healthy in-flight work over one bad TLS
            connection. A single connection going bad must never be able to
            abort a whole fan-out; every transport failure, TLS or
            otherwise, must come back as a structured failure result the
            caller's retry/quorum logic can act on instead of an escaping
            exception.
        """
        method = command.http_method()
        url = command.url_path(base_url)
        params = command.query_params() or None
        body = command.request_body()
        form = command.form_files()

        # timeout is OMITTED from request_kwargs (rather than passed as
        # timeout=None) when the caller did not supply one, so the client's
        # own default timeout applies -- see the timeout Args entry above.
        request_kwargs: _RequestKwargs = {}
        if timeout is not None:
            request_kwargs["timeout"] = timeout

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
                    headers=headers or {},
                    **request_kwargs,
                )
            elif body is not None:
                response = await self._httpx.request(
                    method=method,
                    url=url,
                    params=params,
                    content=body,
                    headers=headers or {},
                    **request_kwargs,
                )
            else:
                response = await self._httpx.request(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers or {},
                    **request_kwargs,
                )
        except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
            # DIAGNOSTIC NOTE: httpx/httpcore timeout and connection
            # exceptions (ConnectTimeout, ReadTimeout, WriteTimeout,
            # PoolTimeout, ConnectError, ...) frequently stringify to an
            # EMPTY string -- str(exc) == "" -- because the underlying
            # transport raises them with no message text. Relying on
            # str(exc) alone therefore silently produces an empty failure
            # reason for exactly the transport failures (timeouts, refused
            # connections) most likely to occur across a large storage-node
            # fan-out. Always prefix the exception's own type name and the
            # request this failure belongs to, and substitute an explicit
            # placeholder when the underlying message is blank.
            #
            # ssl.SSLError and OSError are caught ALONGSIDE httpx.HTTPError,
            # not merely folded into it, because ssl.SSLError is NOT part of
            # the httpx.HTTPError hierarchy: it is a raw socket/TLS-layer
            # exception (a subclass of OSError) that h2/httpcore can raise
            # directly out of the transport on a bad TLS record, bypassing
            # httpx's own exception wrapping entirely. ssl.SSLError is
            # already a subclass of OSError, so catching OSError alone would
            # suffice -- both are named explicitly here for clarity about
            # which failure this guards against. Neither is a
            # BaseException-only type: asyncio.CancelledError (a
            # BaseException, not an Exception/OSError subclass in Python
            # 3.8+) is NOT caught by this clause and continues to propagate,
            # so a deliberate cancellation of this request is never
            # misreported as a transport failure.
            detail = str(exc) or "<no exception message>"
            return SuiRpcResult(
                False, f"{type(exc).__name__} on {method} {url}: {detail}"
            )

        return command.parse_response(response)


async def get_walrus_epoch(*, client: WalrusClient) -> int:
    """Return the current Walrus epoch from the StakingInnerV1 dynamic field.

    Retained as the exported free-function form. New code should prefer
    :meth:`WalrusClient.walrus_epoch`, which this delegates to; both issue the
    same single read.

    Args:
        client (WalrusClient): Active pytusk client.

    Returns:
        int: Current Walrus epoch.

    Raises:
        RuntimeError: If the staking dynamic fields cannot be fetched, the
            staking object exposes none, or no field carries the supported
            staking inner type.
        TypeError: If the decoded StakingInnerV1 does not have the expected shape.
    """
    return await client.walrus_epoch()
