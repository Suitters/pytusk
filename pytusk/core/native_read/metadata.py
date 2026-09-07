#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Metadata stage of the native read pipeline.

See :mod:`pytusk.core.native_read` for the fixed stage order.
"""

import logging
import random

from pytusk.commands.node_commands import GetMetadata, MetadataData
from pytusk.core.chain import WalrusCommittee
from pytusk.core.encoding import (
    MetadataVerificationError,
    VerifiedBlobMetadata,
    blob_id_to_url_base64,
    metadata_length,
    verify_blob_metadata,
)
from pytusk.core.types import ExecuteOnlyClient, MetadataFetchError

_logger = logging.getLogger(__name__)

_METADATA_CAP_SLACK: int = 4096
"""Headroom added to the expected metadata length before it becomes a cap.

The cap exists to stop a node answering a ~64 KB request with a gigabyte,
not to assert an exact length. The wire form is the OUTER
``BlobMetadataWithId``, which carries framing beyond what
:func:`metadata_length` accounts for, and a bound that doubles as an
equality check turns any future framing change into an outage.
"""


async def fetch_verified_metadata(
    *,
    client: ExecuteOnlyClient,
    committee: WalrusCommittee,
    blob_id: bytes,
) -> VerifiedBlobMetadata:
    """Fetch blob metadata from the committee and verify it.

    Nodes are tried one at a time until one supplies metadata that both
    verifies internally AND carries the blob ID that was asked for. The
    starting node is chosen at random: iterating from ``members[0]`` every
    time would make the first committee member serve nearly every metadata
    read.

    The blob-ID comparison is NOT optional. Verification alone proves only
    that the metadata is internally consistent -- a hostile node can return
    perfectly valid metadata for a DIFFERENT blob. Comparing against the
    caller's chain-sourced ``blob_id`` is what binds the read to the blob
    actually requested.

    ``n_shards`` is taken from ``committee``, never from a node response --
    it is a trust boundary for the verification that follows.

    Args:
        client (ExecuteOnlyClient): Client used to issue the metadata GET.
        committee (WalrusCommittee): Chain-sourced committee, supplying both
            the nodes to ask and the authoritative shard count.
        blob_id (bytes): Raw 32-byte blob ID being read.

    Returns:
        VerifiedBlobMetadata: Verified metadata for ``blob_id``.

    Raises:
        MetadataFetchError: If no node supplied verifiable, matching
            metadata.
    """
    members = committee.members
    if not members:
        raise MetadataFetchError(
            message="Committee has no members to read metadata from",
            stage="fetch_metadata",
        )
    metadata_cap = metadata_length(n_shards=committee.n_shards) + _METADATA_CAP_SLACK
    start = random.randrange(len(members))
    attempts = 0
    for offset in range(len(members)):
        member = members[(start + offset) % len(members)]
        attempts += 1
        result = await client.execute(
            command=GetMetadata(blob_id=blob_id, max_bytes=metadata_cap),
            base_url=member.base_url,
        )
        if not result.is_ok():
            continue
        payload = result.result_data
        if not isinstance(payload, MetadataData):
            continue
        try:
            metadata = verify_blob_metadata(
                metadata_bcs=payload.content, n_shards=committee.n_shards
            )
        except MetadataVerificationError:
            continue
        if metadata.blob_id != blob_id:
            _logger.warning(
                "node %s returned metadata for a different blob: asked %s, got %s",
                member.node_id,
                blob_id_to_url_base64(blob_id=blob_id),
                blob_id_to_url_base64(blob_id=metadata.blob_id),
            )
            continue
        return metadata
    raise MetadataFetchError(
        message=(
            f"No storage node supplied verifiable metadata for blob "
            f"{blob_id_to_url_base64(blob_id=blob_id)} "
            f"after {attempts} node(s)"
        ),
        stage="fetch_metadata",
    )
