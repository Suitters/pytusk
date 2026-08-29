#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Argument parser construction for the tusky CLI.

This module owns all argparse definitions and nothing else — it has no
knowledge of command handler logic or dispatch. Handlers live across
tusky_cmds_read.py, tusky_cmds_write.py, tusky_cmds_query.py,
tusky_cmds_exchange.py, tusky_cmds_lifecycle.py,
tusky_cmds_native_upload.py, and tusky_cmds_storage.py (with shared
helpers in tusky_cmds_common.py — see tusky.py's own module docstring),
bridged to this module's subcommands purely by the `subcommand` string
set on each subparser via `set_defaults`.
"""

import argparse
from pathlib import Path

from pysui.sui.sui_common.validators import (
    ValidateAddress,
    ValidateAlias,
    ValidateFile,
    ValidateObjectID,
    ValidatePositive,
)


def _add_config_args(subp: argparse.ArgumentParser) -> None:
    """Add shared PytuskConfiguration/PysuiConfiguration location arguments.

    Args:
        subp (argparse.ArgumentParser): The subparser to add arguments to.
    """
    subp.add_argument(
        "--path",
        dest="from_cfg_path",
        default=None,
        help="Directory containing PytuskConfig.json (default: ~/.pysui).",
    )
    subp.add_argument(
        "--network",
        dest="active_network",
        default=None,
        help="Override the active Walrus network (e.g. testnet, mainnet). Default: value stored in PytuskConfig.json.",
    )
    subp.add_argument(
        "--pysui-config-path",
        dest="pysui_config_path",
        default=None,
        help="Override the pysui config folder path (default: value stored in PytuskConfig.json).",
    )
    subp.add_argument(
        "--pysui-group",
        dest="pysui_group_name",
        default=None,
        help="Override the pysui ProfileGroup (protocol) for this session.",
    )
    subp.add_argument(
        "--pysui-profile",
        dest="pysui_profile_name",
        default="testnet",
        help="Override the pysui Profile for this session (default: testnet).",
    )
    addr_group = subp.add_mutually_exclusive_group()
    addr_group.add_argument(
        "--pysui-address",
        dest="pysui_address",
        action=ValidateAddress,
        default=None,
        help="Set the active Sui address for this session. Mutually exclusive with --pysui-alias.",
    )
    addr_group.add_argument(
        "--pysui-alias",
        dest="pysui_alias",
        action=ValidateAlias,
        default=None,
        help="Set the active Sui address by alias for this session. Mutually exclusive with --pysui-address.",
    )


def _key_value_pair(value: str) -> tuple[str, str]:
    """Parse a CLI argument as a KEY=VALUE pair.

    Args:
        value (str): Raw CLI argument text in the form "key=value".

    Returns:
        tuple[str, str]: The parsed (key, value) pair.

    Raises:
        argparse.ArgumentTypeError: If the value isn't in KEY=VALUE form.
    """
    key, sep, val = value.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(
            f"{value!r} must be in KEY=VALUE form (e.g. patch1=path/to/file)"
        )
    return key, val


def _add_blob_id_arg(
    subp: argparse.ArgumentParser,
    *,
    required: bool = True,
    help_text: str = (
        "Sui object ID of the blob (0x-prefixed) — not the Walrus blob ID "
        "(content hash)."
    ),
    validate_as_object_id: bool = True,
) -> None:
    """Add the -i/--blobid argument.

    Args:
        subp (argparse.ArgumentParser): The subparser to add the argument to.
        required (bool): Whether the argument is required. Defaults to True.
        help_text (str): Help text for the argument. Defaults to describing
            a Sui object ID; override for commands that take a different
            identifier (e.g. read_blob's Walrus blob ID).
        validate_as_object_id (bool): Whether to validate the argument as a
            Sui object ID via ``ValidateObjectID``. Defaults to True.
            read_blob's ``-i/--blobid`` is a Walrus content-hash blob ID
            (URL-safe base64), NOT a Sui object ID, so its call site passes
            False to avoid rejecting a well-formed Walrus blob ID.
    """
    kwargs: dict = {}
    if validate_as_object_id:
        kwargs["action"] = ValidateObjectID
    subp.add_argument(
        "-i",
        "--blobid",
        dest="blobid",
        required=required,
        help=help_text,
        **kwargs,
    )


def _add_signing_args(subp: argparse.ArgumentParser) -> None:
    """Add sender/sponsor/mode arguments for PTB-building commands.

    Args:
        subp (argparse.ArgumentParser): The subparser to add arguments to.
    """
    subp.add_argument(
        "--sender",
        dest="sender",
        default=None,
        help="Address or alias of the sender (must exist in PysuiConfiguration); defaults to the active address.",
    )
    subp.add_argument(
        "--sponsor",
        dest="sponsor",
        default=None,
        help="Address or alias of the gas sponsor; may be outside PysuiConfiguration.",
    )
    subp.add_argument(
        "--mode",
        dest="mode",
        choices=["simulate", "execute"],
        default="simulate",
        help="Simulate the transaction or execute it (default: simulate).",
    )


def build_parser(*, in_args: list[str]) -> argparse.Namespace:
    """Build the tusky argument parser and parse the given arguments.

    Args:
        in_args (list[str]): Command-line arguments, excluding the program name.

    Returns:
        argparse.Namespace: Parsed arguments, with a `subcommand` attribute
            identifying which subcommand was invoked.
    """
    parser = argparse.ArgumentParser(
        prog="tusky",
        description="tusky: a Walrus transaction-signing CLI built on pytusk/pysui.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # --- Informational commands (no --mode; no transaction involved) ---

    p_blobs = subparsers.add_parser(
        "blobs",
        help="List all blobs owned by the active address.",
        description="List all blobs owned by the active address.",
    )
    p_blobs.add_argument(
        "--deletable",
        choices=["any", "true", "false"],
        default="any",
        help="Filter by deletable status (default: any).",
    )
    p_blobs.add_argument(
        "--status",
        choices=["any", "active", "expired"],
        default="any",
        help="Filter by expiry status relative to the current Walrus epoch (default: any).",
    )
    _add_config_args(p_blobs)

    p_blob = subparsers.add_parser(
        "blob",
        help="Show full on-chain details for one blob.",
        description="Show full on-chain details for one blob.",
    )
    _add_blob_id_arg(p_blob)
    _add_config_args(p_blob)

    p_epoch = subparsers.add_parser(
        "epoch",
        help="Show the current Walrus epoch.",
        description="Show the current Walrus epoch.",
    )
    _add_config_args(p_epoch)

    p_committee = subparsers.add_parser(
        "committee",
        help="Show the active Walrus storage committee.",
        description="Show the active Walrus storage committee.",
    )
    _add_config_args(p_committee)

    p_expiry_report = subparsers.add_parser(
        "expiry_report",
        help="Show owned blobs sorted soonest-to-expire first.",
        description=(
            "Show an aging report of owned blobs (object ID, end_epoch, "
            "current epoch, remaining epochs), sorted soonest-to-expire first."
        ),
    )
    p_expiry_report.add_argument(
        "--address",
        dest="address",
        default=None,
        help="Sui address to report on (default: active address).",
    )
    _add_config_args(p_expiry_report)

    p_wal_coins = subparsers.add_parser(
        "wal_coins",
        help="List WAL coin objects owned by an address.",
        description="List WAL coin objects owned by an address.",
    )
    p_wal_coins.add_argument(
        "--address",
        dest="address",
        default=None,
        help="Sui address to list WAL coins for (default: active address).",
    )
    _add_config_args(p_wal_coins)

    p_read_blob = subparsers.add_parser(
        "read_blob",
        help="Read blob content via the Walrus HTTP aggregator.",
        description="Read blob content via the Walrus HTTP aggregator.",
    )
    _add_blob_id_arg(
        p_read_blob,
        help_text="Walrus blob ID (URL-safe base64, content hash) to read.",
        validate_as_object_id=False,
    )
    _add_config_args(p_read_blob)

    p_read_quilt = subparsers.add_parser(
        "read_quilt",
        help="Read a single patch from a quilt via the Walrus HTTP aggregator.",
        description="Read a single patch from a quilt via the Walrus HTTP aggregator.",
    )
    p_read_quilt.add_argument(
        "--quilt-id",
        dest="quilt_id",
        required=True,
        help="Walrus quilt identifier.",
    )
    p_read_quilt.add_argument(
        "--patch-key",
        dest="patch_key",
        required=True,
        help="Key identifying the patch within the quilt.",
    )
    _add_config_args(p_read_quilt)

    # --- HTTP action command (no --mode; no transaction involved) ---

    p_store_blob = subparsers.add_parser(
        "store_blob",
        help="Store a blob via the Walrus HTTP publisher.",
        description="Store a blob via the Walrus HTTP publisher.",
    )
    content_group = p_store_blob.add_mutually_exclusive_group(required=True)
    content_group.add_argument(
        "--content",
        help="Blob content (UTF-8 text). Mutually exclusive with --file.",
    )
    content_group.add_argument(
        "--file",
        action=ValidateFile,
        help="Path to a file whose raw bytes will be stored. Mutually exclusive with --content.",
    )
    p_store_blob.add_argument(
        "--epochs",
        action=ValidatePositive,
        required=True,
        help=(
            "Number of epochs to store the blob for, counted from now "
            "(a duration, not an absolute epoch number)."
        ),
    )
    p_store_blob.add_argument(
        "--permanent",
        action="store_true",
        help="Store as a permanent blob (cannot be deleted before expiry).",
    )
    p_store_blob.add_argument(
        "--recipient",
        dest="recipient",
        default=None,
        help="Sui address to receive the stored blob object (default: active address).",
    )
    _add_config_args(p_store_blob)

    p_store_quilt = subparsers.add_parser(
        "store_quilt",
        help="Store a quilt (batch of named files) via the Walrus HTTP publisher.",
        description="Store a quilt (batch of named files) via the Walrus HTTP publisher.",
    )
    p_store_quilt.add_argument(
        "--paths",
        dest="paths",
        nargs="*",
        default=[],
        metavar="PATH",
        help=(
            "File paths to include in the quilt (shell-expandable, e.g. "
            "*.py); the patch key for each is derived from its filename."
        ),
    )
    p_store_quilt.add_argument(
        "--file",
        dest="file",
        action="append",
        default=[],
        type=_key_value_pair,
        metavar="KEY=PATH",
        help=(
            "Patch key and file path for a quilt member, in KEY=PATH form (repeatable)."
        ),
    )
    p_store_quilt.add_argument(
        "--content",
        dest="content",
        action="append",
        default=[],
        type=_key_value_pair,
        metavar="KEY=TEXT",
        help=(
            "Patch key and inline UTF-8 text content for a quilt member, in "
            "KEY=TEXT form (repeatable)."
        ),
    )
    p_store_quilt.add_argument(
        "--epochs",
        action=ValidatePositive,
        required=True,
        help=(
            "Number of epochs to store the quilt for, counted from now "
            "(a duration, not an absolute epoch number)."
        ),
    )
    p_store_quilt.add_argument(
        "--permanent",
        action="store_true",
        help="Store as a permanent quilt (cannot be deleted before expiry).",
    )
    p_store_quilt.add_argument(
        "--recipient",
        dest="recipient",
        default=None,
        help="Sui address to receive the stored quilt object (default: active address).",
    )
    _add_config_args(p_store_quilt)

    # --- Native upload commands (--mode + sender/sponsor apply) ---

    p_store_blob_native = subparsers.add_parser(
        "store_blob_native",
        help="Store a blob directly via the native Walrus upload pipeline (no publisher).",
        description=(
            "Store a blob via the native Walrus upload pipeline "
            "(reserve_space+register_blob, sliver fan-out, certify_blob)."
        ),
    )
    native_content_group = p_store_blob_native.add_mutually_exclusive_group(
        required=True
    )
    native_content_group.add_argument(
        "--content",
        help="Blob content (UTF-8 text). Mutually exclusive with --file.",
    )
    native_content_group.add_argument(
        "--file",
        action=ValidateFile,
        help="Path to a file whose raw bytes will be stored. Mutually exclusive with --content.",
    )
    p_store_blob_native.add_argument(
        "--epochs",
        action=ValidatePositive,
        required=True,
        help=(
            "Number of epochs to store the blob for, counted from now "
            "(a duration, not an absolute epoch number)."
        ),
    )
    p_store_blob_native.add_argument(
        "--permanent",
        action="store_true",
        help="Store as a permanent blob (cannot be deleted before expiry).",
    )
    p_store_blob_native.add_argument(
        "--recipient",
        dest="recipient",
        action=ValidateAddress,
        default=None,
        help="Sui address to receive the stored blob object (default: sender).",
    )
    p_store_blob_native.add_argument(
        "--full-json",
        dest="full_json",
        action="store_true",
        help=(
            "Also print the complete raw simulate transaction result as "
            "JSON, in addition to the concise cost summary (--mode simulate "
            "only; default: off, since the raw result can run to thousands "
            "of lines for a large blob)."
        ),
    )
    p_store_blob_native.add_argument(
        "--log-file",
        dest="log_file",
        type=Path,
        default=None,
        help=(
            "Write an INFO-level log of this run's native upload progress "
            "to the given path (default: no log file is written)."
        ),
    )
    p_store_blob_native.add_argument(
        "--verbose",
        dest="verbose",
        action="store_true",
        help="Emit INFO-level native upload progress to stdout.",
    )
    _add_signing_args(p_store_blob_native)
    _add_config_args(p_store_blob_native)

    # --- Upload relay commands (read-only) ---
    p_relay_configs = subparsers.add_parser(
        "relay_configs",
        help="List configured upload relays and what each would charge.",
        description=(
            "List the upload relays configured for the active network and "
            "what each would charge to store a blob of a given size. Name, "
            "URL and which relay is active come from PytuskConfig; the tip "
            "amount and address are fetched live from each relay. Relays "
            "are queried concurrently and independently -- one unreachable "
            "relay is reported on its own line rather than aborting the "
            "listing."
        ),
    )
    relay_size_group = p_relay_configs.add_mutually_exclusive_group(required=True)
    relay_size_group.add_argument(
        "--size",
        dest="size",
        type=int,
        help=(
            "Blob size in bytes to price. Mutually exclusive with --file "
            "and --content."
        ),
    )
    relay_size_group.add_argument(
        "--file",
        dest="file",
        action=ValidateFile,
        help=(
            "Path to a file whose byte length is priced. The file is "
            "measured, never read or uploaded. Mutually exclusive with "
            "--size and --content."
        ),
    )
    relay_size_group.add_argument(
        "--content",
        dest="content",
        help=(
            "UTF-8 text whose ENCODED byte length is priced. Mutually "
            "exclusive with --size and --file."
        ),
    )
    _add_config_args(p_relay_configs)

    # --- Upload relay commands (--mode + sender/sponsor apply) ---
    p_store_blob_relay = subparsers.add_parser(
        "store_blob_relay",
        help="Store a blob through a Walrus upload relay (practical for mainnet writes).",
        description=(
            "Store a blob via a Walrus upload relay (reserve_space+register_blob "
            "bundled with the relay tip in one transaction, relay upload, "
            "certify_blob). The relay performs the sliver fan-out on your behalf."
        ),
    )
    relay_content_group = p_store_blob_relay.add_mutually_exclusive_group(
        required=True
    )
    relay_content_group.add_argument(
        "--content",
        help="Blob content (UTF-8 text). Mutually exclusive with --file.",
    )
    relay_content_group.add_argument(
        "--file",
        action=ValidateFile,
        help="Path to a file whose raw bytes will be stored. Mutually exclusive with --content.",
    )
    p_store_blob_relay.add_argument(
        "--epochs",
        action=ValidatePositive,
        required=True,
        help=(
            "Number of epochs to store the blob for, counted from now "
            "(a duration, not an absolute epoch number)."
        ),
    )
    p_store_blob_relay.add_argument(
        "--permanent",
        action="store_true",
        help="Store as a permanent blob (cannot be deleted before expiry).",
    )
    p_store_blob_relay.add_argument(
        "--relay",
        dest="relay",
        default=None,
        help=(
            "Name of the upload relay to use (default: the active network's "
            "active_relay)."
        ),
    )
    p_store_blob_relay.add_argument(
        "--tip-source",
        dest="tip_source",
        default="from_gas",
        help=(
            "Where the relay tip is paid from: 'from_gas', meaning whoever "
            "funds the transaction pays (the sponsor if given, otherwise the "
            "sender), or a SUI coin object id to split the tip from "
            "(default: from_gas)."
        ),
    )
    p_store_blob_relay.add_argument(
        "--max-tip",
        dest="max_tip",
        type=int,
        default=None,
        help=(
            "Refuse to proceed if the relay's quoted tip exceeds this many "
            "MIST. Checked before anything is composed, signed, or spent -- "
            "no transaction is submitted. Default: no ceiling."
        ),
    )
    p_store_blob_relay.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help=(
            "Per-attempt relay upload timeout, in seconds. Every retry "
            "re-sends the blob from the beginning, so a large blob on a "
            "slow link needs this raised or the retry budget is spent on "
            "timeouts alone. Default: the client's configured timeout."
        ),
    )
    p_store_blob_relay.add_argument(
        "--recipient",
        dest="recipient",
        action=ValidateAddress,
        default=None,
        help="Sui address to receive the stored blob object (default: sender).",
    )
    p_store_blob_relay.add_argument(
        "--full-json",
        dest="full_json",
        action="store_true",
        help=(
            "Also print the complete raw simulate transaction result as "
            "JSON, in addition to the concise cost summary (--mode simulate "
            "only; default: off, since the raw result can run to thousands "
            "of lines for a large blob)."
        ),
    )
    _add_signing_args(p_store_blob_relay)
    _add_config_args(p_store_blob_relay)

    p_certify_blob = subparsers.add_parser(
        "certify_blob",
        help=(
            "Recover a registered blob's confirmation-collection and "
            "certify stages after an interrupted upload (native or relay)."
        ),
        description=(
            "Recover the confirmation-collection and certify_blob stages for "
            "a registered blob. By default, assumes slivers have ALREADY "
            "been uploaded to the storage nodes and only collects "
            "confirmations and submits Tx2. Pass --recover together with "
            "--content or --file to also re-upload slivers first, for a "
            "blob whose sliver fan-out never ran (for example, an upload "
            "that failed during the register stage). The re-supplied bytes "
            "must re-encode to the SAME blob_id already registered on-chain "
            "for this object, or the command errors out rather than "
            "uploading mismatched content."
        ),
    )
    _add_blob_id_arg(p_certify_blob)
    p_certify_blob.add_argument(
        "--recover",
        action="store_true",
        help=(
            "Also re-upload slivers before collecting confirmations, for a "
            "blob whose sliver fan-out never ran. Requires --content or "
            "--file."
        ),
    )
    recover_content_group = p_certify_blob.add_mutually_exclusive_group()
    recover_content_group.add_argument(
        "--content",
        help=(
            "Blob content (UTF-8 text) to re-encode and re-upload as "
            "slivers. Requires --recover. Mutually exclusive with --file."
        ),
    )
    recover_content_group.add_argument(
        "--file",
        action=ValidateFile,
        help=(
            "Path to a file whose raw bytes will be re-encoded and "
            "re-uploaded as slivers. Requires --recover. Mutually "
            "exclusive with --content."
        ),
    )
    _add_signing_args(p_certify_blob)
    _add_config_args(p_certify_blob)

    # --- PTB action commands (--mode + sender/sponsor apply) ---

    p_extend_blob_expiration = subparsers.add_parser(
        "extend_blob_expiration",
        help="Extend a blob's storage expiration epoch.",
        description=(
            "Extend a blob's storage expiration (only the expiry epoch "
            "changes; content and object ID are unaffected)."
        ),
    )
    _add_blob_id_arg(p_extend_blob_expiration)
    p_extend_blob_expiration.add_argument(
        "--epochs",
        action=ValidatePositive,
        required=True,
        help=(
            "Number of epochs to extend the blob's storage by, counted "
            "from its current end_epoch (a duration, not an absolute "
            "epoch number)."
        ),
    )
    p_extend_blob_expiration.add_argument(
        "--merge",
        action="store_true",
        help=(
            "Merge all owned WAL coins into one before extending, in case "
            "no single coin covers the extension cost."
        ),
    )
    _add_signing_args(p_extend_blob_expiration)
    _add_config_args(p_extend_blob_expiration)

    p_delete_blob = subparsers.add_parser(
        "delete_blob",
        help="Delete one blob, or all active deletable blobs.",
        description=(
            "Delete a non-expired, deletable blob (or all such blobs), "
            "reclaiming both the gas/storage rebate and the Storage "
            "resource for reuse; requires deletable=true and an unexpired "
            "end_epoch."
        ),
    )
    target_group = p_delete_blob.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "-i",
        "--blobid",
        dest="blobid",
        action=ValidateObjectID,
        help=(
            "Sui object ID of the blob to delete (0x-prefixed) — not the "
            "Walrus blob ID (content hash)."
        ),
    )
    target_group.add_argument(
        "--all-blobs",
        action="store_true",
        help="Delete all active deletable blobs owned by the address.",
    )
    p_delete_blob.add_argument(
        "--burn",
        action="store_true",
        help=(
            "Fallback to burning instead of deleting: in -i mode, burns "
            "this blob only if it isn't eligible for delete_blob (already "
            "expired or not deletable); in --all-blobs mode, additionally "
            "burns expired blobs in the same pass."
        ),
    )
    _add_signing_args(p_delete_blob)
    _add_config_args(p_delete_blob)

    p_burn_blob = subparsers.add_parser(
        "burn_blob",
        help="Burn one or more blob objects directly.",
        description=(
            "Burn one or more owned blob objects to reclaim gas — works "
            "on any blob, expired or not, deletable or permanent, but the "
            "underlying Storage resource is destroyed rather than "
            "reusable; the required path for cleaning up expired blobs."
        ),
    )
    p_burn_blob.add_argument(
        "-i",
        "--blobid",
        dest="blobid",
        action="append",
        required=True,
        help=(
            "Sui object ID of a blob to burn (0x-prefixed) — not the "
            "Walrus blob ID (content hash). Repeat -i/--blobid to burn "
            "multiple blobs in one call."
        ),
    )
    _add_signing_args(p_burn_blob)
    _add_config_args(p_burn_blob)

    p_exchange_for_wal = subparsers.add_parser(
        "exchange_for_wal",
        help="Exchange SUI for WAL.",
        description="Exchange SUI for WAL.",
    )
    p_exchange_for_wal.add_argument(
        "--amount",
        action=ValidatePositive,
        required=True,
        help="Amount of SUI to exchange, in MIST.",
    )
    _add_signing_args(p_exchange_for_wal)
    _add_config_args(p_exchange_for_wal)

    p_exchange_for_sui = subparsers.add_parser(
        "exchange_for_sui",
        help="Exchange WAL for SUI.",
        description="Exchange WAL for SUI.",
    )
    p_exchange_for_sui.add_argument(
        "--amount",
        action=ValidatePositive,
        required=True,
        help="Amount of WAL to exchange, in FROST.",
    )
    p_exchange_for_sui.add_argument(
        "--merge",
        action="store_true",
        help=(
            "Merge multiple owned WAL coins (largest-first) if no single "
            "coin covers --amount."
        ),
    )
    _add_signing_args(p_exchange_for_sui)
    _add_config_args(p_exchange_for_sui)

    p_list_storage = subparsers.add_parser(
        "list_storage",
        help="List standalone Storage objects owned by the active address.",
        description=(
            "List standalone (unwrapped) Storage objects owned by the "
            "active address. Storage still embedded in a Blob is a wrapped "
            "object and does not appear here."
        ),
    )
    p_list_storage.add_argument(
        "--status",
        choices=["any", "active", "expired"],
        default="any",
        help=(
            "Filter by expiry status relative to the current Walrus epoch "
            "(default: any). Expired storage is still splittable, fusable "
            "and reclaimable, so this is a display filter, not a capability "
            "gate."
        ),
    )
    p_list_storage.add_argument(
        "--details",
        action="store_true",
        help=(
            "Show which storage-related operations apply to each object "
            "instead of the plain listing: split_storage, fuse_storage (as "
            "compatible pairs), reclaim_storage, and "
            "extend_blob_with_storage (this makes one extra network call "
            "to fetch owned blobs)."
        ),
    )
    _add_config_args(p_list_storage)

    p_split_storage = subparsers.add_parser(
        "split_storage",
        help="Split a Storage object by epoch or by size.",
        description=(
            "Split a standalone Storage object in two, either at an epoch "
            "boundary or by byte capacity. The original is modified in "
            "place; the newly created Storage is transferred to --recipient."
        ),
    )
    p_split_storage.add_argument(
        "-i",
        "--storageid",
        dest="storageid",
        action=ValidateObjectID,
        required=True,
        help="Sui object ID of the Storage object to split (0x-prefixed).",
    )
    split_group = p_split_storage.add_mutually_exclusive_group(required=True)
    split_group.add_argument(
        "--by-epoch",
        dest="by_epoch",
        action=ValidatePositive,
        help=(
            "Absolute epoch to split at: the original keeps "
            "[start_epoch, split_epoch) and the new Storage takes "
            "[split_epoch, end_epoch)."
        ),
    )
    split_group.add_argument(
        "--by-size",
        dest="by_size",
        action=ValidatePositive,
        help=(
            "Byte capacity to peel off into the new Storage; the original "
            "keeps the remainder over the same epoch range."
        ),
    )
    p_split_storage.add_argument(
        "--recipient",
        dest="recipient",
        default=None,
        help=(
            "Address to receive the newly created Storage object; defaults "
            "to the sender."
        ),
    )
    _add_signing_args(p_split_storage)
    _add_config_args(p_split_storage)

    p_fuse_storage = subparsers.add_parser(
        "fuse_storage",
        help="Fuse compatible Storage objects into one.",
        description=(
            "Fuse Storage objects together. Three mutually exclusive modes: "
            "explicit (--fuse-to/--fuse-from name the objects), --fuse-amount "
            "(bulk-fuses every object sharing one epoch-range group, no "
            "object IDs needed), and --fuse-periods (bulk-fuses every object "
            "reachable from one hub via adjacent-and-equal-size steps, no "
            "object IDs needed for the spokes). Run 'list_storage --details' "
            "to preview the fuse_amount groups and fuse_periods clusters "
            "before choosing."
        ),
    )
    p_fuse_storage.add_argument(
        "--fuse-to",
        dest="fuse_to",
        action=ValidateObjectID,
        default=None,
        help=(
            "Sui object ID of the Storage that survives and absorbs the "
            "other(s) (0x-prefixed). Required for explicit mode; optional "
            "for --fuse-periods, where it names which cluster's hub to "
            "consolidate around when more than one is owned."
        ),
    )
    p_fuse_storage.add_argument(
        "--fuse-from",
        dest="fuse_from",
        nargs="+",
        action=ValidateObjectID,
        default=None,
        help=(
            "One or more Sui object IDs to fold into --fuse-to, in order "
            "(0x-prefixed); each is consumed by its fuse. Explicit mode "
            "only."
        ),
    )
    fuse_bulk_group = p_fuse_storage.add_mutually_exclusive_group()
    fuse_bulk_group.add_argument(
        "--fuse-amount",
        action="store_true",
        help=(
            "Bulk-fuse every owned Storage object sharing one identical "
            "epoch-range group -- no object IDs needed. If more than one "
            "such group is owned, --start-epoch/--end-epoch name which one."
        ),
    )
    fuse_bulk_group.add_argument(
        "--fuse-periods",
        action="store_true",
        help=(
            "Bulk-fuse every owned Storage object reachable, via "
            "equal-size adjacent-range steps, from one hub -- no object "
            "IDs needed for the spokes. If more than one disjoint cluster "
            "is owned, --fuse-to names the hub to consolidate around."
        ),
    )
    p_fuse_storage.add_argument(
        "--start-epoch",
        dest="start_epoch",
        action=ValidatePositive,
        default=None,
        help=(
            "With --fuse-amount: the epoch-range group's start_epoch, "
            "required only when more than one group is owned."
        ),
    )
    p_fuse_storage.add_argument(
        "--end-epoch",
        dest="end_epoch",
        action=ValidatePositive,
        default=None,
        help=(
            "With --fuse-amount: the epoch-range group's end_epoch, "
            "required only when more than one group is owned."
        ),
    )
    _add_signing_args(p_fuse_storage)
    _add_config_args(p_fuse_storage)

    p_reclaim_storage = subparsers.add_parser(
        "reclaim_storage",
        help="Destroy one or more Storage objects, reclaiming their Sui storage rebate.",
        description=(
            "Destroy one or more standalone Storage objects. This does NOT "
            "refund the WAL paid to reserve the capacity -- only the Sui "
            "storage rebate for each object is returned. Irreversible."
        ),
    )
    reclaim_group = p_reclaim_storage.add_mutually_exclusive_group(required=True)
    reclaim_group.add_argument(
        "-i",
        "--storageid",
        dest="storageid",
        nargs="+",
        action=ValidateObjectID,
        default=None,
        help="One or more Sui object IDs of Storage objects to destroy (0x-prefixed).",
    )
    reclaim_group.add_argument(
        "--all",
        action="store_true",
        help=(
            "Destroy every currently owned unwrapped Storage object -- no "
            "object IDs needed."
        ),
    )
    _add_signing_args(p_reclaim_storage)
    _add_config_args(p_reclaim_storage)

    p_extend_blob_with_storage = subparsers.add_parser(
        "extend_blob_with_storage",
        help="Extend a blob's expiration using an owned Storage object.",
        description=(
            "Extend a blob's storage expiration by consuming a standalone "
            "Storage object instead of paying WAL. The Storage must end "
            "strictly later than the blob's current end_epoch, and must be "
            "compatible with the blob's existing storage."
        ),
    )
    _add_blob_id_arg(p_extend_blob_with_storage)
    p_extend_blob_with_storage.add_argument(
        "--storageid",
        dest="storageid",
        action=ValidateObjectID,
        required=True,
        help=(
            "Sui object ID of the Storage object to consume (0x-prefixed). "
            "It is consumed by the extension and ceases to exist."
        ),
    )
    _add_signing_args(p_extend_blob_with_storage)
    _add_config_args(p_extend_blob_with_storage)

    return parser.parse_args(in_args)
