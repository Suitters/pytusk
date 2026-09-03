#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Public surface of the chain package: client-free on-chain state helpers.

Everything in this package reads or interprets already-fetched chain state
and never touches ``WalrusClient`` -- that is the organizing rule of this
package, not an incidental property of it. :mod:`pytusk.client.walrus_client`
imports FROM this package (for :class:`WalrusCommittee` and
:func:`fetch_committee`/:func:`fetch_epoch`), so the reverse dependency would
be a real import cycle, not just a layering smell. Client-taking helpers that
need the same chain concepts (``resolve_package_id``, ``wait_for_finality``,
WAL coin selection) live one layer up, in :mod:`pytusk.core.ops`.

:mod:`pytusk.core.chain.committee` resolves the Walrus committee (epoch,
member ordering, shard assignment) from on-chain staking state, and
re-exports :func:`~pytusk.core.certification.pack_signers_bitmap` /
:func:`~pytusk.core.certification.unpack_signers_bitmap` from
:mod:`pytusk.core.certification` for back-compat (see that module's own
docstring for why certification.py, not this package, owns them).
:mod:`pytusk.core.chain.effects` inspects a transaction's already-fetched
effects/result -- no I/O of its own -- and reads spend costs out of its
balance changes. :mod:`pytusk.core.chain.blob_fields` extracts fields from an
already-fetched Walrus ``Blob`` object; both moved here from the tusky CLI at
Plan #28 step 10 under the placement rule, being domain logic an SDK user
wants rather than CLI presentation. :mod:`pytusk.core.chain.coin_types`
holds :func:`~pytusk.core.chain.coin_types.matches_wal_coin_type`, moved
here from :mod:`pytusk.core.ops.coins` at Plan #28's architect review since
it is client-free and :mod:`pytusk.core.chain.effects` needed it without a
function-local import to dodge the ``chain -> ops -> client -> chain`` cycle.
:mod:`pytusk.core.chain.blob_metadata` reads a Blob's on-chain metadata
(Walrus "attribute") dynamic field, following the same list-and-filter
``GetDynamicFields`` pattern as :func:`~pytusk.core.chain.committee.fetch_staking_inner`.
"""

from pytusk.core.chain.blob_fields import (
    blob_certified_epoch,
    blob_deletable_and_end_epoch,
    blob_id_from_object,
)
from pytusk.core.chain.blob_metadata import fetch_blob_metadata
from pytusk.core.chain.coin_types import matches_wal_coin_type
from pytusk.core.chain.committee import (
    ChainReader,
    WalrusCommittee,
    WalrusCommitteeMember,
    fetch_committee,
    fetch_epoch,
    pack_signers_bitmap,
    unpack_signers_bitmap,
)
from pytusk.core.chain.effects import (
    BalanceChangeCosts,
    extract_balance_change_costs,
    find_created_object_id,
    find_created_shared_object_id,
    require_success,
)

__all__ = [
    "BalanceChangeCosts",
    "ChainReader",
    "WalrusCommittee",
    "WalrusCommitteeMember",
    "blob_certified_epoch",
    "blob_deletable_and_end_epoch",
    "blob_id_from_object",
    "extract_balance_change_costs",
    "fetch_blob_metadata",
    "fetch_committee",
    "fetch_epoch",
    "find_created_object_id",
    "find_created_shared_object_id",
    "matches_wal_coin_type",
    "pack_signers_bitmap",
    "require_success",
    "unpack_signers_bitmap",
]
