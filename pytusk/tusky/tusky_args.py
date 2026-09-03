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
import binascii
from pathlib import Path

from pysui.sui.sui_common.validators import (
    ValidateAddress,
    ValidateAlias,
    ValidateFile,
    ValidateObjectID,
    ValidatePositive,
    valid_sui_address,
)

from pytusk import blob_id_from_url_base64


class ValidateObjectIDAppend(argparse.Action):
    """Validate a Sui object ID and append it to a repeatable list.

    Unlike ``ValidateObjectID``, which overwrites ``dest`` on each call,
    this preserves values across repeated flag invocations (e.g.
    ``-o id1 -o id2``), matching burn_blob's pre-existing repeatable UX
    while adding validation.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str,
        option_string: str | None = None,
    ) -> None:
        """Validate then append rather than overwrite."""
        if not valid_sui_address(values):
            parser.error(f"'{values}' is not a valid Sui object id format.")
        items = list(getattr(namespace, self.dest, None) or [])
        items.append(values)
        setattr(namespace, self.dest, items)


# A Walrus blob ID is a 32-byte content hash rendered as URL-safe base64
# with no padding, which is always exactly 43 characters.
_BLOB_ID_BYTES = 32
_BLOB_ID_B64_LENGTH = 43

# The alphabet MUST be checked explicitly. `base64.urlsafe_b64decode` maps
# '-'/'_' onto '+'/'/' and then decodes with validate=False, which silently
# DISCARDS characters outside the standard alphabet. A blob ID containing
# '+' or '/' therefore decodes cleanly to 32 bytes and would be accepted --
# as a DIFFERENT blob ID than the operator typed. Length and byte-count
# checks alone do not catch this.
_BLOB_ID_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class ValidateBlobID(argparse.Action):
    """Validate a Walrus blob ID before it reaches a storage-node URL.

    Every object-ID flag in this CLI already runs ``ValidateObjectID``,
    while ``-b``/``--blob-id`` was an unchecked string. That gap mattered
    little when ``-b`` only fed a read, but blob-status makes a blob ID the
    KEY of a committee-wide query, so a malformed value would otherwise be
    interpolated into a URL and fanned out to every storage node before
    anything noticed.

    Rejecting at parse time gives the operator a message on stderr and a
    non-zero exit before any network call happens.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str,
        option_string: str | None = None,
    ) -> None:
        """Validate the blob ID, then store the ORIGINAL string.

        The decoded bytes are deliberately discarded: handlers already
        convert as needed, and storing bytes here would make ``dest`` hold
        a different type than every other string argument.
        """
        if len(values) != _BLOB_ID_B64_LENGTH:
            parser.error(
                f"'{values}' is not a valid Walrus blob id: expected "
                f"{_BLOB_ID_B64_LENGTH} URL-safe base64 characters, got "
                f"{len(values)}."
            )
        invalid = sorted(set(values) - _BLOB_ID_ALPHABET)
        if invalid:
            parser.error(
                f"'{values}' is not a valid Walrus blob id: contains "
                f"{invalid} which are not URL-safe base64. The URL-safe "
                "alphabet uses '-' and '_', never '+' or '/'."
            )
        try:
            decoded = blob_id_from_url_base64(value=values)
        except (binascii.Error, ValueError):
            # binascii.Error subclasses ValueError; both are caught so a
            # malformed alphabet and a malformed length report identically.
            parser.error(
                f"'{values}' is not a valid Walrus blob id: not URL-safe "
                "base64. Note the URL-safe alphabet uses '-' and '_', never "
                "'+' or '/'."
            )
            return
        if len(decoded) != _BLOB_ID_BYTES:
            parser.error(
                f"'{values}' is not a valid Walrus blob id: decodes to "
                f"{len(decoded)} bytes, expected {_BLOB_ID_BYTES}."
            )
        setattr(namespace, self.dest, values)


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


def _add_object_id_arg(
    subp: argparse.ArgumentParser,
    *,
    required: bool = True,
    help_text: str = (
        "Sui object ID of the blob (0x-prefixed) — not the Walrus blob ID "
        "(content hash)."
    ),
) -> None:
    """Add the -o/--object-id argument.

    Args:
        subp (argparse.ArgumentParser): The subparser to add the argument to.
        required (bool): Whether the argument is required. Defaults to True.
        help_text (str): Help text for the argument. Defaults to describing
            a Sui object ID of a blob; override for commands where the
            object ID identifies something else (e.g. extend_blob_with_storage's
            blob argument, disambiguated from its own storage object ID).
    """
    subp.add_argument(
        "-o",
        "--object-id",
        dest="object_id",
        action=ValidateObjectID,
        required=required,
        help=help_text,
    )


def _add_blob_id_arg(
    subp: argparse.ArgumentParser,
    *,
    required: bool = True,
    help_text: str = "Walrus blob ID (URL-safe base64, content hash).",
) -> None:
    """Add the -b/--blob-id argument.

    Args:
        subp (argparse.ArgumentParser): The subparser to add the argument to.
        required (bool): Whether the argument is required. Defaults to True.
        help_text (str): Help text for the argument. Defaults to describing
            a Walrus content-hash blob ID.
    """
    subp.add_argument(
        "-b",
        "--blob-id",
        dest="blob_id",
        action=ValidateBlobID,
        required=required,
        help=help_text,
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
    p_blobs.add_argument(
        "--show-type",
        dest="show_type",
        action="store_true",
        help=(
            "Also show each blob's type (blob/quilt), read from the "
            "_walrusBlobType metadata attribute pytusk itself writes at "
            "store time -- NOT an authoritative content check: correct "
            "only for as long as that tag is set and retained. A real "
            "quilt reports as 'blob' if the tag was never set (e.g. "
            "stored by other tooling) or was later removed via "
            "drop_blob_metadata (default: off, since this costs one extra "
            "RPC call per listed blob)."
        ),
    )
    _add_config_args(p_blobs)

    p_blob = subparsers.add_parser(
        "blob",
        help="Show full on-chain details for one blob.",
        description="Show full on-chain details for one blob.",
    )
    _add_object_id_arg(p_blob)
    _add_config_args(p_blob)

    # Defined adjacent to `blob` deliberately: the two are read together.
    # It is a TOP-LEVEL command like every other, NOT nested under `blob` --
    # this CLI has a single flat command level and introducing the first
    # nested subparser here would break every existing `tusky blob -o <id>`
    # invocation.
    p_blob_status = subparsers.add_parser(
        "blob_status",
        help="Show storage-node quorum status for one blob.",
        description=(
            "Ask the Walrus storage committee what it knows about a blob and "
            "resolve the answers against shard-weight thresholds. Unlike "
            "'blob', which reads one on-chain object you name, this answers "
            "for the CONTENT regardless of who owns it. On-chain "
            "blob_sui_object details are added when they can be reached; a "
            "thin result means no object was reachable, NOT that the blob "
            "has no objects."
        ),
    )
    blob_status_id = p_blob_status.add_mutually_exclusive_group(required=True)
    blob_status_id.add_argument(
        "-b",
        "--blob-id",
        dest="blob_id",
        action=ValidateBlobID,
        help=(
            "Walrus blob ID (URL-safe base64, content hash). "
            "blob_sui_object details are added when a Blob object can be "
            "reached for it."
        ),
    )
    blob_status_id.add_argument(
        "-o",
        "--object-id",
        dest="object_id",
        action=ValidateObjectID,
        help=(
            "Sui object ID of a Blob (0x-prefixed) — not the Walrus blob ID. "
            "The blob ID is read from the object, so blob_sui_object "
            "details are always available."
        ),
    )
    p_blob_status.add_argument(
        "--details",
        dest="details",
        action="store_true",
        help=(
            "List every committee member: those confirming the verdict, and "
            "those dissenting with the reason each did not contribute."
        ),
    )
    p_blob_status.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=10.0,
        help=(
            "Overall deadline for the status query, in seconds (default: 10). "
            "This bounds the WHOLE operation, not each request — unlike "
            "store_blob_relay's --timeout, which is per-attempt. The fan-out "
            "stops early once a threshold is met, so this mostly governs "
            "stragglers."
        ),
    )
    _add_config_args(p_blob_status)

    p_get_blob_metadata = subparsers.add_parser(
        "get_blob_metadata",
        help='Show a blob\'s on-chain metadata (Walrus "attribute") key/value pairs.',
        description=(
            "Show a blob's on-chain metadata (Walrus \"attribute\") "
            "key/value pairs, read directly from its metadata dynamic "
            "field. Pure read -- no transaction is built or submitted."
        ),
    )
    _add_object_id_arg(p_get_blob_metadata)
    _add_config_args(p_get_blob_metadata)

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
        action=ValidateAddress,
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
        action=ValidateAddress,
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
    )
    _add_config_args(p_read_blob)

    p_read_quilt = subparsers.add_parser(
        "read_quilt",
        help="Read a single patch from a quilt via the Walrus HTTP aggregator.",
        description=(
            "Read a single patch from a quilt via the Walrus HTTP "
            "aggregator. Two addressing modes: --quilt-id and --patch-key "
            "together (the name given at store time), or --patch-id alone "
            "(the opaque QuiltPatchId, e.g. from 'quilt_patches')."
        ),
    )
    p_read_quilt.add_argument(
        "--quilt-id",
        dest="quilt_id",
        default=None,
        help="Walrus quilt identifier. Used with --patch-key.",
    )
    p_read_quilt.add_argument(
        "--patch-key",
        dest="patch_key",
        default=None,
        help="Key identifying the patch within the quilt. Used with --quilt-id.",
    )
    p_read_quilt.add_argument(
        "--patch-id",
        dest="patch_id",
        default=None,
        help=(
            "Walrus QuiltPatchId (URL-safe base64) addressing the patch "
            "directly. Not combined with --quilt-id/--patch-key."
        ),
    )
    _add_config_args(p_read_quilt)

    p_quilt_patches = subparsers.add_parser(
        "quilt_patches",
        help="List the patches contained in a quilt via the Walrus HTTP aggregator.",
        description=(
            "List the patches contained in a quilt via the Walrus HTTP "
            "aggregator. Gates on expiry first: resolves the committee's "
            "own verdict before ever reaching the aggregator, since a "
            "quilt is a blob like any other and its content can be "
            "expired or never-existed -- both of which the aggregator "
            "reports identically as a bare BLOB_NOT_FOUND."
        ),
    )
    quilt_patches_id = p_quilt_patches.add_mutually_exclusive_group(required=True)
    quilt_patches_id.add_argument(
        "-b",
        "--blob-id",
        dest="blob_id",
        action=ValidateBlobID,
        help="Walrus blob ID (URL-safe base64, content hash) of the quilt.",
    )
    quilt_patches_id.add_argument(
        "-o",
        "--object-id",
        dest="object_id",
        action=ValidateObjectID,
        help=(
            "Sui object ID of the quilt's Blob (0x-prefixed) — not the "
            "Walrus blob ID. The blob ID is read from the object."
        ),
    )
    p_quilt_patches.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=10.0,
        help=(
            "Overall deadline for the committee status query used to gate "
            "on expiry, in seconds (default: 10)."
        ),
    )
    _add_config_args(p_quilt_patches)

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
        action=ValidateAddress,
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
        "--patch-file",
        dest="patch_file",
        action="append",
        default=[],
        type=_key_value_pair,
        metavar="KEY=PATH",
        help=(
            "Patch key and file path for a quilt member, in KEY=PATH form (repeatable)."
        ),
    )
    p_store_quilt.add_argument(
        "--patch-content",
        dest="patch_content",
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
        action=ValidateAddress,
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
        "--tip-gas-source",
        dest="tip_gas_source",
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
        "--log-file",
        dest="log_file",
        type=Path,
        default=None,
        help=(
            "Write an INFO-level log of this run's relay upload progress "
            "to the given path (default: no log file is written)."
        ),
    )
    p_store_blob_relay.add_argument(
        "--verbose",
        dest="verbose",
        action="store_true",
        help="Emit INFO-level relay upload progress to stdout.",
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

    p_store_quilt_relay = subparsers.add_parser(
        "store_quilt_relay",
        help="Store several files as one quilt through a Walrus upload relay.",
        description=(
            "Store a batch of named files as a single quilt via a Walrus "
            "upload relay (reserve_space+register_blob bundled with the relay "
            "tip in one transaction, relay upload, certify_blob). The whole "
            "batch costs one registration and one certification rather than "
            "one each."
        ),
    )
    p_store_quilt_relay.add_argument(
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
    p_store_quilt_relay.add_argument(
        "--patch-file",
        dest="patch_file",
        action="append",
        default=[],
        type=_key_value_pair,
        metavar="KEY=PATH",
        help=(
            "Patch key and file path for a quilt member, in KEY=PATH form (repeatable)."
        ),
    )
    p_store_quilt_relay.add_argument(
        "--patch-content",
        dest="patch_content",
        action="append",
        default=[],
        type=_key_value_pair,
        metavar="KEY=TEXT",
        help=(
            "Patch key and inline UTF-8 text content for a quilt member, in "
            "KEY=TEXT form (repeatable)."
        ),
    )
    p_store_quilt_relay.add_argument(
        "--epochs",
        action=ValidatePositive,
        required=True,
        help=(
            "Number of epochs to store the quilt for, counted from now "
            "(a duration, not an absolute epoch number)."
        ),
    )
    p_store_quilt_relay.add_argument(
        "--permanent",
        action="store_true",
        help="Store as a permanent quilt (cannot be deleted before expiry).",
    )
    p_store_quilt_relay.add_argument(
        "--relay",
        dest="relay",
        default=None,
        help=(
            "Name of the upload relay to use (default: the active network's "
            "active_relay)."
        ),
    )
    p_store_quilt_relay.add_argument(
        "--tip-gas-source",
        dest="tip_gas_source",
        default="from_gas",
        help=(
            "Where the relay tip is paid from: 'from_gas', meaning whoever "
            "funds the transaction pays (the sponsor if given, otherwise the "
            "sender), or a SUI coin object id to split the tip from "
            "(default: from_gas)."
        ),
    )
    p_store_quilt_relay.add_argument(
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
    p_store_quilt_relay.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help=(
            "Per-attempt relay upload timeout, in seconds. A quilt is larger "
            "than any single file in it and every retry re-sends the whole "
            "buffer, so this generally needs raising above what a single "
            "blob would want. Default: the client's configured timeout."
        ),
    )
    p_store_quilt_relay.add_argument(
        "--recipient",
        dest="recipient",
        action=ValidateAddress,
        default=None,
        help="Sui address to receive the stored quilt object (default: sender).",
    )
    p_store_quilt_relay.add_argument(
        "--full-json",
        dest="full_json",
        action="store_true",
        help=(
            "Also print the complete raw simulate transaction result as "
            "JSON, in addition to the concise cost summary (--mode simulate "
            "only; default: off, since the raw result can run to thousands "
            "of lines)."
        ),
    )
    p_store_quilt_relay.add_argument(
        "--log-file",
        dest="log_file",
        type=Path,
        default=None,
        help=(
            "Write an INFO-level log of this run's relay upload progress "
            "to the given path (default: no log file is written)."
        ),
    )
    p_store_quilt_relay.add_argument(
        "--verbose",
        dest="verbose",
        action="store_true",
        help="Emit INFO-level relay upload progress to stdout.",
    )
    _add_signing_args(p_store_quilt_relay)
    _add_config_args(p_store_quilt_relay)

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
    _add_object_id_arg(p_certify_blob)
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
    _add_object_id_arg(p_extend_blob_expiration)
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
            "Merge owned WAL coins (largest-first) if no single coin "
            "covers the extension cost."
        ),
    )
    _add_signing_args(p_extend_blob_expiration)
    _add_config_args(p_extend_blob_expiration)

    p_set_blob_metadata = subparsers.add_parser(
        "set_blob_metadata",
        help='Insert or update one or more metadata (Walrus "attribute") pairs on a blob.',
        description=(
            "Insert or update one or more metadata (Walrus \"attribute\") "
            "key/value pairs on a blob via insert_or_update_metadata_pair "
            "-- one move_call per pair, in one PTB. Upsert semantics: a "
            "key not yet present is inserted; an existing key's value is "
            "overwritten."
        ),
    )
    _add_object_id_arg(p_set_blob_metadata)
    p_set_blob_metadata.add_argument(
        "--attr",
        dest="attr",
        nargs=2,
        action="append",
        required=True,
        metavar=("KEY", "VALUE"),
        help=(
            "Metadata key and value to set (repeatable, e.g. --attr k1 v1 "
            "--attr k2 v2); at least one is required."
        ),
    )
    _add_signing_args(p_set_blob_metadata)
    _add_config_args(p_set_blob_metadata)

    p_drop_blob_metadata = subparsers.add_parser(
        "drop_blob_metadata",
        help='Drop one or more metadata keys, or all metadata, from a blob.',
        description=(
            "Drop metadata (Walrus \"attribute\") from a blob: --keys "
            "removes one or more named keys via remove_metadata_pair (one "
            "move_call per key, in one PTB); --all drops the whole "
            "metadata set via a single take_metadata call. Both modes "
            "check existence against the blob's current metadata before "
            "composing any transaction, reporting a clean error instead "
            "of paying gas for a guaranteed on-chain abort."
        ),
    )
    _add_object_id_arg(p_drop_blob_metadata)
    drop_metadata_group = p_drop_blob_metadata.add_mutually_exclusive_group(
        required=True
    )
    drop_metadata_group.add_argument(
        "--keys",
        dest="keys",
        nargs="+",
        default=None,
        help="One or more metadata keys to remove. Mutually exclusive with --all.",
    )
    drop_metadata_group.add_argument(
        "--all",
        action="store_true",
        help="Remove all metadata from the blob. Mutually exclusive with --keys.",
    )
    _add_signing_args(p_drop_blob_metadata)
    _add_config_args(p_drop_blob_metadata)

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
        "-o",
        "--object-id",
        dest="object_id",
        action=ValidateObjectID,
        help=(
            "Sui object ID of the blob to delete (0x-prefixed) — not the "
            "Walrus blob ID (content hash)."
        ),
    )
    target_group.add_argument(
        "--all",
        action="store_true",
        help=(
            "Delete every active deletable blob owned by the address -- no "
            "object IDs needed."
        ),
    )
    p_delete_blob.add_argument(
        "--burn",
        action="store_true",
        help=(
            "Fallback to burning instead of deleting: in -o mode, burns "
            "this blob only if it isn't eligible for delete_blob (already "
            "expired or not deletable); in --all mode, additionally "
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
        "-o",
        "--object-id",
        dest="object_id",
        action=ValidateObjectIDAppend,
        required=True,
        help=(
            "Sui object ID of a blob to burn (0x-prefixed) — not the "
            "Walrus blob ID (content hash). Repeat -o/--object-id to burn "
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
        "-s",
        "--storage-id",
        dest="storage_id",
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
        action=ValidateAddress,
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
            "Sui storage object id of the Storage that survives and absorbs "
            "the other(s) (0x-prefixed). Required for explicit mode; "
            "optional for --fuse-periods, where it names which cluster's "
            "hub to consolidate around when more than one is owned."
        ),
    )
    p_fuse_storage.add_argument(
        "--fuse-from",
        dest="fuse_from",
        nargs="+",
        action=ValidateObjectID,
        default=None,
        help=(
            "One or more Sui storage object ids to fold into --fuse-to, in "
            "order (0x-prefixed); each is consumed by its fuse. Explicit "
            "mode only."
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
        "-s",
        "--storage-id",
        dest="storage_id",
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
    _add_object_id_arg(
        p_extend_blob_with_storage,
        help_text=(
            "Sui object ID of the blob to extend (0x-prefixed) — not the "
            "Walrus blob ID (content hash)."
        ),
    )
    p_extend_blob_with_storage.add_argument(
        "-s",
        "--storage-id",
        dest="storage_id",
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
