#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Pytusk package."""

import logging

# Client
from pytusk.client.walrus_client import WalrusClient, get_walrus_epoch

# Storage-node commands
from pytusk.commands.node_commands import (
    GetMetadata,
    GetSliver,
    GetStorageConfirmation,
    MetadataAck,
    MetadataData,
    PutMetadata,
    PutSliver,
    SignedConfirmation,
    SliverAck,
    SliverData,
)

# Read commands
from pytusk.commands.read_commands import (
    ConcatBlobs,
    ListQuiltPatches,
    ReadBlob,
    ReadBlobByObjectId,
    ReadBlobPartial,
    ReadQuiltPatch,
    ReadQuiltPatchById,
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
    QuiltPatchItem,
    QuiltPatchListing,
    QuiltReceipt,
    StorageNodeEnvelopeError,
    WalrusCommand,
    error_reason,
    http_failure_message,
    unwrap_storage_node_envelope,
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
    RelayConfig,
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
    is_above_validity,
    is_quorum,
    min_weight_for_quorum,
    min_weight_for_validity,
    pack_signers_bitmap,
    unpack_signers_bitmap,
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
    blob_id_from_object,
    extract_balance_change_costs,
    fetch_blob_metadata,
    fetch_committee,
    fetch_epoch,
    find_created_object_id,
    find_created_shared_object_id,
    matches_wal_coin_type,
    require_success,
)
from pytusk.core.chain.events import event_object_id, fetch_event_object_id

# Encoding (RedStuff, blob-ID and root-hash conversions)
from pytusk.core.encoding import (
    QUILT_BLOB_ATTRIBUTES,
    RS2_ENCODING_TYPE,
    RS2_MAX_SYMBOL_SIZE,
    RS2_REQUIRED_ALIGNMENT,
    BlobDecodeError,
    BlobTooLargeError,
    EncodedBlob,
    MetadataVerificationError,
    QuiltAssemblyError,
    SliverVerificationError,
    VerifiedBlobMetadata,
    assemble_quilt,
    blob_id_from_url_base64,
    blob_id_to_u256,
    blob_id_to_url_base64,
    decode_blob,
    decode_standard_base64,
    encode_blob,
    encoded_blob_length,
    max_blob_size,
    max_n_faulty,
    metadata_length,
    min_n_correct,
    object_id_to_raw_bytes,
    pair_index_to_shard_index,
    quilt_patch_id,
    root_hash_to_u256,
    rotation_offset,
    shard_index_to_pair_index,
    source_symbol_counts,
    symbol_size,
    validate_quilt_identifier,
    verify_blob_metadata,
    verify_sliver,
)

# Native read orchestration (metadata retrieval and verification, sliver
# fan-out, thin end-to-end compose that reconstructs a blob from the
# storage nodes without an aggregator)
from pytusk.core.native_read import (
    fetch_slivers,
    fetch_verified_metadata,
    reconstruct_blob,
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
    ChainContext,
    add_certify,
    add_destroy_storage,
    add_drop_blob_metadata_all,
    add_drop_blob_metadata_keys,
    add_extend_shared_blob,
    add_fund_shared_blob,
    add_fuse,
    add_registration_sequence,
    add_reserve_and_register,
    add_set_blob_metadata,
    add_share_blob,
    add_split_by_epoch,
    add_split_by_size,
    add_tip,
    execute_certify,
    execute_destroy_storage,
    execute_drop_blob_metadata_all,
    execute_drop_blob_metadata_keys,
    execute_extend_shared_blob,
    execute_fund_shared_blob,
    execute_fuse,
    execute_reserve_and_register,
    execute_set_blob_metadata,
    execute_share_blob,
    execute_split_by_epoch,
    execute_split_by_size,
    fuse_incompatibility,
    fuse_periods_incompatibility,
    list_storage_objects,
    preflight_payment,
    preflight_sponsor,
    prepare_chain_context,
    prepare_wal_coin_for_amount,
    resolve_package_id,
    select_wal_payment_coin,
    storage_from_blob,
    storage_from_object,
    validate_blob_metadata_exists,
    validate_blob_metadata_keys_exist,
    validate_fuse_pair,
    wait_for_finality,
    wal_balance_and_decimals,
)
from pytusk.core.ops.blob_status import fetch_blob_status, resolve_blob_sui_objects

# End-to-end write pipelines (the compose functions that own stage order and
# receipt construction -- see pytusk.core.pipelines's module docstring)
from pytusk.core.pipelines import (
    read_blob_native,
    store_blob_native,
    store_blob_relay,
    store_quilt_relay,
)

# Relay upload orchestration (tip quoting/payment, relay POST, confirmation
# certificate, thin end-to-end compose)
from pytusk.core.relay_upload import (
    DEFAULT_MAX_UPLOAD_ATTEMPTS,
    RelayBlobReceipt,
    RelayCertificateParseError,
    RelayCertifyTransactionError,
    RelayOutcome,
    RelayStageTimings,
    RelayUploadError,
    TipCeilingExceededError,
    TipConfigError,
    TipPaymentError,
    assert_tip_within_ceiling,
    build_auth_package,
    parse_relay_certificate,
    quote_tip,
    upload_to_relay,
)
from pytusk.core.types import (
    FROM_GAS,
    AssembledQuilt,
    AuthPackage,
    BlobMetadata,
    BlobMetadataOpResult,
    BlobStatus,
    BlobStatusReport,
    CertifyResult,
    ChainContextError,
    CommitteeAndNodeClient,
    ConstTip,
    DeletableCounts,
    DeletableStatus,
    DissentReason,
    EventRef,
    ExecuteOnlyClient,
    InvalidStatus,
    LinearTip,
    Metadata,
    MetadataFetchError,
    NativeReadError,
    NativeReadResult,
    NodeDissent,
    NodeRef,
    NonexistentStatus,
    PermanentStatus,
    QuiltPatchInput,
    QuiltPatchLayout,
    QuiltPatchReceipt,
    QuiltRelayReceipt,
    Registration,
    RegistrationPendingError,
    RelayUploadOutcome,
    RelayUploadResult,
    Resolution,
    SharedBlobOpResult,
    SharedBlobReceipt,
    SliverFetchError,
    SplitResult,
    StageTimingsProtocol,
    StorageObject,
    StorageOpResult,
    TipComposition,
    TipConfig,
    TipKind,
    TipQuote,
    TipResult,
    UnresolvedStatus,
    UploadReceipt,
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
    "RelayConfig",
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
    "blob_id_from_object",
    "extract_balance_change_costs",
    "fetch_blob_metadata",
    "fetch_committee",
    "fetch_epoch",
    "find_created_object_id",
    "find_created_shared_object_id",
    "require_success",
    # Command base and response types
    "StorageNodeEnvelopeError",
    "WalrusCommand",
    "BlobData",
    "BlobSlice",
    "QuiltPatch",
    "QuiltPatchItem",
    "QuiltPatchListing",
    "BlobReceipt",
    "QuiltReceipt",
    "error_reason",
    "http_failure_message",
    "unwrap_storage_node_envelope",
    # Read commands
    "ReadBlob",
    "ReadBlobPartial",
    "ReadBlobByObjectId",
    "ReadQuiltPatch",
    "ReadQuiltPatchById",
    "ListQuiltPatches",
    "ConcatBlobs",
    # Relay commands
    "GetTipConfig",
    "RelayUploadAck",
    "UploadRelayBlob",
    # Write commands
    "StoreBlob",
    "StoreQuilt",
    # Storage-node commands
    "GetMetadata",
    "GetSliver",
    "GetStorageConfirmation",
    "MetadataAck",
    "MetadataData",
    "PutMetadata",
    "PutSliver",
    "SignedConfirmation",
    "SliverAck",
    "SliverData",
    # Encoding
    "RS2_ENCODING_TYPE",
    "RS2_MAX_SYMBOL_SIZE",
    "RS2_REQUIRED_ALIGNMENT",
    "BlobDecodeError",
    "BlobTooLargeError",
    "EncodedBlob",
    "MetadataVerificationError",
    "SliverVerificationError",
    "VerifiedBlobMetadata",
    "BlobMetadata",
    "BlobStatus",
    "BlobStatusReport",
    "DeletableCounts",
    "DeletableStatus",
    "DissentReason",
    "EventRef",
    "InvalidStatus",
    "Metadata",
    "NodeDissent",
    "NodeRef",
    "NonexistentStatus",
    "PermanentStatus",
    "Resolution",
    "UnresolvedStatus",
    "blob_id_from_url_base64",
    "event_object_id",
    "fetch_blob_status",
    "fetch_event_object_id",
    "resolve_blob_sui_objects",
    "blob_id_to_u256",
    "blob_id_to_url_base64",
    "decode_blob",
    "decode_standard_base64",
    "QUILT_BLOB_ATTRIBUTES",
    "QuiltAssemblyError",
    "assemble_quilt",
    "encode_blob",
    "encoded_blob_length",
    "quilt_patch_id",
    "max_blob_size",
    "max_n_faulty",
    "metadata_length",
    "min_n_correct",
    "object_id_to_raw_bytes",
    "pair_index_to_shard_index",
    "root_hash_to_u256",
    "rotation_offset",
    "shard_index_to_pair_index",
    "source_symbol_counts",
    "symbol_size",
    "verify_blob_metadata",
    "verify_sliver",
    "validate_quilt_identifier",
    # Certification
    "Certificate",
    "ConfirmationMismatchError",
    "InvalidConfirmationError",
    "NodeConfirmation",
    "QuorumNotReachedError",
    "build_certificate",
    "confirmation_message",
    "is_above_validity",
    "is_quorum",
    "min_weight_for_quorum",
    "min_weight_for_validity",
    "pack_signers_bitmap",
    "unpack_signers_bitmap",
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
    # Blob metadata operations
    "BlobMetadataOpResult",
    "add_drop_blob_metadata_all",
    "add_drop_blob_metadata_keys",
    "add_set_blob_metadata",
    "execute_drop_blob_metadata_all",
    "execute_drop_blob_metadata_keys",
    "execute_set_blob_metadata",
    "validate_blob_metadata_exists",
    "validate_blob_metadata_keys_exist",
    # System operations
    "CertifyResult",
    "Registration",
    "RegistrationPendingError",
    "add_certify",
    "add_registration_sequence",
    "add_reserve_and_register",
    "execute_certify",
    "execute_reserve_and_register",
    "preflight_payment",
    "preflight_sponsor",
    # Shared blob operations
    "SharedBlobOpResult",
    "SharedBlobReceipt",
    "add_extend_shared_blob",
    "add_fund_shared_blob",
    "add_share_blob",
    "execute_extend_shared_blob",
    "execute_fund_shared_blob",
    "execute_share_blob",
    # Shared ops helpers (client-taking)
    "ChainContext",
    "ChainContextError",
    "DEFAULT_FINALITY_MAX_ATTEMPTS",
    "DEFAULT_FINALITY_MAX_DELAY",
    "matches_wal_coin_type",
    "prepare_chain_context",
    "prepare_wal_coin_for_amount",
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
    "store_blob_native",
    "upload_slivers",
    # Native read orchestration
    "MetadataFetchError",
    "NativeReadError",
    "NativeReadResult",
    "SliverFetchError",
    "fetch_slivers",
    "fetch_verified_metadata",
    "read_blob_native",
    "reconstruct_blob",
    # Relay upload orchestration
    "RelayBlobReceipt",
    "RelayCertificateParseError",
    "RelayCertifyTransactionError",
    "RelayOutcome",
    "RelayStageTimings",
    "RelayUploadError",
    "TipCeilingExceededError",
    "TipComposition",
    "TipConfigError",
    "TipPaymentError",
    "add_tip",
    "AuthPackage",
    "ConstTip",
    "DEFAULT_MAX_UPLOAD_ATTEMPTS",
    "FROM_GAS",
    "LinearTip",
    "RelayUploadOutcome",
    "RelayUploadResult",
    "TipConfig",
    "TipKind",
    "TipQuote",
    "TipResult",
    "assert_tip_within_ceiling",
    "build_auth_package",
    "parse_relay_certificate",
    "AssembledQuilt",
    "QuiltPatchInput",
    "QuiltPatchLayout",
    "QuiltPatchReceipt",
    "QuiltRelayReceipt",
    "quote_tip",
    "store_blob_relay",
    "store_quilt_relay",
    "upload_to_relay",
    # Receipt protocols
    "StageTimingsProtocol",
    "UploadReceipt",
    # Client protocols
    "CommitteeAndNodeClient",
    "ExecuteOnlyClient",
]
