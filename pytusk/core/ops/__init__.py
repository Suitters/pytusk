#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""On-chain PTB operations for Walrus blob registration/certification and
``Storage`` object lifecycle, split into COMPOSE, EXECUTE, and READ layers.

- :mod:`pytusk.core.ops.blob_compose` / :mod:`pytusk.core.ops.tip_compose` /
  :mod:`pytusk.core.ops.storage_compose`: PURE PTB composition (``add_*``
  functions, plus the pure fuse validators). No client, no network calls,
  no submission -- see the NO-CLIENT INVARIANT documented in each module's
  own docstring.
- :mod:`pytusk.core.ops.blob_execute` / :mod:`pytusk.core.ops.storage_execute`:
  thin ``execute_*`` wrappers that open a transaction, delegate composition
  to the matching ``add_*``, then build, sign, and submit -- plus
  :func:`~pytusk.core.ops.blob_execute.preflight_sponsor` and
  :func:`~pytusk.core.ops.blob_execute.preflight_payment`, the shared
  guard/resolution steps every Tx1 caller runs before composing.
- :mod:`pytusk.core.ops.storage_reads`: client-driven read/list/parse
  helpers that compose no PTB and submit nothing, but do need a client to
  query owned objects.
- :mod:`pytusk.core.ops.system_reads`: client-driven package-ID resolution
  and transaction-finality polling -- see that module's docstring for why
  these live here rather than in the client-free
  :mod:`pytusk.core.chain` package.
- :mod:`pytusk.core.ops.coins`: client-driven WAL coin selection and
  validation.
- :mod:`pytusk.core.ops.shared_blob_compose` / :mod:`pytusk.core.ops.shared_blob_execute`:
  ``SharedBlob`` creation, funding, and extension -- same COMPOSE/EXECUTE
  split as the blob and storage modules above.

Every public name from all eight submodules is re-exported here so callers
can import from :mod:`pytusk.core.ops` directly without knowing which
submodule a given function lives in.
"""

from pytusk.core.ops.blob_compose import (
    add_certify,
    add_registration_sequence,
    add_reserve_and_register,
)
from pytusk.core.ops.blob_execute import (
    CertificationOutcome,
    execute_certify,
    execute_registration_txn,
    execute_reserve_and_register,
    preflight_payment,
    preflight_sponsor,
    submit_certification,
)
from pytusk.core.ops.blob_metadata_compose import (
    add_drop_blob_metadata_all,
    add_drop_blob_metadata_keys,
    add_set_blob_metadata,
)
from pytusk.core.ops.blob_metadata_execute import (
    execute_drop_blob_metadata_all,
    execute_drop_blob_metadata_keys,
    execute_set_blob_metadata,
    validate_blob_metadata_exists,
    validate_blob_metadata_keys_exist,
)
from pytusk.core.ops.coins import (
    assert_coin_usable,
    matches_wal_coin_type,
    prepare_wal_coin_for_amount,
    select_wal_payment_coin,
    wal_balance_and_decimals,
)
from pytusk.core.ops.shared_blob_compose import (
    add_extend_shared_blob,
    add_fund_shared_blob,
    add_share_blob,
)
from pytusk.core.ops.shared_blob_execute import (
    execute_extend_shared_blob,
    execute_fund_shared_blob,
    execute_share_blob,
)
from pytusk.core.ops.storage_compose import (
    add_destroy_storage,
    add_fuse,
    add_split_by_epoch,
    add_split_by_size,
    fuse_incompatibility,
    fuse_periods_incompatibility,
    validate_fuse_pair,
)
from pytusk.core.ops.storage_execute import (
    execute_destroy_storage,
    execute_fuse,
    execute_split_by_epoch,
    execute_split_by_size,
)
from pytusk.core.ops.storage_reads import (
    list_storage_objects,
    storage_from_blob,
    storage_from_object,
)
from pytusk.core.ops.system_reads import (
    DEFAULT_FINALITY_MAX_ATTEMPTS,
    DEFAULT_FINALITY_MAX_DELAY,
    ChainContext,
    prepare_chain_context,
    resolve_package_id,
    wait_for_finality,
)
from pytusk.core.ops.tip_compose import add_tip

__all__ = [
    "DEFAULT_FINALITY_MAX_ATTEMPTS",
    "DEFAULT_FINALITY_MAX_DELAY",
    "CertificationOutcome",
    "ChainContext",
    "add_certify",
    "add_destroy_storage",
    "add_drop_blob_metadata_all",
    "add_drop_blob_metadata_keys",
    "add_extend_shared_blob",
    "add_fund_shared_blob",
    "add_fuse",
    "add_registration_sequence",
    "add_reserve_and_register",
    "add_set_blob_metadata",
    "add_share_blob",
    "add_split_by_epoch",
    "add_split_by_size",
    "add_tip",
    "assert_coin_usable",
    "execute_certify",
    "execute_destroy_storage",
    "execute_drop_blob_metadata_all",
    "execute_drop_blob_metadata_keys",
    "execute_extend_shared_blob",
    "execute_fund_shared_blob",
    "execute_fuse",
    "execute_registration_txn",
    "execute_reserve_and_register",
    "execute_set_blob_metadata",
    "execute_share_blob",
    "execute_split_by_epoch",
    "execute_split_by_size",
    "fuse_incompatibility",
    "fuse_periods_incompatibility",
    "list_storage_objects",
    "matches_wal_coin_type",
    "preflight_payment",
    "preflight_sponsor",
    "prepare_chain_context",
    "prepare_wal_coin_for_amount",
    "resolve_package_id",
    "select_wal_payment_coin",
    "storage_from_blob",
    "storage_from_object",
    "submit_certification",
    "validate_blob_metadata_exists",
    "validate_blob_metadata_keys_exist",
    "validate_fuse_pair",
    "wait_for_finality",
    "wal_balance_and_decimals",
]
