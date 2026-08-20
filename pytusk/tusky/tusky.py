#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""tusky CLI entry point.

Dispatches parsed subcommands to their handlers in tusky_cmds.py.
"""

import asyncio
import sys

from pytusk.tusky import tusky_cmds
from pytusk.tusky.tusky_args import build_parser

_DISPATCH = {
    "read_blob": tusky_cmds.read_blob,
    "read_quilt": tusky_cmds.read_quilt,
    "store_blob": tusky_cmds.store_blob,
    "store_quilt": tusky_cmds.store_quilt,
    "blobs": tusky_cmds.blobs,
    "blob": tusky_cmds.blob,
    "epoch": tusky_cmds.epoch,
    "committee": tusky_cmds.committee,
    "expiry_report": tusky_cmds.expiry_report,
    "wal_coins": tusky_cmds.wal_coins,
    "exchange_for_wal": tusky_cmds.exchange_for_wal,
    "exchange_for_sui": tusky_cmds.exchange_for_sui,
    "extend_blob_expiration": tusky_cmds.extend_blob_expiration,
    "delete_blob": tusky_cmds.delete_blob,
    "burn_blob": tusky_cmds.burn_blob,
    "store_blob_native": tusky_cmds.store_blob_native,
    "certify_blob": tusky_cmds.certify_blob,
    "list_storage": tusky_cmds.list_storage,
    "split_storage": tusky_cmds.split_storage,
    "fuse_storage": tusky_cmds.fuse_storage,
    "reclaim_storage": tusky_cmds.reclaim_storage,
    "extend_blob_with_storage": tusky_cmds.extend_blob_with_storage,
}


def main() -> None:
    """Parse CLI arguments and dispatch to the matching subcommand handler."""
    parsed = build_parser(in_args=sys.argv[1:])
    handler = _DISPATCH.get(parsed.subcommand)
    if handler is None:
        print(f"'{parsed.subcommand}' is not yet implemented.", file=sys.stderr)
        sys.exit(1)
    asyncio.run(handler(parsed))


if __name__ == "__main__":
    main()
