#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""End-to-end native read pipeline.

The inverse of :mod:`pytusk.core.pipelines.write`: it reconstructs a blob
from the storage nodes directly, without an aggregator standing in the
middle. Like the write pipelines, this module OWNS THE ORDER and owns the
result; it does not own the stages. Metadata retrieval and the sliver
fan-out live in :mod:`pytusk.core.native_read`, the decode and the BFT
thresholds in :mod:`pytusk.core.encoding`. What is left here is the
sequence, the choice of threshold for the requested axis, and the mapping
from stage outcomes to a :class:`~pytusk.core.types.NativeReadResult`.

THERE IS NO ERROR BOUNDARY HERE, and that is the whole difference from the
write side. A read spends no WAL, registers nothing, and leaves no on-chain
state behind, so there is never a partial success worth returning on a
receipt. Every failure RAISES. The write pipelines' raise-then-return split
exists to avoid stranding storage a caller has already paid for; a read has
nothing to strand, and giving it a "failed" result object would hand callers
a success-shaped value carrying no content.

IT RESOLVES ONLY THE COMMITTEE, not a full chain context.
:func:`~pytusk.core.ops.system_reads.prepare_chain_context` also resolves the
package ID, which costs an additional object read and exists so a caller can
COMPOSE A TRANSACTION. This pipeline composes none. Paying for that read on
every blob read, to discard it, is the reuse trade going the wrong way -- so
the four lines of ``ChainContextError`` translation are duplicated here
deliberately rather than inherited along with a round trip.

THE EPOCH IS NOT RE-CHECKED against the chain before decoding. The write
path compares :func:`~pytusk.client.walrus_client.WalrusClient.walrus_epoch`
against ``committee.epoch`` before certifying because a certificate's
signer bitmap is bound to committee ordering and a stale one is rejected
on-chain. A read has no such consequence: a committee that has gone stale
simply fails to yield enough slivers and raises
:class:`~pytusk.core.types.SliverFetchError`, which is self-correcting on the
caller's next attempt. Adding the check would cost a chain round trip per
read and prevent nothing.
"""

import typing

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.encoding import source_symbol_counts
from pytusk.core.native_read import fetch_verified_metadata, reconstruct_blob
from pytusk.core.types.errors import NativeReadError
from pytusk.core.types.receipts import NativeReadResult


async def read_blob_native(
    *,
    client: WalrusClient,
    blob_id: bytes,
    axis: typing.Literal["primary", "secondary"] = "primary",
    verify: bool = True,
    max_node_connections: int = 10,
    grace_base_seconds: float = 0.5,
    grace_factor: float = 0.5,
) -> NativeReadResult:
    """Read a blob by reconstructing it from storage-node slivers.

    Resolves the current committee, fetches and verifies the blob metadata
    from any node that will serve it, fans out for slivers until the
    decoding threshold for ``axis`` is met, and decodes.

    Args:
        client (WalrusClient): Client used for both the chain read and the
            storage-node requests.
        blob_id (bytes): The 32-byte blob id to read.
        axis (Literal["primary", "secondary"]): Which sliver family to
            reconstruct from. ``"primary"`` needs roughly half as many
            slivers and is the default; ``"secondary"`` exists for the case
            where primary slivers are unavailable.
        verify (bool): When True the decoder re-derives the blob id from the
            decoded content and rejects a mismatch. Passing False loses ALL
            content authentication of the slivers themselves — metadata
            verification only authenticates the metadata, not the sliver
            bytes used to reconstruct the blob.
        max_node_connections (int): Ceiling on concurrent storage-node
            requests during the sliver fan-out.
        grace_base_seconds (float): Base grace period before the fan-out
            stops waiting on stragglers once it can proceed.
        grace_factor (float): Multiplier applied to the grace period.

    Returns:
        NativeReadResult: The reconstructed content plus the epoch, axis and
        sliver count it was rebuilt from.

    Raises:
        ValueError: If ``blob_id`` is not exactly 32 bytes. Raised before any
            network request, since it is a caller error rather than a read
            failure.
        NativeReadError: If the committee could not be resolved. ``stage``
            names which chain read failed.
        MetadataFetchError: If no node served metadata that both verified and
            matched ``blob_id``.
        SliverFetchError: If too few nodes answered to reach the decoding
            threshold for ``axis``.
        BlobDecodeError: If the decoder rejected the slivers or the
            reconstructed content failed its blob-id check -- after the
            single recovery round described in
            :mod:`pytusk.core.native_read.reconstruct` has had its chance.
    """
    if len(blob_id) != 32:
        raise ValueError(f"blob_id must be 32 bytes, got {len(blob_id)}")

    try:
        committee = await client.committee()
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        raise NativeReadError(message=str(exc), stage="committee") from exc

    metadata = await fetch_verified_metadata(
        client=client, committee=committee, blob_id=blob_id
    )

    # source_symbol_counts returns (primary, secondary) -- the count of
    # source symbols per axis, which IS the sliver count the decoder needs
    # for that axis. Taking the wrong element under-fetches for secondary
    # and the failure surfaces as a decode error, not as a shortfall.
    primary_threshold, secondary_threshold = source_symbol_counts(
        n_shards=committee.n_shards
    )
    threshold = primary_threshold if axis == "primary" else secondary_threshold

    # Every sliver the fan-out collected is fed to the decoder, including any
    # that arrived past the threshold -- they are already paid for, and
    # discarding them to hit an exact count would only narrow the margin the
    # decoder has to work with.
    content, slivers_used = await reconstruct_blob(
        client=client,
        committee=committee,
        blob_id=blob_id,
        axis=axis,
        threshold=threshold,
        metadata=metadata,
        verify=verify,
        max_node_connections=max_node_connections,
        grace_base_seconds=grace_base_seconds,
        grace_factor=grace_factor,
    )

    return NativeReadResult(
        content=content,
        blob_id=blob_id,
        epoch=committee.epoch,
        axis=axis,
        slivers_used=slivers_used,
    )
