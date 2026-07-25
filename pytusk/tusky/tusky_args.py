#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Argument parser construction for the tusky CLI.

This module owns all argparse definitions and nothing else — it has no
knowledge of command handler logic or dispatch. Handlers live in
tusky_cmds.py and are bridged to this module's subcommands purely by the
`subcommand` string set on each subparser via `set_defaults`.
"""

import argparse


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
        default=None,
        help="Set the active Sui address for this session. Mutually exclusive with --pysui-alias.",
    )
    addr_group.add_argument(
        "--pysui-alias",
        dest="pysui_alias",
        default=None,
        help="Set the active Sui address by alias for this session. Mutually exclusive with --pysui-address.",
    )


def _add_blob_id_arg(
    subp: argparse.ArgumentParser,
    *,
    required: bool = True,
    help_text: str = "Sui object ID of the blob.",
) -> None:
    """Add the -i/--blobid argument.

    Args:
        subp (argparse.ArgumentParser): The subparser to add the argument to.
        required (bool): Whether the argument is required. Defaults to True.
        help_text (str): Help text for the argument. Defaults to describing
            a Sui object ID; override for commands that take a different
            identifier (e.g. read_blob's Walrus blob ID).
    """
    subp.add_argument(
        "-i",
        "--blobid",
        dest="blobid",
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
        "blobs", help="List all blobs owned by the active address."
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
        "blob", help="Show full on-chain details for one blob."
    )
    _add_blob_id_arg(p_blob)
    _add_config_args(p_blob)

    p_read_blob = subparsers.add_parser(
        "read_blob", help="Read blob content via the Walrus HTTP aggregator."
    )
    _add_blob_id_arg(
        p_read_blob,
        help_text="Walrus blob ID (URL-safe base64, content hash) to read.",
    )
    _add_config_args(p_read_blob)

    # --- HTTP action command (no --mode; no transaction involved) ---

    p_store_blob = subparsers.add_parser(
        "store_blob", help="Store a blob via the Walrus HTTP publisher."
    )
    content_group = p_store_blob.add_mutually_exclusive_group(required=True)
    content_group.add_argument(
        "--content",
        help="Blob content (UTF-8 text). Mutually exclusive with --file.",
    )
    content_group.add_argument(
        "--file",
        help="Path to a file whose raw bytes will be stored. Mutually exclusive with --content.",
    )
    p_store_blob.add_argument(
        "--epochs", type=int, required=True, help="Number of epochs to store."
    )
    p_store_blob.add_argument(
        "--deletable", action="store_true", help="Store as a deletable blob."
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

    # --- PTB action commands (--mode + sender/sponsor apply) ---

    p_get_wal = subparsers.add_parser(
        "get_wal", help="Exchange SUI for WAL (testnet only)."
    )
    p_get_wal.add_argument(
        "--amount",
        type=int,
        required=True,
        help="Amount of SUI to exchange, in MIST.",
    )
    _add_signing_args(p_get_wal)
    _add_config_args(p_get_wal)

    p_exchange_for_sui = subparsers.add_parser(
        "exchange_for_sui", help="Exchange WAL for SUI (testnet only)."
    )
    p_exchange_for_sui.add_argument(
        "--amount",
        type=int,
        required=True,
        help="Amount of WAL to exchange, in FROST.",
    )
    _add_signing_args(p_exchange_for_sui)
    _add_config_args(p_exchange_for_sui)

    p_extend_blob = subparsers.add_parser(
        "extend_blob", help="Extend a blob's storage lifetime."
    )
    _add_blob_id_arg(p_extend_blob)
    p_extend_blob.add_argument(
        "--epochs",
        type=int,
        required=True,
        help="Number of Walrus epochs to extend by.",
    )
    _add_signing_args(p_extend_blob)
    _add_config_args(p_extend_blob)

    p_delete_blob = subparsers.add_parser(
        "delete_blob", help="Delete one blob, or all active deletable blobs."
    )
    target_group = p_delete_blob.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "-i", "--blobid", dest="blobid", help="Sui object ID of the blob to delete."
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
            "Also burn: in -i mode, burns this blob after deleting it; "
            "in --all-blobs mode, additionally burns expired blobs in the same pass."
        ),
    )
    _add_signing_args(p_delete_blob)
    _add_config_args(p_delete_blob)

    p_burn_blob = subparsers.add_parser(
        "burn_blob", help="Burn one or more blob objects directly."
    )
    p_burn_blob.add_argument(
        "-i",
        "--blobid",
        dest="blobid",
        action="append",
        required=True,
        help="Sui object ID of a blob to burn (repeatable).",
    )
    _add_signing_args(p_burn_blob)
    _add_config_args(p_burn_blob)

    return parser.parse_args(in_args)
