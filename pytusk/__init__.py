#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Pytusk package."""

import logging

# Client
from pytusk.client.walrus_client import WalrusClient, get_walrus_epoch

# Storage-node commands
from pytusk.commands.node_commands import (
    GetStorageConfirmation,
    MetadataAck,
    PutMetadata,
    PutSliver,
    SignedConfirmation,
    SliverAck,
)

# Read commands
from pytusk.commands.read_commands import (
    ConcatBlobs,
    ReadBlob,
    ReadBlobByObjectId,
    ReadBlobPartial,
    ReadQuiltPatch,
)

# Relay commands (upload-relay tip config and blob upload)
from pytusk.commands.relay_commands import (
    GetTipConfig,
    RelayUploadAck,
    UploadRelayBlob,
)

# Command base and response types
from pytusk.commands.walrus_command import (
    BlobData,
    BlobReceipt,
    BlobSlice,
    QuiltPatch,
    QuiltReceipt,
    WalrusCommand,
    error_reason,
    http_failure_message,
)

# Write commands
from pytusk.commands.write_commands import (
    StoreBlob,
    StoreQuilt,
)

# Configuration
from pytusk.config.tusk_config import (
    NetworkType,
    PytuskConfigModel,
    PytuskConfiguration,
    WalrusNetworkConfig,
)

# Certification (certify_blob wire-format: bitmap, quorum, verify, aggregate)
from pytusk.core.certification import (
    Certificate,
    ConfirmationMismatchError,
    InvalidConfirmationError,
    NodeConfirmation,
    QuorumNotReachedError,
    build_certificate,
    confirmation_message,
    is_quorum,
    min_weight_for_quorum,
    verify_certificate,
    verify_confirmation,
)

# Chain (committee resolution/epoch, transaction-effects inspection --
# client-free)
from pytusk.core.chain import (
    BalanceChangeCosts,
    ChainReader,
    WalrusCommittee,
    WalrusCommitteeMember,
    blob_certified_epoch,
    blob_deletable_and_end_epoch,
    extract_balance_change_costs,
    fetch_committee,
    fetch_epoch,
    find_created_object_id,
    pack_signers_bitmap,
    require_success,
    unpack_signers_bitmap,
)

# Encoding (RedStuff, blob-ID and root-hash conversions)
from pytusk.core.encoding import (
    RS2_ENCODING_TYPE,
    RS2_MAX_SYMBOL_SIZE,
    RS2_REQUIRED_ALIGNMENT,
    BlobTooLargeError,
    EncodedBlob,
    blob_id_from_url_base64,
    blob_id_to_u256,
    blob_id_to_url_base64,
    decode_standard_base64,
    encode_blob,
    encoded_blob_length,
    max_blob_size,
    max_n_faulty,
    metadata_length,
    min_n_correct,
    root_hash_to_u256,
    source_symbol_counts,
    symbol_size,
)

# Native upload orchestration (sliver fan-out, confirmation collection,
# certification, thin end-to-end compose)
from pytusk.core.native_upload import (
    CertifyTransactionError,
    ConfirmationCollectionError,
    EpochMismatchError,
    FanoutReport,
    NativeBlobReceipt,
    NativeUploadError,
    NodeUploadOutcome,
    SliverUploadError,
    StageTimings,
    assert_certificate_epoch_current,
    certify,
    collect_confirmations,
    object_id_to_raw_bytes,
    upload_slivers,
)

# Storage-object lifecycle (split, fuse, reclaim, listing; same
# caller-owns-the-transaction model -- see pytusk.core.ops's module
# docstring)
# System operations (Tx1 reserve_space+register_blob and Tx2 certify_blob
# PTB composition; the caller owns the transaction lifecycle -- see
# pytusk.core.ops's module docstring)
# Shared ops helpers (chain resolution, finality, WAL coin selection --
# client-taking)
from pytusk.core.ops import (
    DEFAULT_FINALITY_MAX_ATTEMPTS,
    DEFAULT_FINALITY_MAX_DELAY,
    add_certify,
    add_destroy_storage,
    add_fuse,
    add_reserve_and_register,
    add_split_by_epoch,
    add_split_by_size,
    execute_certify,
    execute_destroy_storage,
    execute_fuse,
    execute_reserve_and_register,
    execute_split_by_epoch,
    execute_split_by_size,
    fuse_incompatibility,
    fuse_periods_incompatibility,
    list_storage_objects,
    matches_wal_coin_type,
    resolve_package_id,
    select_wal_payment_coin,
    storage_from_blob,
    storage_from_object,
    validate_fuse_pair,
    wait_for_finality,
    wal_balance_and_decimals,
)

# End-to-end write pipelines (the compose functions that own stage order and
# receipt construction -- see pytusk.core.pipelines's module docstring)
from pytusk.core.pipelines import store_blob_native
from pytusk.core.types import (
    CertifyResult,
    Registration,
    RegistrationPendingError,
    SplitResult,
    StorageObject,
    StorageOpResult,
)
from pytusk.version import __version__

# pytusk is a LIBRARY: it only emits log records, never configures
# handlers, levels, or any other global logging state. A NullHandler on
# the top-level "pytusk" logger is the standard library idiom for this --
# it silences Python's "No handlers could be found for logger pytusk"
# warning for an SDK user who configures nothing, without imposing any
# output of pytusk's own choosing. Whoever codes to this SDK (e.g. the
# tusky CLI in pytusk/tusky/, or any other application) owns handler
# setup, destinations, and rotation for their own process.
logging.getLogger(__name__).addHandler(logging.NullHandler())

# `__all__` is deliberately grouped by source module rather than alphabetically
# sorted; ruff's auto-fix would flatten it into one global alpha sort and
# scatter these grouping comments onto unrelated entries. Note that the groups
# are NOT in the same order as the import blocks above -- only the grouping is
# shared, not the ordering.
__all__ = [  # noqa: RUF022
    "__version__",
    # Configuration
    "NetworkType",
    "WalrusNetworkConfig",
    "PytuskConfigModel",
    "PytuskConfiguration",
    # Client
    "WalrusClient",
    "get_walrus_epoch",
    # Chain (client-free)
    "BalanceChangeCosts",
    "ChainReader",
    "WalrusCommittee",
    "WalrusCommitteeMember",
    "blob_certified_epoch",
    "blob_deletable_and_end_epoch",
    "extract_balance_change_costs",
    "fetch_committee",
    "fetch_epoch",
    "find_created_object_id",
    "pack_signers_bitmap",
    "require_success",
    "unpack_signers_bitmap",
    # Command base and response types
    "WalrusCommand",
    "BlobData",
    "BlobSlice",
    "QuiltPatch",
    "BlobReceipt",
    "QuiltReceipt",
    "error_reason",
    "http_failure_message",
    # Read commands
    "ReadBlob",
    "ReadBlobPartial",
    "ReadBlobByObjectId",
    "ReadQuiltPatch",
    "ConcatBlobs",
    # Relay commands
    "GetTipConfig",
    "RelayUploadAck",
    "UploadRelayBlob",
    # Write commands
    "StoreBlob",
    "StoreQuilt",
    # Storage-node commands
    "PutSliver",
    "PutMetadata",
    "GetStorageConfirmation",
    "SliverAck",
    "MetadataAck",
    "SignedConfirmation",
    # Encoding
    "RS2_ENCODING_TYPE",
    "RS2_MAX_SYMBOL_SIZE",
    "RS2_REQUIRED_ALIGNMENT",
    "BlobTooLargeError",
    "EncodedBlob",
    "blob_id_from_url_base64",
    "blob_id_to_u256",
    "blob_id_to_url_base64",
    "decode_standard_base64",
    "encode_blob",
    "encoded_blob_length",
    "max_blob_size",
    "max_n_faulty",
    "metadata_length",
    "min_n_correct",
    "root_hash_to_u256",
    "source_symbol_counts",
    "symbol_size",
    # Certification
    "Certificate",
    "ConfirmationMismatchError",
    "InvalidConfirmationError",
    "NodeConfirmation",
    "QuorumNotReachedError",
    "build_certificate",
    "confirmation_message",
    "is_quorum",
    "min_weight_for_quorum",
    "verify_certificate",
    "verify_confirmation",
    # Storage-object operations
    "SplitResult",
    "StorageObject",
    "StorageOpResult",
    "add_destroy_storage",
    "add_fuse",
    "add_split_by_epoch",
    "add_split_by_size",
    "execute_destroy_storage",
    "execute_fuse",
    "execute_split_by_epoch",
    "execute_split_by_size",
    "fuse_incompatibility",
    "fuse_periods_incompatibility",
    "list_storage_objects",
    "storage_from_blob",
    "storage_from_object",
    "validate_fuse_pair",
    # System operations
    "CertifyResult",
    "Registration",
    "RegistrationPendingError",
    "add_certify",
    "add_reserve_and_register",
    "execute_certify",
    "execute_reserve_and_register",
    # Shared ops helpers (client-taking)
    "DEFAULT_FINALITY_MAX_ATTEMPTS",
    "DEFAULT_FINALITY_MAX_DELAY",
    "matches_wal_coin_type",
    "resolve_package_id",
    "select_wal_payment_coin",
    "wait_for_finality",
    "wal_balance_and_decimals",
    # Native upload orchestration
    "CertifyTransactionError",
    "ConfirmationCollectionError",
    "EpochMismatchError",
    "FanoutReport",
    "NativeBlobReceipt",
    "NativeUploadError",
    "NodeUploadOutcome",
    "SliverUploadError",
    "StageTimings",
    "assert_certificate_epoch_current",
    "certify",
    "collect_confirmations",
    "object_id_to_raw_bytes",
    "store_blob_native",
    "upload_slivers",
]
