#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Sliver fan-out stage of the native read pipeline.

See :mod:`pytusk.core.native_read` for the fixed stage order and for why
this fan-out does not retry a failing node.
"""

import asyncio
import logging

from pytusk.commands.node_commands import GetSliver, SliverData
from pytusk.core.chain import WalrusCommittee, WalrusCommitteeMember
from pytusk.core.committee_fanout import fan_out_to_committee
from pytusk.core.encoding import blob_id_to_url_base64, shard_index_to_pair_index
from pytusk.core.types import ExecuteOnlyClient, SliverFetchError

_logger = logging.getLogger(__name__)


async def _fetch_node_slivers(
    *,
    client: ExecuteOnlyClient,
    member: WalrusCommitteeMember,
    blob_id: bytes,
    axis: str,
    n_shards: int,
    max_node_connections: int,
    fanout_semaphore: asyncio.Semaphore,
    max_sliver_bytes: int | None = None,
) -> list[bytes]:
    """Fetch every sliver one node holds, on a single axis.

    Each of the node's SHARD indices is rotated into its SLIVER-PAIR index
    before the URL is built. Slivers that fail are dropped rather than
    retried: the fan-out above only needs enough slivers in total.

    Args:
        client (ExecuteOnlyClient): Client used to issue sliver GETs.
        member (WalrusCommitteeMember): The node to read from.
        blob_id (bytes): Raw 32-byte blob ID being read.
        axis (str): ``"primary"`` or ``"secondary"``.
        n_shards (int): Committee shard count, for the rotation.
        max_node_connections (int): Concurrent requests allowed to this node.
        fanout_semaphore (asyncio.Semaphore): Ceiling shared across EVERY
            node in the fan-out. The per-node semaphore below bounds one
            node's parallelism; without a shared one the whole fan-out's
            resident bytes are bounded only by httpx's connection pool,
            which makes the ceiling a transport side effect rather than
            this library's decision.
        max_sliver_bytes (int | None): Per-response cap handed to each
            ``GetSliver``, or None to leave responses uncapped.

    Returns:
        list[bytes]: Raw bytes of every sliver this node supplied.
    """
    semaphore = asyncio.Semaphore(max_node_connections)

    async def _one(shard_index: int) -> bytes | None:
        """Fetch a single sliver, returning None if it is unavailable."""
        pair_index = shard_index_to_pair_index(
            shard_index=shard_index, blob_id=blob_id, n_shards=n_shards
        )
        async with fanout_semaphore, semaphore:
            result = await client.execute(
                command=GetSliver(
                    blob_id=blob_id,
                    sliver_pair_index=pair_index,
                    sliver_type=axis,
                    max_bytes=max_sliver_bytes,
                ),
                base_url=member.base_url,
            )
        if not result.is_ok():
            return None
        payload = result.result_data
        if not isinstance(payload, SliverData):
            return None
        return payload.content

    outcomes = await asyncio.gather(
        *(_one(shard_index) for shard_index in member.shard_indices),
        return_exceptions=True,
    )
    return [item for item in outcomes if isinstance(item, bytes)]


async def _fetch_slivers_attributed(
    *,
    client: ExecuteOnlyClient,
    committee: WalrusCommittee,
    blob_id: bytes,
    axis: str,
    threshold: int,
    exclude_node_ids: frozenset[str] = frozenset(),
    max_node_connections: int = 10,
    max_concurrent_requests: int = 512,
    max_sliver_bytes: int | None = None,
    grace_base_seconds: float = 0.5,
    grace_factor: float = 0.5,
) -> list[tuple[bytes, str]]:
    """Fetch slivers across the committee, recording which node served each.

    The shared core beneath :func:`fetch_slivers`. Node attribution is what
    lets a caller INDICT whoever served an unusable sliver and re-run the
    fan-out without them, so it is carried here and dropped by the public
    wrapper: an ordinary read has no use for it, and a return type padded
    with it would make every caller carry the recovery path's machinery.

    Work is grouped by NODE, not by raw shard index, so a member holding
    several shards is dispatched once and contributes all of its slivers
    together. Dispatch, grace period and cancellation are delegated to
    :func:`~pytusk.core.committee_fanout.fan_out_to_committee`; what stays
    here is what THIS caller means by a result, namely a running count of
    usable slivers.

    Sliver ORDER does not matter to the decoder -- each sliver carries its
    own index in its bytes -- so the returned list is in completion order
    and may contain more than ``threshold`` entries.

    Slivers are deduplicated BY CONTENT, and the threshold counts only what
    survives that. Counting raw responses instead would let one node holding
    k shards answer all k of its requests with a single valid sliver and
    advance the count by k: the fan-out would stop early, cancel the honest
    stragglers still in flight, and hand the decoder fewer DISTINCT symbols
    than it needs. Byte-identity is an exact proxy for index-identity here --
    BCS is canonical, so for any given sliver index there is exactly one byte
    string that verifies against the metadata.

    Args:
        client (ExecuteOnlyClient): Client used to issue sliver GETs.
        committee (WalrusCommittee): Chain-sourced committee to read from.
        blob_id (bytes): Raw 32-byte blob ID being read.
        axis (str): ``"primary"`` or ``"secondary"``. Primary needs roughly
            half as many slivers and should be preferred.
        threshold (int): Number of slivers the decoder needs for ``axis``,
            from :func:`~pytusk.core.encoding.source_symbol_counts`.
        exclude_node_ids (frozenset[str]): Node IDs to skip entirely. Used to
            re-run the fan-out without nodes already shown to have served an
            unusable sliver.
        max_node_connections (int): Concurrent requests allowed per node.
        max_concurrent_requests (int): Ceiling on in-flight sliver GETs
            across the WHOLE fan-out. Deliberately below httpx's pool limit
            so the bound on resident bytes is this library's choice rather
            than a transport artefact.
        max_sliver_bytes (int | None): Per-response cap for each sliver GET,
            or None to leave them uncapped.
        grace_base_seconds (float): Straggler grace base, passed through.
        grace_factor (float): Straggler grace factor, passed through.

    Returns:
        list[tuple[bytes, str]]: ``(sliver_bytes, node_id)`` pairs for at
            least ``threshold`` DISTINCT slivers, in completion order.

    Raises:
        SliverFetchError: If the threshold could not be reached.
    """
    fanout_semaphore = asyncio.Semaphore(max_concurrent_requests)
    task_members: dict[asyncio.Task[list[bytes]], WalrusCommitteeMember] = {}
    for member in committee.members:
        if not member.shard_indices:
            continue
        if member.node_id in exclude_node_ids:
            continue
        task = asyncio.create_task(
            _fetch_node_slivers(
                client=client,
                member=member,
                blob_id=blob_id,
                axis=axis,
                n_shards=committee.n_shards,
                max_node_connections=max_node_connections,
                fanout_semaphore=fanout_semaphore,
                max_sliver_bytes=max_sliver_bytes,
            )
        )
        task_members[task] = member

    collected: list[tuple[bytes, str]] = []
    seen: set[bytes] = set()
    nodes_answered = 0

    def _fold(
        task: asyncio.Task[list[bytes]], member: WalrusCommitteeMember
    ) -> None:
        """Fold one node's slivers into the running total. Must not raise."""
        nonlocal nodes_answered
        try:
            node_slivers = task.result()
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - one node's failure must not abort the fan-out
            _logger.debug("node %s supplied no slivers: %s", member.node_id, exc)
            return
        if node_slivers:
            nodes_answered += 1
            for sliver in node_slivers:
                # Dedupe by content: the threshold must count distinct
                # symbols, not responses. The decoder dedupes on each
                # sliver's own index, so counting a repeat here would stop
                # the fan-out short of what the decode actually needs.
                if sliver in seen:
                    continue
                seen.add(sliver)
                collected.append((sliver, member.node_id))

    def _cancelled(
        _task: asyncio.Task[list[bytes]], _member: WalrusCommitteeMember
    ) -> None:
        """Ignore stragglers cancelled after the threshold was reached."""
        return None

    await fan_out_to_committee(
        tasks=task_members,
        on_completed=_fold,
        on_cancelled=_cancelled,
        should_stop=lambda: len(collected) >= threshold,
        label="fetch_slivers",
        grace_base_seconds=grace_base_seconds,
        grace_factor=grace_factor,
    )

    if len(collected) < threshold:
        raise SliverFetchError(
            message=(
                f"Sliver fan-out collected {len(collected)} of {threshold} "
                f"required {axis} slivers for blob "
                f"{blob_id_to_url_base64(blob_id=blob_id)} "
                f"from {nodes_answered} responding node(s)"
            ),
            stage="fetch_slivers",
        )
    return collected


async def fetch_slivers(
    *,
    client: ExecuteOnlyClient,
    committee: WalrusCommittee,
    blob_id: bytes,
    axis: str,
    threshold: int,
    max_node_connections: int = 10,
    max_concurrent_requests: int = 512,
    max_sliver_bytes: int | None = None,
    grace_base_seconds: float = 0.5,
    grace_factor: float = 0.5,
) -> list[bytes]:
    """Fetch slivers across the committee until the decoding threshold is met.

    Work is grouped by NODE, not by raw shard index, so a member holding
    several shards is dispatched once and contributes all of its slivers
    together.

    Slivers are deduplicated BY CONTENT and the threshold counts only what
    survives that. :func:`_fetch_slivers_attributed` does the work and also
    records which node served each sliver; this wrapper drops that, because
    an ordinary read never needs to indict anyone.

    Sliver ORDER does not matter to the decoder -- each sliver carries its
    own index in its bytes -- so the returned list is in completion order
    and may contain more than ``threshold`` entries.

    Args:
        client (ExecuteOnlyClient): Client used to issue sliver GETs.
        committee (WalrusCommittee): Chain-sourced committee to read from.
        blob_id (bytes): Raw 32-byte blob ID being read.
        axis (str): ``"primary"`` or ``"secondary"``. Primary needs roughly
            half as many slivers and should be preferred.
        threshold (int): Number of slivers the decoder needs for ``axis``,
            from :func:`~pytusk.core.encoding.source_symbol_counts`.
        max_node_connections (int): Concurrent requests allowed per node.
        max_concurrent_requests (int): Ceiling on in-flight sliver GETs
            across the whole fan-out.
        max_sliver_bytes (int | None): Per-response cap for each sliver GET,
            or None to leave them uncapped.
        grace_base_seconds (float): Straggler grace base, passed through.
        grace_factor (float): Straggler grace factor, passed through.

    Returns:
        list[bytes]: At least ``threshold`` DISTINCT raw slivers, in
            completion order.

    Raises:
        SliverFetchError: If the threshold could not be reached.
    """
    attributed = await _fetch_slivers_attributed(
        client=client,
        committee=committee,
        blob_id=blob_id,
        axis=axis,
        threshold=threshold,
        max_node_connections=max_node_connections,
        max_concurrent_requests=max_concurrent_requests,
        max_sliver_bytes=max_sliver_bytes,
        grace_base_seconds=grace_base_seconds,
        grace_factor=grace_factor,
    )
    return [sliver for sliver, _node_id in attributed]
