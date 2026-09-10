#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Sliver fetch and decode, with recovery from a node that serves bad bytes.

WHY THIS STAGE EXISTS. The extension decodes a batch of slivers in a single
call, and that call is all-or-nothing at its BCS boundary: every sliver is
deserialized into one vector before any of them is used, so a SINGLE
unparseable sliver aborts the batch and discards the hundreds of good
slivers gathered alongside it. Retrying re-fetches from the same committee,
the same bad node answers as promptly as it did before, and the read fails
identically. One faulty node out of a thousand can therefore deny a read
outright -- against a design meant to tolerate roughly a third of them.

RECOVERY COSTS NOTHING ON THE HAPPY PATH. The first decode is attempted on
everything the fan-out collected, and only its FAILURE triggers the
per-sliver verification that identifies whom to blame. That ordering is
deliberate rather than lazy: verifying up front would be correct and far too
expensive, since each check re-encodes one sliver out to ``n_shards``
symbols and rebuilds a Merkle tree over them, where a single decode proves
the same property for the whole blob in one re-encode.

RECOVERY IS BOUNDED TO ONE ADDITIONAL FAN-OUT. Fetching slivers is the
expensive half of a read, an unbounded indict-and-retry loop would let a
committee holding many bad nodes stall a caller indefinitely, and the second
round already bars every node the first round convicted.

WHAT VERIFICATION DOES AND DOES NOT ESTABLISH. A sliver is bound to the
index it declares for itself, never to whichever index some node was asked
for, so a node answering with a genuine sliver for a different index passes
the check. That is correct: the decoder reads each sliver's own index and
dedupes on it. What is caught here is forged or corrupted content.
"""

import logging
import typing

from pytusk.core.chain import WalrusCommittee
from pytusk.core.encoding import (
    BlobDecodeError,
    SliverVerificationError,
    VerifiedBlobMetadata,
    decode_blob,
    source_symbol_counts,
    symbol_size,
    verify_sliver,
)
from pytusk.core.native_read.fetch import _fetch_slivers_attributed
from pytusk.core.types import ExecuteOnlyClient

_logger = logging.getLogger(__name__)

_SLIVER_CAP_SLACK: int = 4096
"""Headroom added to a sliver's computed size before it becomes a cap.

A bound, not a size check -- BCS framing rides along with the symbols, and
an equality would turn a framing change into an outage.
"""

_Attributed: typing.TypeAlias = list[tuple[bytes, str]]
"""``(sliver_bytes, node_id)`` pairs, as ``_fetch_slivers_attributed`` returns."""


def _sliver_cap(*, metadata: VerifiedBlobMetadata) -> int:
    """Return a ceiling on the wire size of any one sliver of this blob.

    The LARGER axis's symbol count is used whichever axis is being read: the
    two differ by roughly half again, and sizing the cap to the wrong one
    would reject honest slivers, where sizing it to the larger merely bounds
    a hostile node slightly less tightly. A cap that can produce false
    rejections is worse than a slightly loose one.

    Args:
        metadata (VerifiedBlobMetadata): Verified metadata for this blob.

    Returns:
        int: Maximum bytes a single sliver of this blob should occupy.
    """
    symbols = max(source_symbol_counts(n_shards=metadata.n_shards))
    per_symbol = symbol_size(
        blob_length=metadata.unencoded_length, n_shards=metadata.n_shards
    )
    return symbols * per_symbol + _SLIVER_CAP_SLACK


def _decode_attributed(
    *,
    attributed: _Attributed,
    metadata: VerifiedBlobMetadata,
    axis: typing.Literal["primary", "secondary"],
    verify: bool,
) -> tuple[bytes, int]:
    """Decode the slivers out of ``attributed``, discarding attribution.

    Args:
        attributed (_Attributed): Slivers paired with their serving node.
        metadata (VerifiedBlobMetadata): Verified metadata for this blob.
        axis (Literal["primary", "secondary"]): Axis being reconstructed.
        verify (bool): Passed through to :func:`decode_blob`.

    Returns:
        tuple[bytes, int]: Decoded content, and the sliver count used.

    Raises:
        BlobDecodeError: If the slivers are insufficient or bad.
    """
    slivers = [sliver for sliver, _node_id in attributed]
    content = decode_blob(
        slivers=slivers, metadata=metadata, axis=axis, verify=verify
    )
    return content, len(slivers)


def _partition_by_verification(
    *,
    attributed: _Attributed,
    metadata: VerifiedBlobMetadata,
    axis: typing.Literal["primary", "secondary"],
) -> tuple[_Attributed, frozenset[str]]:
    """Split slivers into those that verify and the nodes serving the rest.

    EVERY sliver is checked rather than stopping at the first failure: a
    second bad node left unconvicted would simply be re-queried by the
    retry, and the retry is the only one there is.

    A node that served one good sliver and one bad one keeps its good sliver
    here -- that sliver verified, so it is usable -- while still being
    indicted, because nothing further it serves can be trusted to arrive
    intact.

    Args:
        attributed (_Attributed): Slivers paired with their serving node.
        metadata (VerifiedBlobMetadata): Verified metadata for this blob.
        axis (Literal["primary", "secondary"]): Axis being reconstructed.

    Returns:
        tuple[_Attributed, frozenset[str]]: The slivers that verified, and
            the IDs of every node that served one that did not.
    """
    verified: _Attributed = []
    indicted: set[str] = set()
    for sliver, node_id in attributed:
        try:
            verify_sliver(sliver=sliver, metadata=metadata, axis=axis)
        except SliverVerificationError as exc:
            _logger.warning(
                "node %s served an unusable sliver, excluding it: %s",
                node_id,
                exc,
            )
            indicted.add(node_id)
            continue
        verified.append((sliver, node_id))
    return verified, frozenset(indicted)


def _merge_distinct(*, first: _Attributed, second: _Attributed) -> _Attributed:
    """Concatenate two attributed sliver lists, dropping repeated content.

    Args:
        first (_Attributed): Slivers to keep in full.
        second (_Attributed): Slivers to append where their content is new.

    Returns:
        _Attributed: ``first``, followed by the unseen entries of ``second``.
    """
    merged = list(first)
    seen = {sliver for sliver, _node_id in first}
    for sliver, node_id in second:
        if sliver in seen:
            continue
        seen.add(sliver)
        merged.append((sliver, node_id))
    return merged


async def reconstruct_blob(
    *,
    client: ExecuteOnlyClient,
    committee: WalrusCommittee,
    blob_id: bytes,
    axis: typing.Literal["primary", "secondary"],
    threshold: int,
    metadata: VerifiedBlobMetadata,
    verify: bool = True,
    max_node_connections: int = 10,
    grace_base_seconds: float = 0.5,
    grace_factor: float = 0.5,
) -> tuple[bytes, int]:
    """Fetch slivers for ``axis`` and decode them into blob content.

    On a decode failure the slivers are verified one by one, whoever served
    an unusable one is barred, and the fan-out is run once more without
    them. See this module's docstring for why that ordering is the cheap one
    and why the retry stops at a single round.

    Args:
        client (ExecuteOnlyClient): Client used to issue sliver GETs.
        committee (WalrusCommittee): Chain-sourced committee to read from.
        blob_id (bytes): Raw 32-byte blob ID being read.
        axis (Literal["primary", "secondary"]): Sliver axis to reconstruct.
        threshold (int): Slivers the decoder needs for ``axis``.
        metadata (VerifiedBlobMetadata): Verified metadata for this blob.
        verify (bool): Whether the decode re-derives the blob ID. Sliver
            verification during recovery happens either way -- it is the
            only thing that can name the node at fault.
        max_node_connections (int): Concurrent requests allowed per node.
        grace_base_seconds (float): Straggler grace base, passed through.
        grace_factor (float): Straggler grace factor, passed through.

    Returns:
        tuple[bytes, int]: The reconstructed content, and the number of
            distinct slivers the successful decode was given.

    Raises:
        SliverFetchError: If the fan-out cannot reach ``threshold`` -- on the
            first attempt, or on the retry once bad nodes are barred.
        BlobDecodeError: If the slivers will not decode and no single sliver
            can be blamed, or if they still will not decode once the
            culprits have been replaced.
    """
    sliver_cap = _sliver_cap(metadata=metadata)
    attributed = await _fetch_slivers_attributed(
        client=client,
        committee=committee,
        blob_id=blob_id,
        axis=axis,
        threshold=threshold,
        max_sliver_bytes=sliver_cap,
        max_node_connections=max_node_connections,
        grace_base_seconds=grace_base_seconds,
        grace_factor=grace_factor,
    )
    try:
        return _decode_attributed(
            attributed=attributed, metadata=metadata, axis=axis, verify=verify
        )
    except BlobDecodeError:
        verified, indicted = _partition_by_verification(
            attributed=attributed, metadata=metadata, axis=axis
        )
        if not indicted:
            # Every sliver stands on its own, so no node can be blamed and a
            # second fan-out would fetch the same bytes to the same end. The
            # original failure is the honest answer.
            raise
        if len(verified) >= threshold:
            return _decode_attributed(
                attributed=verified, metadata=metadata, axis=axis, verify=verify
            )
        replacements = await _fetch_slivers_attributed(
            client=client,
            committee=committee,
            blob_id=blob_id,
            axis=axis,
            threshold=threshold,
            exclude_node_ids=indicted,
            max_sliver_bytes=sliver_cap,
            max_node_connections=max_node_connections,
            grace_base_seconds=grace_base_seconds,
            grace_factor=grace_factor,
        )
        return _decode_attributed(
            attributed=_merge_distinct(first=verified, second=replacements),
            metadata=metadata,
            axis=axis,
            verify=verify,
        )
