#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""tusky CLI entry point.

Dispatches parsed subcommands to their handlers, split by domain across
tusky_cmds_read.py, tusky_cmds_write.py, tusky_cmds_query.py,
tusky_cmds_exchange.py, tusky_cmds_lifecycle.py,
tusky_cmds_native_upload.py, and tusky_cmds_storage.py (with shared
helpers in tusky_cmds_common.py).
"""

import asyncio
import sys

from pytusk.tusky import (
    tusky_cmds_exchange,
    tusky_cmds_lifecycle,
    tusky_cmds_native_upload,
    tusky_cmds_query,
    tusky_cmds_read,
    tusky_cmds_relay,
    tusky_cmds_storage,
    tusky_cmds_write,
)
from pytusk.tusky.tusky_args import build_parser

_DISPATCH = {
    "read_blob": tusky_cmds_read.read_blob,
    "read_blob_native": tusky_cmds_read.read_blob_native,
    "read_quilt": tusky_cmds_read.read_quilt,
    "quilt_patches": tusky_cmds_read.quilt_patches,
    "store_blob": tusky_cmds_write.store_blob,
    "store_quilt": tusky_cmds_write.store_quilt,
    "blobs": tusky_cmds_query.blobs,
    "blob": tusky_cmds_query.blob,
    "blob_status": tusky_cmds_query.blob_status,
    "get_blob_metadata": tusky_cmds_query.get_blob_metadata,
    "epoch": tusky_cmds_query.epoch,
    "committee": tusky_cmds_query.committee,
    "expiry_report": tusky_cmds_query.expiry_report,
    "wal_coins": tusky_cmds_query.wal_coins,
    "exchange_for_wal": tusky_cmds_exchange.exchange_for_wal,
    "exchange_for_sui": tusky_cmds_exchange.exchange_for_sui,
    "extend_blob_expiration": tusky_cmds_lifecycle.extend_blob_expiration,
    "set_blob_metadata": tusky_cmds_lifecycle.set_blob_metadata,
    "drop_blob_metadata": tusky_cmds_lifecycle.drop_blob_metadata,
    "delete_blob": tusky_cmds_lifecycle.delete_blob,
    "burn_blob": tusky_cmds_lifecycle.burn_blob,
    "share_blob": tusky_cmds_lifecycle.share_blob,
    "fund_shared_blob": tusky_cmds_lifecycle.fund_shared_blob,
    "extend_shared_blob": tusky_cmds_lifecycle.extend_shared_blob,
    "store_blob_native": tusky_cmds_native_upload.store_blob_native,
    "certify_blob": tusky_cmds_native_upload.certify_blob,
    "relay_configs": tusky_cmds_relay.relay_configs,
    "store_blob_relay": tusky_cmds_relay.store_blob_relay,
    "store_quilt_relay": tusky_cmds_relay.store_quilt_relay,
    "list_storage": tusky_cmds_storage.list_storage,
    "split_storage": tusky_cmds_storage.split_storage,
    "fuse_storage": tusky_cmds_storage.fuse_storage,
    "reclaim_storage": tusky_cmds_storage.reclaim_storage,
    "extend_blob_with_storage": tusky_cmds_storage.extend_blob_with_storage,
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
