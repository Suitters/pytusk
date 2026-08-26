#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Read command handlers for the tusky CLI.

Handlers for reading blob/quilt content via the Walrus HTTP aggregator.
Each handler takes the parsed argparse.Namespace for its subcommand and
performs the corresponding pytusk operation. Handlers are async; tusky.py
drives them via asyncio.run.
"""

import argparse
import sys

from pytusk import BlobData, QuiltPatch, ReadBlob, ReadQuiltPatch, WalrusClient
from pytusk.tusky.tusky_cmds_common import _config_from_args


async def read_blob(args: argparse.Namespace) -> None:
    """Read a blob via the Walrus HTTP aggregator and write its content to stdout.

    Args:
        args (argparse.Namespace): Parsed `read_blob` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(command=ReadBlob(blob_id=args.blobid))
    if not result.is_ok():
        print(f"Error reading blob: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    data: BlobData = result.result_data
    sys.stdout.buffer.write(data.content)
    if sys.stdout.isatty():
        sys.stdout.buffer.write(b"\n")


async def read_quilt(args: argparse.Namespace) -> None:
    """Read a single quilt patch via the Walrus HTTP aggregator and write it to stdout.

    Args:
        args (argparse.Namespace): Parsed `read_quilt` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(
            command=ReadQuiltPatch(quilt_id=args.quilt_id, patch_key=args.patch_key)
        )
    if not result.is_ok():
        print(f"Error reading quilt patch: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    data: QuiltPatch = result.result_data
    sys.stdout.buffer.write(data.content)
    if sys.stdout.isatty():
        sys.stdout.buffer.write(b"\n")
