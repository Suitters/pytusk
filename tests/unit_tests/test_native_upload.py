#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for ``pytusk.core.native_upload``.

Scope, deliberately narrow: :func:`~pytusk.core.native_upload.upload_slivers`
is the only stage fully exercisable without a live node -- it talks to
storage nodes over a fake ``WalrusClient.execute``, and this module builds
real ``WalrusCommittee``/``EncodedBlob`` fixtures (via a genuine
:func:`~pytusk.core.encoding.encode_blob` call) rather than hand-rolling
``RedstuffSliverPair`` fakes, since that PyO3 type has no Python-visible
constructor.

``collect_confirmations`` is exercised for its happy path and its
no-confirmations failure using placeholder keypairs/signatures with
``bls_aggregate``/``bls_aggregate_verify`` mocked (see
``test_certification.py`` for why: only 3 real BLS keypairs are available
to the test suite, and these tests need more simultaneous distinct
signers than that) -- faked crypto, faked transport.
``certify`` and ``store_blob_native`` are NOT exercised end-to-end against
real PTBs here: both submit real transactions via
``pytusk.core.system_ops.execute_certify`` / ``execute_reserve_and_register``,
which need either a live node or an elaborate ``AsyncSuiTransaction`` mock
that would fake coverage rather than prove behaviour (matching the scope
note already recorded in ``test_system_ops.py``). Full PTB
composition/execution for both remains deferred to integration tests.

``TestCertifyTx2Failure`` is a narrow exception to that boundary: it
monkeypatches ``execute_certify``/``fetch_epoch`` (``certify()``'s only
network calls) and, for the ``store_blob_native`` case, also
``resolve_package_id``/``execute_reserve_and_register``/``upload_slivers``/
``collect_confirmations``, so the REAL ``certify()`` and ``store_blob_native``
orchestration code runs -- only the PTB-submission boundary is faked, not the
exception-conversion or timing-threading logic under test.
"""

import asyncio
import dataclasses
from collections.abc import Coroutine, Mapping
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from pysui import SuiRpcResult

from pytusk.client.walrus_client import WalrusClient
from pytusk.commands.node_commands import (
    GetStorageConfirmation,
    PutMetadata,
    PutSliver,
    SignedConfirmation,
)
from pytusk.core import native_upload
from pytusk.core.certification import (
    Certificate,
    confirmation_message,
    min_weight_for_quorum,
)
from pytusk.core.committee import WalrusCommittee, WalrusCommitteeMember
from pytusk.core.encoding import encode_blob
from pytusk.core.native_upload import (
    CertifyTransactionError,
    ConfirmationCollectionError,
    FanoutReport,
    NativeBlobReceipt,
    NodeUploadOutcome,
    SliverUploadError,
    StageTimings,
    _BytesInFlightThrottle,
    _ConfirmProgress,
    _FanoutProgress,
    _upload_node,
    certify,
    collect_confirmations,
    object_id_to_raw_bytes,
    store_blob_native,
    upload_slivers,
)
from pytusk.core.system_ops import Registration

_BLOB_DATA = b"pytusk native upload deliverable 5 test payload"
_N_SHARDS = 7

# The blob-ID-dependent rotation offset depends on content; this payload is
# confirmed (against n_shards=7) to rotate every shard's sliver_pair_index
# away from its shard index, which the routing test below relies on.
_ROTATED_BLOB_DATA = b"hello world native upload test"


def _public_key(seed: int) -> bytes:
    """A distinct, well-formed-length (96 byte) placeholder public key."""
    return bytes([seed % 256]) * 96


def _signature(seed: int) -> bytes:
    """A distinct, well-formed-length (96 byte) placeholder signature.

    ``bls_aggregate``/``bls_aggregate_verify`` are mocked in every test that
    consumes these placeholders (only 3 real BLS keypairs are available to
    the test suite -- see ``test_certification.py`` -- and these tests need
    up to 7 simultaneous distinct signers), so the bytes need only be
    well-formed-length, not a genuine BLS signature.
    """
    return bytes([(seed + 1) % 256]) * 96


def _one_shard_per_node_committee() -> WalrusCommittee:
    """7 shards, 7 nodes, one shard each -- node k always holds shard k.

    Chosen so that ``member_for_shard(shard_index=i)`` and
    ``member_for_shard(shard_index=pair.sliver_pair_index)`` resolve to
    DIFFERENT nodes whenever rotation moves shard ``i``'s pair away from
    URL index ``i`` -- see ``test_encoding.py``/the module docstring on
    ``EncodedBlob`` for why that rotation exists.
    """
    members = tuple(
        WalrusCommitteeMember(
            node_id=f"node{k}",
            shard_indices=(k,),
            network_address=f"node{k}.example:443",
            public_key=_public_key(k),
        )
        for k in range(_N_SHARDS)
    )
    return WalrusCommittee(epoch=1, n_shards=_N_SHARDS, members=members)


def _multi_shard_committee() -> WalrusCommittee:
    """4 nodes over 7 shards: node0 holds 3 shards, the rest hold fewer.

    Weights: node0=3, node1=1, node2=1, node3=2 (sums to 7).
    ``min_weight_for_quorum(n_shards=7) == 5``.
    """
    assignment = {
        "node0": (0, 1, 2),
        "node1": (3,),
        "node2": (4,),
        "node3": (5, 6),
    }
    members = tuple(
        WalrusCommitteeMember(
            node_id=node_id,
            shard_indices=shards,
            network_address=f"{node_id}.example:443",
            public_key=_public_key(index),
        )
        for index, (node_id, shards) in enumerate(assignment.items())
    )
    return WalrusCommittee(epoch=1, n_shards=_N_SHARDS, members=members)


class _FakeStorageClient:
    """Fake ``WalrusClient``-shaped object for storage-node dispatch.

    Implements only ``execute(command=..., base_url=...)``, the subset the
    native_upload stage functions use. ``put_failures`` maps
    ``(base_url, sliver_type)`` to a failure reason string; anything not
    listed succeeds. ``metadata_failures`` maps ``base_url`` to a
    ``PutMetadata`` failure reason string; anything not listed succeeds --
    this defaults every node's metadata PUT to success so existing
    sliver-fan-out tests (written before the metadata stage existed) are
    unaffected. ``confirmations`` maps ``base_url`` to a canned
    ``SignedConfirmation`` (or ``None`` to fail that node's confirmation
    request). Every dispatched command is recorded in ``calls`` for
    assertions.
    """

    def __init__(
        self,
        *,
        put_failures: dict[tuple[str, str], str] | None = None,
        metadata_failures: dict[str, str] | None = None,
        confirmations: Mapping[str, SignedConfirmation | None] | None = None,
    ) -> None:
        self.put_failures = put_failures or {}
        self.metadata_failures = metadata_failures or {}
        self.confirmations = confirmations or {}
        self.calls: list[tuple[str | None, object]] = []

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        self.calls.append((base_url, command))
        assert base_url is not None
        if isinstance(command, PutMetadata):
            reason = self.metadata_failures.get(base_url)
            if reason is not None:
                return SuiRpcResult(False, reason)
            return SuiRpcResult(True, "")
        if isinstance(command, PutSliver):
            reason = self.put_failures.get((base_url, command.sliver_type))
            if reason is not None:
                return SuiRpcResult(False, reason)
            return SuiRpcResult(True, "")
        if isinstance(command, GetStorageConfirmation):
            confirmation = self.confirmations.get(base_url)
            if confirmation is None:
                return SuiRpcResult(False, f"no confirmation configured for {base_url}")
            return SuiRpcResult(True, "", confirmation)
        raise NotImplementedError(f"Unhandled command type: {type(command)}")


class _FlakyPutClient:
    """Fake client whose ``PutSliver`` dispatch fails a configurable number
    of times per ``(base_url, sliver_type)`` before succeeding.

    ``fail_first`` maps ``(base_url, sliver_type)`` to how many leading
    attempts against that key should fail before the next attempt (and
    every attempt thereafter) succeeds; a key absent from ``fail_first``
    always succeeds. ``attempts`` records how many times each key was
    dispatched, for asserting retry counts. Every ``PutMetadata`` dispatch
    unconditionally succeeds -- this fake only models sliver-PUT flakiness.
    """

    def __init__(self, *, fail_first: dict[tuple[str, str], int]) -> None:
        self.fail_first = dict(fail_first)
        self.attempts: dict[tuple[str, str], int] = {}
        self.calls: list[tuple[str | None, object]] = []

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        self.calls.append((base_url, command))
        assert base_url is not None
        if isinstance(command, PutMetadata):
            return SuiRpcResult(True, "")
        if not isinstance(command, PutSliver):
            raise NotImplementedError(f"Unhandled command type: {type(command)}")
        key = (base_url, command.sliver_type)
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.attempts[key] <= self.fail_first.get(key, 0):
            return SuiRpcResult(False, "transient")
        return SuiRpcResult(True, "")


class _RaisingPutClient:
    """Fake client whose ``PutSliver`` dispatch RAISES a bare exception for
    one configured ``(base_url, sliver_type)`` key, instead of returning a
    failed ``SuiRpcResult`` -- models an exception escaping
    ``client.execute`` itself (e.g. a bug in the transport layer, or --
    before FIX 1 -- a raw ``ssl.SSLError`` that used to escape ``_send``).
    Every other dispatch (``PutMetadata``, and any ``PutSliver`` not
    matching ``raise_for``) succeeds unconditionally.
    """

    def __init__(self, *, raise_for: tuple[str, str], exc: BaseException) -> None:
        self.raise_for = raise_for
        self.exc = exc
        self.calls: list[tuple[str | None, object]] = []

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        self.calls.append((base_url, command))
        if isinstance(command, PutMetadata):
            return SuiRpcResult(True, "")
        if not isinstance(command, PutSliver):
            raise NotImplementedError(f"Unhandled command type: {type(command)}")
        if (base_url, command.sliver_type) == self.raise_for:
            raise self.exc
        return SuiRpcResult(True, "")


class _RaisingMetadataClient:
    """Fake client whose ``PutMetadata`` dispatch RAISES a bare exception
    for one configured ``base_url``, instead of returning a failed
    ``SuiRpcResult`` -- models an exception escaping ``_upload_node``'s own
    task ENTIRELY: neither ``_put_metadata`` nor ``_upload_node`` wraps the
    metadata-PUT call in a ``try``/``except``, so this is exactly the case
    ``_outcome_from_task`` exists to convert into a synthesized failed
    ``NodeUploadOutcome`` rather than letting it abort the whole fan-out.
    Every other node's dispatch succeeds unconditionally.
    """

    def __init__(self, *, raise_for_base_url: str, exc: BaseException) -> None:
        self.raise_for_base_url = raise_for_base_url
        self.exc = exc
        self.calls: list[tuple[str | None, object]] = []

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        self.calls.append((base_url, command))
        if isinstance(command, PutMetadata):
            if base_url == self.raise_for_base_url:
                raise self.exc
            return SuiRpcResult(True, "")
        if isinstance(command, PutSliver):
            return SuiRpcResult(True, "")
        raise NotImplementedError(f"Unhandled command type: {type(command)}")


class _HangingSliverPutClient:
    """Fake client whose ``PutSliver`` dispatch for one configured
    ``(base_url, sliver_type)`` key hangs forever -- awaits an
    ``asyncio.Event`` that is never set -- while ``PutMetadata`` and every
    other sliver dispatch succeed immediately. Models a node whose OWN
    ``_upload_node`` task must itself be cancelled from outside (
    ``upload_slivers``'s grace-window straggler cleanup) while it still has
    an orphaned sliver-PUT task of its own pending, exercising BOTH levels
    of FIX 3's cleanup: ``_upload_node``'s own ``finally`` and
    ``upload_slivers``'s.
    """

    def __init__(self, *, hang_for: tuple[str, str]) -> None:
        self.hang_for = hang_for
        self.calls: list[tuple[str | None, object]] = []
        self._hang_event = asyncio.Event()

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        self.calls.append((base_url, command))
        if isinstance(command, PutMetadata):
            return SuiRpcResult(True, "")
        if not isinstance(command, PutSliver):
            raise NotImplementedError(f"Unhandled command type: {type(command)}")
        if (base_url, command.sliver_type) == self.hang_for:
            await self._hang_event.wait()  # never set; cancelled by the caller
        return SuiRpcResult(True, "")


class _HangingConfirmationClient:
    """Fake client whose ``GetStorageConfirmation`` dispatch for a configured
    subset of nodes hangs forever -- awaits an ``asyncio.Event`` that is
    never set -- while every other configured node responds immediately
    with a canned ``SignedConfirmation``. Models slow/dead storage nodes so
    that ``collect_confirmations``'s quorum early-exit genuinely has
    stragglers still in flight when quorum weight is reached.

    ``_FakeStorageClient`` cannot exercise the early-exit branch: every one
    of its dispatches resolves immediately (no real ``await`` suspension),
    so every candidate task completes within the very first
    ``asyncio.wait`` tick and the ``if weight_confirmed >= required_weight:
    break`` branch, while technically reached, never actually skips
    anything still outstanding. This fake forces a genuine still-pending
    task at the point quorum is reached.

    ``hang_for`` is the set of base URLs whose dispatch hangs permanently.
    ``responded`` counts, per base URL, how many times a dispatch for that
    node actually RETURNED a result -- incremented only after crossing the
    hang point, so a node in ``hang_for`` (whose gate is never released)
    has a permanent count of 0. Tests use this counter, not wall-clock
    timing, to prove a hanging node's confirmation genuinely never
    completed.
    """

    def __init__(
        self,
        *,
        confirmations: Mapping[str, SignedConfirmation | None],
        hang_for: frozenset[str],
    ) -> None:
        self.confirmations = confirmations
        self.hang_for = hang_for
        self.calls: list[tuple[str | None, object]] = []
        self.responded: dict[str, int] = {}
        self._hang_event = asyncio.Event()

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        self.calls.append((base_url, command))
        assert base_url is not None
        if not isinstance(command, GetStorageConfirmation):
            raise NotImplementedError(f"Unhandled command type: {type(command)}")
        if base_url in self.hang_for:
            await self._hang_event.wait()  # never set; cancelled by the caller
        confirmation = self.confirmations.get(base_url)
        self.responded[base_url] = self.responded.get(base_url, 0) + 1
        if confirmation is None:
            return SuiRpcResult(False, f"no confirmation configured for {base_url}")
        return SuiRpcResult(True, "", confirmation)


class _GatedStragglerConfirmationClient:
    """Fake client for a DETERMINISTIC 'grace window admits a late-but-quick
    straggler' scenario.

    Every base URL in ``quorum_base_urls`` responds immediately with its
    canned ``SignedConfirmation``. ``straggler_base_url`` instead awaits an
    internal ``asyncio.Event`` that THIS FAKE releases itself, synchronously,
    the moment every ``quorum_base_urls`` member has responded -- not after
    a fixed sleep or any other wall-clock delay. This makes the straggler's
    completion deterministic relative to the quorum-reaching batch: per
    ``asyncio.wait``'s own implementation (it recomputes ``done``/``pending``
    from every future's ``.done()`` state once its internal waiter fires,
    rather than returning only the single future that triggered it), the
    straggler is released and reschedules while
    ``collect_confirmations`` is still unwinding the batch that reached
    quorum, and it resolves on one of the very next event-loop iterations --
    always well inside any positive grace window, however small. No sleep
    or timing assertion is used anywhere in this fake or in the tests that
    use it.
    """

    def __init__(
        self,
        *,
        confirmations: dict[str, SignedConfirmation],
        quorum_base_urls: frozenset[str],
        straggler_base_url: str,
    ) -> None:
        self.confirmations = confirmations
        self.quorum_base_urls = quorum_base_urls
        self.straggler_base_url = straggler_base_url
        self.calls: list[tuple[str | None, object]] = []
        self._quorum_responded: set[str] = set()
        self._release_event = asyncio.Event()

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        self.calls.append((base_url, command))
        assert base_url is not None
        if not isinstance(command, GetStorageConfirmation):
            raise NotImplementedError(f"Unhandled command type: {type(command)}")
        if base_url == self.straggler_base_url:
            await self._release_event.wait()
        confirmation = self.confirmations[base_url]
        if base_url in self.quorum_base_urls:
            self._quorum_responded.add(base_url)
            if self._quorum_responded == self.quorum_base_urls:
                self._release_event.set()
        return SuiRpcResult(True, "", confirmation)


class TestUploadSliversWeightPerNode:
    """Weight is counted per NODE, not per shard."""

    async def test_multi_shard_node_weight_counted_once(self) -> None:
        """node0 holds 3 shards; a full success reports its weight as 3,
        via exactly one outcome -- not 3 separate outcomes summing to more
        than the node's own shard count."""
        committee = _multi_shard_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        client = _FakeStorageClient()

        report = await upload_slivers(client=cast(WalrusClient, client), committee=committee, encoded=encoded)

        assert isinstance(report, FanoutReport)
        assert len(report.outcomes) == 4  # one per node, not one per shard
        node0_outcome = next(o for o in report.outcomes if o.node_id == "node0")
        assert node0_outcome.succeeded is True
        assert node0_outcome.weight == 3
        assert report.weight_succeeded == 7  # 3 + 1 + 1 + 2, each counted once


class TestUploadSliversQuorum:
    """Fan-out quorum success/failure and the SliverUploadError message."""

    async def test_quorum_reached_when_minority_fails(self) -> None:
        """Failing the smallest node (weight 1) still leaves weight 6 >= the
        required 5, so the call succeeds despite a failure."""
        committee = _multi_shard_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        client = _FakeStorageClient(
            put_failures={("https://node1.example:443", "primary"): "boom"}
        )

        # Zero backoff: the fake's failure is permanent, so retries would
        # otherwise run to max_retries with real (if tiny) exponential
        # sleeps; this test only cares about the final outcome.
        report = await upload_slivers(
            client=cast(WalrusClient, client),
            committee=committee,
            encoded=encoded,
            retry_min_backoff=0.0,
            retry_max_backoff=0.0,
        )

        assert report.weight_succeeded == 6
        failed = next(o for o in report.outcomes if o.node_id == "node1")
        assert failed.succeeded is False

    async def test_quorum_not_reached_raises_with_weight_details(self) -> None:
        """Failing node0 (weight 3) drops achieved weight to 4, below the
        required 5; the raised error names both figures plus n_shards and a
        per-reason failure count."""
        committee = _multi_shard_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        client = _FakeStorageClient(
            put_failures={("https://node0.example:443", "primary"): "boom"}
        )

        with pytest.raises(SliverUploadError) as excinfo:
            await upload_slivers(
                client=cast(WalrusClient, client),
                committee=committee,
                encoded=encoded,
                retry_min_backoff=0.0,
                retry_max_backoff=0.0,
            )

        message = str(excinfo.value)
        assert "4" in message  # achieved weight
        assert "5" in message  # required weight
        assert "n_shards=7" in message
        assert "boom" in message
        assert excinfo.value.stage == "upload_slivers"


class TestUploadSliversRouting:
    """PUT routing uses sliver_pair_index for the path and shard-index-based
    member resolution for the host -- the two must not be conflated."""

    async def test_put_uses_pair_index_and_shard_indexed_host(self) -> None:
        """For a shard where rotation actually moves the sliver-pair index
        away from the shard index, the target host must still be
        ``member_for_shard(shard_index=<shard index>)``, while the URL path
        value must be the pair's own ``sliver_pair_index`` -- NOT the raw
        shard index, and the shard index must NOT be used to pick the host
        either."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_ROTATED_BLOB_DATA, n_shards=committee.n_shards)
        # The blob-ID-dependent rotation offset varies by content; find a
        # shard where it actually moves the sliver-pair index, rather than
        # assuming shard 0 specifically does. A plain generator + next()
        # would raise a bare StopIteration inside this coroutine if none
        # matched (PEP 479 turns that into a confusing RuntimeError), so
        # this loops explicitly and fails with a clear message instead.
        shard_index = None
        for i, pair in enumerate(encoded.slivers):
            if pair.sliver_pair_index != i:
                shard_index = i
                break
        assert shard_index is not None, (
            "fixture data no longer rotates any shard for n_shards=7 -- "
            "pick different _ROTATED_BLOB_DATA"
        )
        pair0 = encoded.slivers[shard_index]

        wrong_host = committee.member_for_shard(shard_index=pair0.sliver_pair_index).base_url
        correct_host = committee.member_for_shard(shard_index=shard_index).base_url
        assert wrong_host != correct_host

        client = _FakeStorageClient()
        await upload_slivers(client=cast(WalrusClient, client), committee=committee, encoded=encoded)

        put_calls_to_correct_host = [
            command
            for base_url, command in client.calls
            if isinstance(command, PutSliver) and base_url == correct_host
        ]
        assert put_calls_to_correct_host, "the shard's PUTs never reached its own node"
        for command in put_calls_to_correct_host:
            assert command.sliver_pair_index == pair0.sliver_pair_index

        put_calls_to_wrong_host = [
            command
            for base_url, command in client.calls
            if isinstance(command, PutSliver)
            and base_url == wrong_host
            and command.sliver_pair_index == pair0.sliver_pair_index
        ]
        assert not put_calls_to_wrong_host, (
            "shard 0's sliver_pair_index-indexed PUT leaked to the node "
            "selected by treating sliver_pair_index as a shard index"
        )


class TestUploadSliversBothSliversRequired:
    """A node succeeds only when BOTH its primary and secondary stored."""

    async def test_secondary_failure_fails_the_node(self) -> None:
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        client = _FakeStorageClient(
            put_failures={("https://node0.example:443", "secondary"): "boom"}
        )

        report = await upload_slivers(
            client=cast(WalrusClient, client),
            committee=committee,
            encoded=encoded,
            retry_min_backoff=0.0,
            retry_max_backoff=0.0,
        )

        node0_outcome = next(o for o in report.outcomes if o.node_id == "node0")
        assert node0_outcome.succeeded is False
        assert node0_outcome.reason is not None
        assert "boom" in node0_outcome.reason

    async def test_primary_only_success_still_fails_the_node(self) -> None:
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        client = _FakeStorageClient(
            put_failures={("https://node3.example:443", "secondary"): "nope"}
        )

        report = await upload_slivers(
            client=cast(WalrusClient, client),
            committee=committee,
            encoded=encoded,
            retry_min_backoff=0.0,
            retry_max_backoff=0.0,
        )

        node3_outcome = next(o for o in report.outcomes if o.node_id == "node3")
        assert node3_outcome.succeeded is False
        # Every other node had both slivers succeed.
        others = [o for o in report.outcomes if o.node_id != "node3"]
        assert all(o.succeeded for o in others)


class TestUploadNodeMetadataStage:
    """The metadata stage runs before any sliver PUT, and short-circuits the
    node -- no sliver PUT is attempted -- when the metadata PUT fails after
    retries are exhausted."""

    async def test_metadata_put_precedes_every_sliver_put(self) -> None:
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        member = committee.members[0]
        client = _FakeStorageClient()
        progress = _FanoutProgress(total_nodes=1, required_weight=1)

        outcome = await _upload_node(
            client=cast(WalrusClient, client),
            committee=committee,
            encoded=encoded,
            member=member,
            bytes_throttle=_BytesInFlightThrottle(max_bytes=10_000_000),
            max_node_connections=10,
            retry_min_backoff=0.0,
            retry_max_backoff=0.0,
            max_retries=0,
            progress=progress,
            global_write_semaphore=asyncio.Semaphore(10),
        )

        assert outcome.succeeded is True
        node_calls = [
            command for base_url, command in client.calls if base_url == member.base_url
        ]
        assert node_calls, "no calls recorded for this node"
        assert isinstance(node_calls[0], PutMetadata)
        assert node_calls[0].blob_id == encoded.blob_id
        assert node_calls[0].metadata_bcs == encoded.metadata_bcs
        assert all(isinstance(command, PutSliver) for command in node_calls[1:])
        # One metadata PUT, then primary+secondary per shard the node holds.
        assert len(node_calls) == 1 + 2 * len(member.shard_indices)

    async def test_metadata_failure_short_circuits_node_with_no_sliver_puts(
        self,
    ) -> None:
        committee = _one_shard_per_node_committee()
        member = committee.members[0]
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        client = _FakeStorageClient(
            metadata_failures={member.base_url: "METADATA_NOT_FOUND"}
        )
        progress = _FanoutProgress(total_nodes=1, required_weight=1)

        outcome = await _upload_node(
            client=cast(WalrusClient, client),
            committee=committee,
            encoded=encoded,
            member=member,
            bytes_throttle=_BytesInFlightThrottle(max_bytes=10_000_000),
            max_node_connections=10,
            retry_min_backoff=0.0,
            retry_max_backoff=0.0,
            max_retries=1,
            progress=progress,
            global_write_semaphore=asyncio.Semaphore(10),
        )

        assert outcome.succeeded is False
        assert outcome.reason is not None
        assert outcome.reason.startswith("metadata: ")
        assert "METADATA_NOT_FOUND" in outcome.reason
        node_calls = [
            command for base_url, command in client.calls if base_url == member.base_url
        ]
        assert node_calls
        assert all(isinstance(command, PutMetadata) for command in node_calls)
        assert not any(isinstance(command, PutSliver) for command in node_calls)
        # max_retries=1 => initial attempt + 1 retry = 2 dispatches.
        assert len(node_calls) == 2


class TestDataclassContract:
    """Dataclass immutability and the absence of storage_object_id."""

    def test_node_upload_outcome_is_frozen(self) -> None:
        outcome = NodeUploadOutcome(
            node_id="n", position=0, weight=1, succeeded=True, reason=None
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.succeeded = False  # type: ignore[misc]

    def test_fanout_report_is_frozen(self) -> None:
        report = FanoutReport(outcomes=(), weight_succeeded=0, n_shards=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            report.n_shards = 2  # type: ignore[misc]

    def test_native_blob_receipt_is_frozen(self) -> None:
        receipt = NativeBlobReceipt(
            blob_id="abc",
            object_id="0xabc",
            certified=True,
            end_epoch=10,
            failed_stage=None,
            timings=StageTimings(
                encode=None,
                register_tx1=None,
                sliver_upload=None,
                confirmations=None,
                certify_tx2=None,
                total=None,
            ),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            receipt.certified = False  # type: ignore[misc]

    def test_native_blob_receipt_has_no_storage_object_id_field(self) -> None:
        field_names = {f.name for f in dataclasses.fields(NativeBlobReceipt)}
        assert "storage_object_id" not in field_names
        assert field_names == {
            "blob_id",
            "object_id",
            "certified",
            "end_epoch",
            "failed_stage",
            "timings",
        }

    def test_stage_timings_is_frozen(self) -> None:
        timings = StageTimings(
            encode=1.0,
            register_tx1=2.0,
            sliver_upload=None,
            confirmations=None,
            certify_tx2=None,
            total=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            timings.encode = 5.0  # type: ignore[misc]

    def test_fanout_report_succeeded_positions(self) -> None:
        outcomes = (
            NodeUploadOutcome(node_id="a", position=0, weight=1, succeeded=True, reason=None),
            NodeUploadOutcome(
                node_id="b", position=1, weight=1, succeeded=False, reason="x"
            ),
            NodeUploadOutcome(node_id="c", position=2, weight=1, succeeded=True, reason=None),
        )
        report = FanoutReport(outcomes=outcomes, weight_succeeded=2, n_shards=3)
        assert report.succeeded_positions == (0, 2)


class TestObjectIdToRawBytes:
    """0x-prefixed Sui object ID <-> 32 raw bytes conversion."""

    def test_known_object_id_converts_to_expected_bytes(self) -> None:
        object_id = "0x" + "ab" * 32
        result = object_id_to_raw_bytes(object_id=object_id)
        assert result == bytes([0xAB] * 32)
        assert len(result) == 32

    def test_uppercase_prefix_accepted(self) -> None:
        object_id = "0X" + "cd" * 32
        result = object_id_to_raw_bytes(object_id=object_id)
        assert result == bytes([0xCD] * 32)

    def test_malformed_hex_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            object_id_to_raw_bytes(object_id="0x" + "zz" * 32)

    def test_wrong_length_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            object_id_to_raw_bytes(object_id="0x" + "ab" * 10)

    def test_empty_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            object_id_to_raw_bytes(object_id="")


def _registration(*, blob_id: bytes, deletable: bool = False) -> Registration:
    """A real Registration fixture; only object_id/deletable are read
    by collect_confirmations, the rest are filled with placeholders."""
    return Registration(
        object_id="0xblob",
        blob_id=blob_id,
        end_epoch=99,
        deletable=deletable,
        digest="fake-digest",
    )


def _committee_with_signed_confirmations(
    *, committee: WalrusCommittee, blob_id: bytes
) -> tuple[WalrusCommittee, dict[str, SignedConfirmation], bytes]:
    """Build placeholder keypairs/signatures for every member of
    ``committee`` over the expected confirmation message for ``blob_id``.

    Factors out the placeholder-keypair setup duplicated across
    ``TestCollectConfirmations``/``TestCollectConfirmationsQueriesAll``/
    ``TestCollectConfirmationsTolerance`` above (kept as-is, unmodified) so
    new tests exercising the quorum early-exit path do not have to repeat
    it a fourth+ time. ``bls_aggregate``/``bls_aggregate_verify`` are mocked
    by every caller of this helper (only 3 real BLS keypairs are available
    to the test suite -- see ``test_certification.py`` -- and these tests
    need up to 7 simultaneous distinct signers), so the public keys and
    signatures here are well-formed-length placeholders, not genuine BLS
    material.

    Args:
        committee (WalrusCommittee): The committee to sign confirmations
            for; NOT mutated -- a replacement committee carrying the
            placeholder public keys is returned instead.
        blob_id (bytes): Raw 32-byte blob ID the confirmation message is
            built for (non-deletable/permanent blob message).

    Returns:
        tuple[WalrusCommittee, dict[str, SignedConfirmation], bytes]: A new
        committee with placeholder public keys swapped in, a
        ``base_url -> SignedConfirmation`` map covering every member, and
        the signed message itself. Callers wanting a subset of nodes to
        hang, fail, or respond late simply drop or override entries in the
        returned map before constructing their fake client.
    """
    message = confirmation_message(epoch=committee.epoch, blob_id=blob_id)
    confirmations: dict[str, SignedConfirmation] = {}
    keys_by_node: dict[str, bytes] = {}
    for index, member in enumerate(committee.members):
        keys_by_node[member.node_id] = _public_key(index)
        confirmations[member.base_url] = SignedConfirmation(
            serialized_message=message, signature=_signature(index)
        )
    members_with_real_keys = tuple(
        dataclasses.replace(member, public_key=keys_by_node[member.node_id])
        for member in committee.members
    )
    signed_committee = dataclasses.replace(committee, members=members_with_real_keys)
    return signed_committee, confirmations, message


class TestCollectConfirmations:
    """Confirmation collection against a permanent (non-deletable) blob,
    using genuine BLS keypairs/signatures over a faked transport -- the
    same pattern as ``test_certification.py``."""

    async def test_happy_path_builds_certificate(self) -> None:
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        registration = _registration(blob_id=encoded.blob_id)
        message = confirmation_message(epoch=committee.epoch, blob_id=encoded.blob_id)
        confirmations: dict[str, SignedConfirmation | None] = {}
        keys_by_node: dict[str, bytes] = {}
        for index, member in enumerate(committee.members):
            keys_by_node[member.node_id] = _public_key(index)
            confirmations[member.base_url] = SignedConfirmation(
                serialized_message=message, signature=_signature(index)
            )
        # Committee members must carry the SAME keys used to sign, or
        # build_certificate's verification step fails.
        members_with_real_keys = tuple(
            dataclasses.replace(member, public_key=keys_by_node[member.node_id])
            for member in committee.members
        )
        committee = dataclasses.replace(committee, members=members_with_real_keys)

        client = _FakeStorageClient(confirmations=confirmations)
        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
        ):
            certificate = await collect_confirmations(
                client=cast(WalrusClient, client), committee=committee, blob_id=encoded.blob_id, registration=registration
            )
        assert certificate.serialized_message == message
        assert certificate.weight == committee.n_shards

    async def test_no_confirmations_raises_collection_error(self) -> None:
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        registration = _registration(blob_id=encoded.blob_id)
        client = _FakeStorageClient(confirmations={})

        with pytest.raises(ConfirmationCollectionError) as excinfo:
            await collect_confirmations(
                client=cast(WalrusClient, client), committee=committee, blob_id=encoded.blob_id, registration=registration
            )
        assert excinfo.value.stage == "collect_confirmations"


class TestUploadSliversRetry:
    """Per-sliver retry with exponential backoff, and node abandonment once
    retries are exhausted -- upstream's ``ExponentialBackoffConfig`` /
    ``store_pairs`` policy."""

    async def test_transient_failure_is_retried_and_node_still_counts(self) -> None:
        """A sliver PUT that fails twice then succeeds is retried in place;
        the node's full weight is still credited once it eventually
        succeeds."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        key = ("https://node0.example:443", "primary")
        client = _FlakyPutClient(fail_first={key: 2})

        report = await upload_slivers(
            client=cast(WalrusClient, client),
            committee=committee,
            encoded=encoded,
            retry_min_backoff=0.001,
            retry_max_backoff=0.01,
            max_retries=5,
        )

        assert report.weight_succeeded == committee.n_shards
        node0_outcome = next(o for o in report.outcomes if o.node_id == "node0")
        assert node0_outcome.succeeded is True
        # 2 failures + 1 succeeding attempt.
        assert client.attempts[key] == 3

    async def test_exhausted_retries_abandons_node_without_weight(self) -> None:
        """A sliver PUT that fails on every attempt exhausts its retries and
        the whole node is abandoned -- its weight is not credited -- while
        the rest of the committee still reaches quorum."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        key = ("https://node0.example:443", "primary")
        client = _FlakyPutClient(fail_first={key: 999})

        report = await upload_slivers(
            client=cast(WalrusClient, client),
            committee=committee,
            encoded=encoded,
            retry_min_backoff=0.001,
            retry_max_backoff=0.01,
            max_retries=2,
        )

        node0_outcome = next(o for o in report.outcomes if o.node_id == "node0")
        assert node0_outcome.succeeded is False
        assert report.weight_succeeded == committee.n_shards - node0_outcome.weight
        # 1 initial attempt + 2 retries, then abandoned.
        assert client.attempts[key] == 3


class TestUploadNodeSliverTaskException:
    """FIX 2 (sliver level): a sliver PUT task that RAISES instead of
    returning its ``(succeeded, reason)`` tuple must not propagate out of
    ``_upload_node`` -- it is treated as a failed PUT, and the node still
    returns a ``NodeUploadOutcome`` carrying the exception's type and
    message in ``reason``."""

    async def test_raising_sliver_task_yields_failed_outcome_not_exception(
        self,
    ) -> None:
        """One sliver PUT (node0's primary) raises a bare ``RuntimeError``
        instead of returning; ``_upload_node`` must still return a failed
        ``NodeUploadOutcome`` -- not let the exception propagate out of this
        call -- with the exception's type and message in ``reason``."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        member = committee.members[0]
        client = _RaisingPutClient(
            raise_for=(member.base_url, "primary"),
            exc=RuntimeError("unexpected transport failure"),
        )
        progress = _FanoutProgress(total_nodes=1, required_weight=1)

        outcome = await _upload_node(
            client=cast(WalrusClient, client),
            committee=committee,
            encoded=encoded,
            member=member,
            bytes_throttle=_BytesInFlightThrottle(max_bytes=10_000_000),
            max_node_connections=10,
            retry_min_backoff=0.0,
            retry_max_backoff=0.0,
            max_retries=0,
            progress=progress,
            global_write_semaphore=asyncio.Semaphore(10),
        )

        assert isinstance(outcome, NodeUploadOutcome)
        assert outcome.succeeded is False
        assert outcome.reason is not None
        assert "RuntimeError" in outcome.reason
        assert "unexpected transport failure" in outcome.reason


class TestUploadSliversNodeTaskException:
    """FIX 2 (node level) -- the single most important robustness test: one
    node's ``_upload_node`` TASK raising an unhandled exception must not
    abort ``upload_slivers`` for the rest of the fan-out. Here the exception
    is a ``PutMetadata`` dispatch that escapes both ``_put_metadata`` and
    ``_upload_node`` entirely, uncaught, since neither wraps that call in a
    ``try``/``except``. ``_outcome_from_task`` converts the raised task into
    a synthesized failed ``NodeUploadOutcome``, and ``upload_slivers`` still
    reaches quorum from the other nodes."""

    async def test_one_raising_node_task_does_not_abort_fanout(self) -> None:
        """node0's metadata PUT raises entirely (weight 1 lost); the other
        6 of 7 one-shard-per-node members (weight 6) still meet the
        required weight of 5, so ``upload_slivers`` must complete
        successfully -- not propagate node0's exception -- and report
        node0 as a failed outcome carrying the exception's type and
        message."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        node0 = committee.members[0]
        client = _RaisingMetadataClient(
            raise_for_base_url=node0.base_url,
            exc=RuntimeError("unexpected metadata dispatch failure"),
        )

        report = await upload_slivers(client=cast(WalrusClient, client), committee=committee, encoded=encoded)

        assert isinstance(report, FanoutReport)
        assert report.weight_succeeded == committee.n_shards - 1  # 6 of 7
        failed = next(o for o in report.outcomes if o.node_id == node0.node_id)
        assert failed.succeeded is False
        assert failed.reason is not None
        assert "RuntimeError" in failed.reason
        assert "unexpected metadata dispatch failure" in failed.reason
        others = [o for o in report.outcomes if o.node_id != node0.node_id]
        assert all(o.succeeded for o in others)


class TestUploadSliversQuorumNotMetWithRaisingNode:
    """Complement to ``TestUploadSliversNodeTaskException``: the fix must
    not swallow a genuine quorum failure. When a raising node's lost weight
    means quorum is never reached, ``upload_slivers`` must still raise
    ``SliverUploadError``, and its message must still carry the raising
    node's reason in the per-reason failure histogram."""

    async def test_quorum_not_reached_still_raises_when_node_task_raises(
        self,
    ) -> None:
        """node0 (weight 3) raises entirely; the remaining nodes
        (1 + 1 + 2 = 4) fall short of the required weight of 5, so
        ``upload_slivers`` must still raise ``SliverUploadError`` -- the fix
        must not be mistaken for 'swallow everything'."""
        committee = _multi_shard_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        node0 = committee.members[0]  # weight 3; without it, 1 + 1 + 2 = 4 < 5
        client = _RaisingMetadataClient(
            raise_for_base_url=node0.base_url,
            exc=RuntimeError("unexpected metadata dispatch failure"),
        )

        with pytest.raises(SliverUploadError) as excinfo:
            await upload_slivers(client=cast(WalrusClient, client), committee=committee, encoded=encoded)

        message = str(excinfo.value)
        assert "4" in message  # achieved weight
        assert "5" in message  # required weight
        assert "n_shards=7" in message
        assert "RuntimeError: unexpected metadata dispatch failure=1" in message
        assert excinfo.value.stage == "upload_slivers"


class TestUploadSliversNoOrphanedTasks:
    """FIX 3: every task ``upload_slivers`` (and, transitively,
    ``_upload_node``) creates via ``asyncio.create_task`` must be
    ``done()`` -- not left pending, running detached -- by the time
    ``upload_slivers`` returns or raises. Task creation is intercepted by
    monkeypatching ``native_upload.asyncio.create_task`` so every task the
    call spawns, at any depth, is tracked without needing access to
    ``upload_slivers``'s own local task dict."""

    def _track_tasks(
        self, *, monkeypatch: pytest.MonkeyPatch
    ) -> list[asyncio.Task[object]]:
        """Wrap ``asyncio.create_task`` (as seen through ``native_upload``'s
        own module-level ``asyncio`` import -- the SAME global ``asyncio``
        module, so this intercepts every call made anywhere in the process
        while installed) to record every task created, and return the live
        list.

        Args:
            monkeypatch (pytest.MonkeyPatch): Fixture used to install and
                auto-restore the wrapper.

        Returns:
            list[asyncio.Task[object]]: The live list tasks are appended to
            as they are created; inspect it after the call under test.
        """
        created: list[asyncio.Task[object]] = []
        original_create_task = asyncio.create_task

        def _tracking_create_task(
            coro: Coroutine[object, object, object], *, name: str | None = None
        ) -> asyncio.Task[object]:
            task = original_create_task(coro, name=name)
            created.append(task)
            return task

        monkeypatch.setattr(native_upload.asyncio, "create_task", _tracking_create_task)
        return created

    async def test_no_pending_tasks_after_quorum_with_straggler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A node whose primary sliver PUT hangs forever is left pending
        when quorum is reached from the other 6 nodes; the grace window
        must cancel its node task, which in turn (via ``_upload_node``'s
        own ``finally``) cancels its still-pending sliver-PUT task --
        leaving no orphaned task at either level by the time
        ``upload_slivers`` returns."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        hanging_node = committee.members[-1]
        client = _HangingSliverPutClient(hang_for=(hanging_node.base_url, "primary"))
        created = self._track_tasks(monkeypatch=monkeypatch)

        report = await asyncio.wait_for(
            upload_slivers(
                client=cast(WalrusClient, client),
                committee=committee,
                encoded=encoded,
                grace_base_seconds=0.05,
                grace_factor=0.0,
            ),
            timeout=5.0,
        )

        assert report.weight_succeeded == committee.n_shards - 1
        assert created, "no tasks were tracked -- the create_task hook did not fire"
        assert all(task.done() for task in created)
        straggler = next(
            o for o in report.outcomes if o.node_id == hanging_node.node_id
        )
        assert straggler.succeeded is False
        assert straggler.reason == "cancelled"

    async def test_no_pending_tasks_after_quorum_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same invariant on the quorum-FAILURE exit path: when
        ``SliverUploadError`` is raised, every task the call created is
        still ``done()`` -- none left orphaned by the early raise.

        This scenario has no hung task of its own: combining a permanent
        hang with a genuine quorum failure would deadlock the outer
        collection loop in ``upload_slivers``, since the grace window only
        bounds STRAGGLERS after quorum is reached, never a fan-out that can
        never reach quorum at all (that loop only exits early on reaching
        quorum, or once every task is done). Every task here therefore also
        finishes on its own before the raise; this still confirms the
        ``finally`` cleanup does not itself leave anything behind on the
        raising exit path."""
        committee = _multi_shard_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        client = _FakeStorageClient(
            put_failures={("https://node0.example:443", "primary"): "boom"}
        )
        created = self._track_tasks(monkeypatch=monkeypatch)

        with pytest.raises(SliverUploadError):
            await asyncio.wait_for(
                upload_slivers(
                    client=cast(WalrusClient, client),
                    committee=committee,
                    encoded=encoded,
                    retry_min_backoff=0.0,
                    retry_max_backoff=0.0,
                ),
                timeout=5.0,
            )

        assert created, "no tasks were tracked -- the create_task hook did not fire"
        assert all(task.done() for task in created)


class TestBytesInFlightThrottle:
    """The bytes-in-flight throttle (replacing the old request-count
    ``max_in_flight`` semaphore) admits an oversized single sliver rather
    than deadlocking."""

    async def test_oversized_single_acquire_is_admitted_without_deadlock(self) -> None:
        """A single acquire larger than the throttle's whole capacity must
        still be admitted when nothing else is in flight, rather than
        blocking forever waiting for room that can never exist."""
        throttle = _BytesInFlightThrottle(max_bytes=100)

        await asyncio.wait_for(throttle.acquire(size=1_000), timeout=1.0)
        await throttle.release(size=1_000)

        # Capacity is fully reclaimed after release; a normal-sized acquire
        # proceeds immediately afterwards.
        await asyncio.wait_for(throttle.acquire(size=50), timeout=1.0)
        await throttle.release(size=50)


class TestConfirmProgress:
    """``_ConfirmProgress`` accumulates confirmed WEIGHT as a running sum of
    each successful call's ``weight`` argument, not a count of successful
    calls -- the same shard-weight-not-node-count distinction
    ``TestUploadSliversWeightPerNode`` guards for the sliver fan-out side.
    ``render()`` embeds an elapsed-time value that varies per run, so these
    tests assert on stable substrings of the rendered line rather than full
    string equality."""

    def test_initial_state_renders_zero_done_weight_ok_fail(self) -> None:
        """A freshly constructed ``_ConfirmProgress`` renders done=0/total,
        weight=0/required_weight, ok=0, and fail=0 before ``finished`` is
        ever called."""
        progress = _ConfirmProgress(total=101, required_weight=667)

        rendered = progress.render()

        assert "done=0/101" in rendered
        assert "weight=0/667" in rendered
        assert "ok=0" in rendered
        assert "fail=0" in rendered

    def test_successful_confirmations_sum_weight_not_count(self) -> None:
        """Weight is the SUM of each successful call's ``weight`` argument,
        not a count of how many times ``finished(ok=True, ...)`` was
        called. This is the core regression guard: it must fail if
        accumulation ever reverts to counting nodes instead of summing
        shard weight."""
        progress = _ConfirmProgress(total=10, required_weight=667)

        progress.finished(ok=True, weight=3)
        progress.finished(ok=True, weight=100)
        progress.finished(ok=True, weight=177)

        rendered = progress.render()

        # 3 + 100 + 177 = 280, NOT 3 (a call count).
        assert "weight=280/667" in rendered
        assert "done=3/10" in rendered

    def test_failed_confirmation_does_not_accumulate_weight(self) -> None:
        """A failed confirmation increments ``done``/``fail`` but its
        ``weight`` argument must never be added to the confirmed weight
        total."""
        progress = _ConfirmProgress(total=10, required_weight=667)
        progress.finished(ok=True, weight=50)

        progress.finished(ok=False, weight=999)

        rendered = progress.render()

        assert "weight=50/667" in rendered
        assert "done=2/10" in rendered
        assert "fail=1" in rendered

    def test_mixed_sequence_keeps_done_ok_fail_weight_consistent(self) -> None:
        """Interleaving successful and failed confirmations keeps ``done``,
        ``ok``, ``fail``, and the summed ``weight`` numerator simultaneously
        correct, and preserves the invariant that rendered ``ok`` equals
        completed minus failed."""
        progress = _ConfirmProgress(total=10, required_weight=667)

        progress.finished(ok=True, weight=2)
        progress.finished(ok=False, weight=50)
        progress.finished(ok=True, weight=7)
        progress.finished(ok=False, weight=3)
        progress.finished(ok=True, weight=1)

        rendered = progress.render()

        assert "done=5/10" in rendered
        assert "weight=10/667" in rendered  # 2 + 7 + 1, excluding failures
        assert "ok=3" in rendered
        assert "fail=2" in rendered
        assert progress.completed - progress.failed == 3

    def test_required_weight_denominator_is_verbatim_and_immutable(self) -> None:
        """``required_weight`` is reported verbatim as passed to
        ``__init__`` and never changes across any number of ``finished``
        calls."""
        progress = _ConfirmProgress(total=10, required_weight=381)

        assert "weight=0/381" in progress.render()

        progress.finished(ok=True, weight=40)
        progress.finished(ok=False, weight=999)
        progress.finished(ok=True, weight=15)

        assert "weight=55/381" in progress.render()


class TestCollectConfirmationsQueriesAll:
    """Change 4: confirmation collection queries the WHOLE committee by
    default, regardless of any node's sliver-upload outcome."""

    async def test_queries_all_members_by_default_including_failed_upload_node(
        self,
    ) -> None:
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        registration = _registration(blob_id=encoded.blob_id)
        message = confirmation_message(epoch=committee.epoch, blob_id=encoded.blob_id)
        confirmations: dict[str, SignedConfirmation | None] = {}
        keys_by_node: dict[str, bytes] = {}
        for index, member in enumerate(committee.members):
            keys_by_node[member.node_id] = _public_key(index)
            confirmations[member.base_url] = SignedConfirmation(
                serialized_message=message, signature=_signature(index)
            )
        members_with_real_keys = tuple(
            dataclasses.replace(member, public_key=keys_by_node[member.node_id])
            for member in committee.members
        )
        committee = dataclasses.replace(committee, members=members_with_real_keys)

        # node3's sliver upload is treated as having failed in a prior
        # stage; collect_confirmations receives no `positions` restriction
        # and must query it anyway, exactly like every other member.
        failed_upload_node = next(m for m in committee.members if m.node_id == "node3")

        client = _FakeStorageClient(confirmations=confirmations)
        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
        ):
            certificate = await collect_confirmations(
                client=cast(WalrusClient, client), committee=committee, blob_id=encoded.blob_id, registration=registration
            )

        confirmation_calls = [
            base_url
            for base_url, command in client.calls
            if isinstance(command, GetStorageConfirmation) and base_url is not None
        ]
        assert failed_upload_node.base_url in confirmation_calls
        assert len(confirmation_calls) == len(committee.members)
        assert certificate.weight == committee.n_shards


class TestCollectConfirmationsTolerance:
    """Change 4: a minority of confirmation failures is tolerated -- logged
    and excluded -- so long as the rest still reach quorum."""

    async def test_minority_confirmation_failure_tolerated_when_quorum_met(self) -> None:
        committee = _multi_shard_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        registration = _registration(blob_id=encoded.blob_id)
        message = confirmation_message(epoch=committee.epoch, blob_id=encoded.blob_id)
        confirmations: dict[str, SignedConfirmation | None] = {}
        keys_by_node: dict[str, bytes] = {}
        for index, member in enumerate(committee.members):
            keys_by_node[member.node_id] = _public_key(index)
            if member.node_id == "node1":
                # node1 (weight 1) never gets a confirmation entry, so the
                # fake client reports "no confirmation configured" for it.
                continue
            confirmations[member.base_url] = SignedConfirmation(
                serialized_message=message, signature=_signature(index)
            )
        members_with_real_keys = tuple(
            dataclasses.replace(member, public_key=keys_by_node[member.node_id])
            for member in committee.members
        )
        committee = dataclasses.replace(committee, members=members_with_real_keys)

        client = _FakeStorageClient(confirmations=confirmations)
        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
        ):
            certificate = await collect_confirmations(
                client=cast(WalrusClient, client), committee=committee, blob_id=encoded.blob_id, registration=registration
            )

        # node0=3 + node2=1 + node3=2 = 6, meeting the required 5; node1's
        # weight (1) is excluded but does not prevent quorum.
        assert certificate.weight == committee.n_shards - 1


class TestCollectConfirmationsQuorumEarlyExit:
    """The NEW quorum early-exit path in ``collect_confirmations``: once
    confirmed weight reaches ``required_weight``, the collection loop stops
    WAITING for outstanding candidates rather than always awaiting every
    one (see the function's docstring, 'Concurrency policy' paragraph,
    mirroring ``upload_slivers``'s own early-exit).

    ``_FakeStorageClient`` cannot exercise this branch: every one of its
    dispatches resolves without any genuine ``await`` suspension, so all
    candidate tasks complete within the very first ``asyncio.wait`` call in
    the SAME event-loop pass, and there is never anything left pending for
    the early exit to actually skip -- the existing
    ``TestCollectConfirmations``/``TestCollectConfirmationsTolerance`` tests
    pass regardless of whether the early-exit ``break`` is present or
    removed. ``_HangingConfirmationClient`` forces a genuine, permanently
    still-pending straggler by hanging two nodes' dispatches on an
    ``asyncio.Event`` that is never set, so this test would FAIL (timeout)
    if the early-exit branch were deleted."""

    async def test_early_exit_returns_certificate_without_waiting_on_stragglers(
        self,
    ) -> None:
        """5 of 7 one-shard-per-node members (weight 5, exactly
        ``min_weight_for_quorum(n_shards=7)``) respond immediately; the
        remaining 2 hang forever. ``collect_confirmations`` must still
        return a valid, quorum-backed ``Certificate`` -- proving it did not
        wait for the hanging pair -- and the hanging nodes' response
        counter must stay at 0, proving their confirmations genuinely never
        completed. This is a call-tracking assertion, not a wall-clock one:
        the whole test is wrapped in ``asyncio.wait_for(..., timeout=5.0)``
        purely as a safety net against a regression that removes the
        early-exit/grace-window cancellation entirely (which would hang
        forever on the never-set event), not as a timing assertion on the
        early-exit path itself."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        registration = _registration(blob_id=encoded.blob_id)
        signed_committee, confirmations, message = _committee_with_signed_confirmations(
            committee=committee, blob_id=encoded.blob_id
        )
        hanging_nodes = signed_committee.members[5:]
        hang_for = frozenset(member.base_url for member in hanging_nodes)
        client = _HangingConfirmationClient(confirmations=confirmations, hang_for=hang_for)
        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
        ):
            certificate = await asyncio.wait_for(
                collect_confirmations(
                    client=cast(WalrusClient, client),
                    committee=signed_committee,
                    blob_id=encoded.blob_id,
                    registration=registration,
                    grace_base_seconds=0.05,
                    grace_factor=0.0,
                ),
                timeout=5.0,
            )

        required_weight = min_weight_for_quorum(n_shards=signed_committee.n_shards)
        assert certificate.serialized_message == message
        assert certificate.weight >= required_weight
        for member in hanging_nodes:
            assert client.responded.get(member.base_url, 0) == 0


class TestCollectConfirmationsNoOrphanedTasks:
    """No orphaned tasks after the early-exit path: every task
    ``collect_confirmations`` creates via ``asyncio.create_task`` (each
    ``_confirm_node`` task, plus the diagnostic heartbeat task) must be
    ``done()`` by the time it returns -- the ``finally`` block's
    cancel-and-await cleanup must reach the hanging stragglers left behind
    by the early exit, not just the tasks already resolved when quorum was
    reached. Reuses the ``_track_tasks`` monkeypatch pattern established by
    ``TestUploadSliversNoOrphanedTasks`` (see its docstring for why:
    intercepting ``native_upload``'s own module-level ``asyncio.create_task``
    catches every task the call spawns, at any depth, without needing
    access to ``collect_confirmations``'s own local task dict)."""

    def _track_tasks(
        self, *, monkeypatch: pytest.MonkeyPatch
    ) -> list[asyncio.Task[object]]:
        """Wrap ``asyncio.create_task`` to record every task created.

        Duplicated from ``TestUploadSliversNoOrphanedTasks._track_tasks``
        rather than shared, matching this file's existing per-class helper
        convention (see that method's own docstring for the full mechanism
        description).

        Args:
            monkeypatch (pytest.MonkeyPatch): Fixture used to install and
                auto-restore the wrapper.

        Returns:
            list[asyncio.Task[object]]: The live list tasks are appended to
            as they are created; inspect it after the call under test.
        """
        created: list[asyncio.Task[object]] = []
        original_create_task = asyncio.create_task

        def _tracking_create_task(
            coro: Coroutine[object, object, object], *, name: str | None = None
        ) -> asyncio.Task[object]:
            task = original_create_task(coro, name=name)
            created.append(task)
            return task

        monkeypatch.setattr(native_upload.asyncio, "create_task", _tracking_create_task)
        return created

    async def test_no_pending_tasks_after_early_exit_with_stragglers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same 5-immediate/2-hanging scenario as
        ``TestCollectConfirmationsQuorumEarlyExit``: once quorum is reached
        from the 5 immediate responders, the grace window must cancel the 2
        hanging confirmation tasks -- leaving no task (heartbeat included)
        still pending by the time ``collect_confirmations`` returns."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        registration = _registration(blob_id=encoded.blob_id)
        signed_committee, confirmations, _ = _committee_with_signed_confirmations(
            committee=committee, blob_id=encoded.blob_id
        )
        hanging_nodes = signed_committee.members[5:]
        hang_for = frozenset(member.base_url for member in hanging_nodes)
        client = _HangingConfirmationClient(confirmations=confirmations, hang_for=hang_for)
        created = self._track_tasks(monkeypatch=monkeypatch)
        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
        ):
            certificate = await asyncio.wait_for(
                collect_confirmations(
                    client=cast(WalrusClient, client),
                    committee=signed_committee,
                    blob_id=encoded.blob_id,
                    registration=registration,
                    grace_base_seconds=0.05,
                    grace_factor=0.0,
                ),
                timeout=5.0,
            )

        required_weight = min_weight_for_quorum(n_shards=signed_committee.n_shards)
        assert certificate.weight >= required_weight
        assert created, "no tasks were tracked -- the create_task hook did not fire"
        assert all(task.done() for task in created)


class TestCollectConfirmationsGraceWindow:
    """The dynamic grace window -- ``extra_time = grace_base_seconds +
    grace_factor * time_to_quorum`` -- must still admit a straggler that
    finishes shortly AFTER quorum weight is reached, not only ones that
    finish before.

    Made deterministic via ``_GatedStragglerConfirmationClient`` (see its
    docstring): the straggler is gated on an ``asyncio.Event`` the fake
    itself releases the instant the quorum-reaching batch has responded,
    not on any sleep or wall-clock delay, so its completion relative to the
    grace window is guaranteed by ``asyncio.wait``'s own scheduling
    semantics, not by timing luck -- no ``asyncio.sleep``-based race and no
    wall-clock assertion appears anywhere in this test or its fake."""

    async def test_late_but_quick_straggler_is_included_in_certificate(
        self,
    ) -> None:
        """6 of the 7 one-shard-per-node members respond immediately
        (weight 6, already past the required 5); the 7th (the straggler,
        weight 1) is gated to respond only once those 6 have. With an
        explicit, short grace window (``grace_base_seconds=0.2``,
        ``grace_factor=0.0``), the straggler must still be included: the
        final certificate's weight covers all 7 members, not just the 6
        that were already done when quorum was checked."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        registration = _registration(blob_id=encoded.blob_id)
        signed_committee, confirmations, message = _committee_with_signed_confirmations(
            committee=committee, blob_id=encoded.blob_id
        )
        straggler = signed_committee.members[5]
        quorum_members = tuple(
            member for member in signed_committee.members if member is not straggler
        )
        quorum_base_urls = frozenset(member.base_url for member in quorum_members)
        client = _GatedStragglerConfirmationClient(
            confirmations=confirmations,
            quorum_base_urls=quorum_base_urls,
            straggler_base_url=straggler.base_url,
        )
        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
        ):
            certificate = await asyncio.wait_for(
                collect_confirmations(
                    client=cast(WalrusClient, client),
                    committee=signed_committee,
                    blob_id=encoded.blob_id,
                    registration=registration,
                    grace_base_seconds=0.2,
                    grace_factor=0.0,
                ),
                timeout=5.0,
            )

        assert certificate.serialized_message == message
        # All 7 members: the 6 quorum-group responders plus the straggler,
        # admitted within the grace window -- not just the 6 that were
        # already done when weight_confirmed first reached required_weight.
        assert certificate.weight == signed_committee.n_shards


class TestCollectConfirmationsFailurePathUnchanged:
    """The NEW early-exit branch must never fire when quorum is
    unreachable: with achievable confirmed weight permanently below
    ``required_weight``, every candidate is still queried and awaited
    exactly as before the early-exit change, and
    ``ConfirmationCollectionError`` is still raised carrying the underlying
    ``QuorumNotReachedError`` diagnostics (achieved weight, required
    weight, ``n_shards``) -- consistent with how
    ``TestCollectConfirmationsTolerance`` already asserts the tolerated-
    failure behaviour on the success side."""

    async def test_insufficient_weight_awaits_every_candidate_and_raises(
        self,
    ) -> None:
        """node0 (weight 3) and node1 (weight 1) never get a confirmation
        configured for them (the fake reports 'no confirmation configured'
        for both, exactly like ``TestCollectConfirmationsTolerance``'s
        excluded node); node2 (weight 1) and node3 (weight 2) do respond,
        for a maximum achievable confirmed weight of 3 -- permanently below
        the required weight of 5, so the early-exit ``break`` can never
        fire. Every one of the 4 candidates must still be queried, and the
        raised error must name both the achieved and required weight."""
        committee = _multi_shard_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        registration = _registration(blob_id=encoded.blob_id)
        signed_committee, confirmations, _ = _committee_with_signed_confirmations(
            committee=committee, blob_id=encoded.blob_id
        )
        node0 = next(m for m in signed_committee.members if m.node_id == "node0")
        node1 = next(m for m in signed_committee.members if m.node_id == "node1")
        del confirmations[node0.base_url]
        del confirmations[node1.base_url]

        client = _FakeStorageClient(confirmations=confirmations)
        with (
            patch("pytusk.core.certification.bls_aggregate", return_value=b"\x00" * 96),
            patch(
                "pytusk.core.certification.bls_aggregate_verify", return_value=True
            ),
            pytest.raises(ConfirmationCollectionError) as excinfo,
        ):
            await collect_confirmations(
                client=cast(WalrusClient, client),
                committee=signed_committee,
                blob_id=encoded.blob_id,
                registration=registration,
            )

        confirmation_calls = [
            base_url
            for base_url, command in client.calls
            if isinstance(command, GetStorageConfirmation) and base_url is not None
        ]
        assert sorted(confirmation_calls) == sorted(
            member.base_url for member in signed_committee.members
        )
        message = str(excinfo.value)
        assert "Signer weight 3 does not reach quorum" in message
        assert "at least 5 for n_shards=7" in message
        assert excinfo.value.stage == "collect_confirmations"


class _FakeCertifyNetworkConfig:
    """Minimal ``client.config.network``-shaped fake: only the two fields
    ``store_blob_native`` reads before its mocked stage functions take
    over."""

    def __init__(self, *, system_object: str, staking_object: str) -> None:
        self.system_object = system_object
        self.staking_object = staking_object


class _FakeCertifyClientConfig:
    """Minimal ``client.config``-shaped fake wrapping ``.network``."""

    def __init__(self, *, network: _FakeCertifyNetworkConfig) -> None:
        self.network = network


class _FakeCertifyClient:
    """Minimal ``WalrusClient``-shaped fake for the Tx2-failure test below.

    Only ``.committee()`` and ``.config.network.{system_object,
    staking_object}`` are ever read from this object -- every network call
    ``store_blob_native``/``certify`` would otherwise make
    (``resolve_package_id``, ``execute_reserve_and_register``,
    ``upload_slivers``, ``collect_confirmations``, ``fetch_epoch``,
    ``execute_certify``) is monkeypatched out in the test itself, so this
    fake never needs an ``execute()`` method.
    """

    def __init__(
        self, *, committee: WalrusCommittee, system_object: str, staking_object: str
    ) -> None:
        self._committee = committee
        self.config = _FakeCertifyClientConfig(
            network=_FakeCertifyNetworkConfig(
                system_object=system_object, staking_object=staking_object
            )
        )

    async def committee(self) -> WalrusCommittee:
        return self._committee


class TestCertifyTx2Failure:
    """Fix: a Tx2 failure -- whether ``execute_certify``'s bare
    ``RuntimeError`` (submission failure or on-chain abort) or pysui's bare
    ``ValueError`` (gas-estimation/simulate dry-run failure inside
    ``txn.build_and_sign()``) -- must become a ``CertifyTransactionError``
    so it is handled uniformly by ``store_blob_native``'s ``except
    NativeUploadError`` clause and yields a receipt -- not an uncaught
    exception with no blob ID, object ID, or timings. The ``ValueError``
    cases pin a real defect observed on a live testnet run: before the fix,
    ``certify()``'s ``except RuntimeError`` clause did not catch
    ``ValueError``, so a Tx2 simulate failure escaped this module entirely.
    See the module docstring for why these tests monkeypatch the
    network-call boundary instead of using a live node.
    """

    async def test_certify_converts_execute_certify_runtimeerror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``certify()`` itself: the original on-chain error text must
        survive verbatim in the re-raised exception's message, the stage
        must be ``"certify"``, the exception must chain ``from`` the
        original ``RuntimeError``, and a ``duration`` must be attached."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        registration = _registration(blob_id=encoded.blob_id)
        original_message = (
            "certify_blob transaction aborted on-chain: EWrongEpoch"
        )

        async def fake_fetch_epoch(*, reader: object, staking_object: str) -> int:
            return committee.epoch

        async def fake_execute_certify(**kwargs: object) -> object:
            raise RuntimeError(original_message)

        monkeypatch.setattr(native_upload, "fetch_epoch", fake_fetch_epoch)
        monkeypatch.setattr(native_upload, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(
            native_upload, "verify_certificate", lambda **kwargs: True
        )

        with pytest.raises(CertifyTransactionError) as excinfo:
            await certify(
                client=cast(WalrusClient, object()),
                committee=committee,
                blob_id=encoded.blob_id,
                registration=registration,
                certificate=cast(Certificate, SimpleNamespace(signer_positions=())),
                package_id="0xpkg",
                system_object="0xsystem",
                staking_object="0xstaking",
            )

        assert excinfo.value.stage == "certify"
        assert original_message in str(excinfo.value)
        assert excinfo.value.duration is not None
        assert excinfo.value.duration >= 0
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert str(excinfo.value.__cause__) == original_message

    async def test_certify_converts_execute_certify_valueerror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for the exception-wrapping defect: pysui's
        gas-estimation dry run inside ``txn.build_and_sign()`` signals
        failure with a bare ``ValueError`` (pysui's ``txn_gas.py`` raises
        ``ValueError(f"Error running SimulateTransactionKind: ...")``), NOT
        ``RuntimeError``. ``certify()``'s ``except`` clause must catch both,
        exactly mirroring
        ``test_certify_converts_execute_certify_runtimeerror`` above: the
        original error text must survive verbatim, the stage must be
        ``"certify"``, the exception must chain ``from`` the original
        ``ValueError``, and a ``duration`` must be attached."""
        committee = _one_shard_per_node_committee()
        encoded = encode_blob(data=_BLOB_DATA, n_shards=committee.n_shards)
        registration = _registration(blob_id=encoded.blob_id)
        original_message = "Error running SimulateTransactionKind: boom"

        async def fake_fetch_epoch(*, reader: object, staking_object: str) -> int:
            return committee.epoch

        async def fake_execute_certify(**kwargs: object) -> object:
            raise ValueError(original_message)

        monkeypatch.setattr(native_upload, "fetch_epoch", fake_fetch_epoch)
        monkeypatch.setattr(native_upload, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(
            native_upload, "verify_certificate", lambda **kwargs: True
        )

        with pytest.raises(CertifyTransactionError) as excinfo:
            await certify(
                client=cast(WalrusClient, object()),
                committee=committee,
                blob_id=encoded.blob_id,
                registration=registration,
                certificate=cast(Certificate, SimpleNamespace(signer_positions=())),
                package_id="0xpkg",
                system_object="0xsystem",
                staking_object="0xstaking",
            )

        assert excinfo.value.stage == "certify"
        assert original_message in str(excinfo.value)
        assert excinfo.value.duration is not None
        assert excinfo.value.duration >= 0
        assert isinstance(excinfo.value.__cause__, ValueError)
        assert str(excinfo.value.__cause__) == original_message

    async def test_store_blob_native_tx2_failure_yields_receipt_with_timings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of the fix: a Tx2 failure must reach
        ``store_blob_native``'s caller as a ``NativeBlobReceipt`` --
        ``certified=False``, ``failed_stage="certify"``, and every stage's
        timing populated, INCLUDING ``certify_tx2`` (the regression this
        fix closes: that field was previously always ``None`` on the
        failure path)."""
        committee = _one_shard_per_node_committee()
        registration = _registration(blob_id=b"\x00" * 32)

        async def fake_fetch_epoch(*, reader: object, staking_object: str) -> int:
            return committee.epoch

        async def fake_execute_certify(**kwargs: object) -> object:
            raise RuntimeError(
                "certify_blob transaction aborted on-chain: EWrongEpoch"
            )

        async def fake_resolve_package_id(**kwargs: object) -> str:
            return "0xpkg"

        async def fake_execute_reserve_and_register(**kwargs: object) -> Registration:
            return registration

        async def fake_upload_slivers(**kwargs: object) -> FanoutReport:
            return FanoutReport(
                outcomes=(), weight_succeeded=committee.n_shards, n_shards=committee.n_shards
            )

        async def fake_collect_confirmations(**kwargs: object) -> object:
            return SimpleNamespace(signer_positions=())

        monkeypatch.setattr(native_upload, "fetch_epoch", fake_fetch_epoch)
        monkeypatch.setattr(native_upload, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(native_upload, "resolve_package_id", fake_resolve_package_id)
        monkeypatch.setattr(
            native_upload, "execute_reserve_and_register", fake_execute_reserve_and_register
        )
        monkeypatch.setattr(native_upload, "upload_slivers", fake_upload_slivers)
        monkeypatch.setattr(native_upload, "collect_confirmations", fake_collect_confirmations)
        monkeypatch.setattr(
            native_upload, "verify_certificate", lambda **kwargs: True
        )

        client = _FakeCertifyClient(
            committee=committee, system_object="0xsystem", staking_object="0xstaking"
        )

        receipt = await store_blob_native(client=cast(WalrusClient, client), data=_BLOB_DATA, epochs=3)

        assert receipt.certified is False
        assert receipt.failed_stage == "certify"
        assert receipt.object_id == registration.object_id
        assert receipt.timings.encode is not None
        assert receipt.timings.register_tx1 is not None
        assert receipt.timings.sliver_upload is not None
        assert receipt.timings.confirmations is not None
        assert receipt.timings.certify_tx2 is not None
        assert receipt.timings.certify_tx2 >= 0
        assert receipt.timings.total is not None

    async def test_store_blob_native_tx2_valueerror_yields_receipt_with_timings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for the exception-wrapping defect observed on a
        live testnet run: pysui's gas-estimation dry run inside
        ``execute_certify``'s ``txn.build_and_sign()`` raises ``ValueError``,
        not ``RuntimeError``. Before the fix, this escaped ``certify()``'s
        ``except RuntimeError`` clause unwrapped, propagated past
        ``store_blob_native``'s ``except NativeUploadError`` handler, and
        left the library as a raised exception instead of the documented
        failed ``NativeBlobReceipt``. This must now behave identically to
        the ``RuntimeError`` case above: ``certified=False``,
        ``failed_stage="certify"``, and every stage's timing populated --
        NOT an exception escaping this call."""
        committee = _one_shard_per_node_committee()
        registration = _registration(blob_id=b"\x00" * 32)

        async def fake_fetch_epoch(*, reader: object, staking_object: str) -> int:
            return committee.epoch

        async def fake_execute_certify(**kwargs: object) -> object:
            raise ValueError("Error running SimulateTransactionKind: boom")

        async def fake_resolve_package_id(**kwargs: object) -> str:
            return "0xpkg"

        async def fake_execute_reserve_and_register(**kwargs: object) -> Registration:
            return registration

        async def fake_upload_slivers(**kwargs: object) -> FanoutReport:
            return FanoutReport(
                outcomes=(), weight_succeeded=committee.n_shards, n_shards=committee.n_shards
            )

        async def fake_collect_confirmations(**kwargs: object) -> object:
            return SimpleNamespace(signer_positions=())

        monkeypatch.setattr(native_upload, "fetch_epoch", fake_fetch_epoch)
        monkeypatch.setattr(native_upload, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(native_upload, "resolve_package_id", fake_resolve_package_id)
        monkeypatch.setattr(
            native_upload, "execute_reserve_and_register", fake_execute_reserve_and_register
        )
        monkeypatch.setattr(native_upload, "upload_slivers", fake_upload_slivers)
        monkeypatch.setattr(native_upload, "collect_confirmations", fake_collect_confirmations)
        monkeypatch.setattr(
            native_upload, "verify_certificate", lambda **kwargs: True
        )

        client = _FakeCertifyClient(
            committee=committee, system_object="0xsystem", staking_object="0xstaking"
        )

        receipt = await store_blob_native(client=cast(WalrusClient, client), data=_BLOB_DATA, epochs=3)

        assert receipt.certified is False
        assert receipt.failed_stage == "certify"
        assert receipt.object_id == registration.object_id
        assert receipt.timings.encode is not None
        assert receipt.timings.register_tx1 is not None
        assert receipt.timings.sliver_upload is not None
        assert receipt.timings.confirmations is not None
        assert receipt.timings.certify_tx2 is not None
        assert receipt.timings.certify_tx2 >= 0
        assert receipt.timings.total is not None
