#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""tusky CLI entry point.

Dispatches parsed subcommands to their handlers in tusky_cmds.py. Only a
subset of subcommands have a wired handler so far; the rest fall through to
a "not yet implemented" message.
"""

import asyncio
import sys

from pytusk.tusky import tusky_cmds
from pytusk.tusky.tusky_args import build_parser

_DISPATCH = {
    "read_blob": tusky_cmds.read_blob,
    "store_blob": tusky_cmds.store_blob,
    "blobs": tusky_cmds.blobs,
    "blob": tusky_cmds.blob,
    "wal_coins": tusky_cmds.wal_coins,
    "exchange_for_wal": tusky_cmds.exchange_for_wal,
    "exchange_for_sui": tusky_cmds.exchange_for_sui,
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
