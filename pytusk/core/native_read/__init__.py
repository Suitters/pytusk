#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Native Walrus read orchestration: metadata fetch/verify and sliver fan-out.

The native read order is FIXED and must not be rearranged::

    fetch+verify metadata -> fetch slivers (one axis, to threshold) -> decode

The metadata must come first because it is the only source of the blob's
unencoded length and the only thing that can verify a reconstruction; the
sliver fetch needs the caller's decoding threshold, which is derived from
the on-chain committee's shard count. This package owns the network stages,
and -- in :mod:`pytusk.core.native_read.reconstruct` -- the one place where
that order loops rather than running straight through: a failed decode is
what identifies a node serving bad slivers, so recovery necessarily
interleaves the fetch and decode stages. Decoding itself still belongs to
:mod:`pytusk.core.encoding.redstuff`. What lives ABOVE this package, in
:mod:`pytusk.core.pipelines.read`, is the end-to-end sequence and the shape
of the result; this package owns the stages and that single recovery loop.

TWO INDEX SPACES ARE IN PLAY and confusing them yields a wrong URL with no
error: a committee addresses nodes by SHARD index
(:attr:`~pytusk.core.chain.WalrusCommitteeMember.shard_indices`), while a
storage-node sliver URL addresses slivers by SLIVER-PAIR index. They differ
by a per-blob rotation -- see
:func:`~pytusk.core.encoding.shard_index_to_pair_index`, which this package
applies at every sliver GET.

Unlike the write path, this fan-out does NOT retry a failing node -- but it
does EXCLUDE one. A write must eventually place slivers on every node it is
responsible for, so a failure there is worth retrying; a read only needs
ENOUGH slivers, so a node that fails to RESPOND is simply skipped and its
work is covered by the others. A node that responds with unusable BYTES is
a different case, and is NOT covered by the others: its bad sliver aborts
the whole decode batch. Such a node is identified after the failed decode,
barred, and the fan-out is run once more without it. See
:mod:`pytusk.core.native_read.reconstruct`.
"""

from pytusk.core.native_read.fetch import fetch_slivers
from pytusk.core.native_read.metadata import fetch_verified_metadata
from pytusk.core.native_read.reconstruct import reconstruct_blob

__all__ = [
    "fetch_slivers",
    "fetch_verified_metadata",
    "reconstruct_blob",
]
