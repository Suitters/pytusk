#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Write command handlers for the tusky CLI.

Handlers for storing blobs/quilts via the Walrus HTTP publisher. Each
handler takes the parsed argparse.Namespace for its subcommand and
performs the corresponding pytusk operation. Handlers are async; tusky.py
drives them via asyncio.run.
"""

import argparse
import asyncio
import os
import sys

from pytusk import StoreBlob, StoreQuilt, WalrusClient
from pytusk.tusky.tusky_cmds_common import config_from_args, read_file_bytes


async def store_blob(args: argparse.Namespace) -> None:
    """Store a blob via the Walrus HTTP publisher and print the resulting receipt.

    Content comes from --content (UTF-8 text) or --file (raw bytes), whichever
    was given. The blob object is sent to --recipient if given, otherwise to
    the active address, so it transfers to a wallet instead of staying with
    the publisher.

    Args:
        args (argparse.Namespace): Parsed `store_blob` subcommand arguments.
    """
    if args.file:
        try:
            data = await asyncio.to_thread(read_file_bytes, args.file)
        except OSError as exc:
            print(f"Error reading file {args.file}: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        data = args.content.encode("utf-8")

    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        recipient = args.recipient or client.pysui_client.config.active_address
        result = await client.execute(
            command=StoreBlob(
                data=data,
                epochs=args.epochs,
                send_object_to=recipient,
                permanent=args.permanent,
            )
        )
    if not result.is_ok():
        print(f"Error storing blob: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))


async def store_quilt(args: argparse.Namespace) -> None:
    """Store a quilt via the Walrus HTTP publisher and print the resulting receipt.

    Patch content comes from --paths (file paths, patch key = filename),
    --file (KEY=PATH, read from disk), and/or --content (KEY=TEXT, UTF-8
    encoded) — all repeatable and combinable. The quilt object is sent to
    --recipient if given, otherwise to the active address, so it transfers
    to a wallet instead of staying with the publisher.

    Args:
        args (argparse.Namespace): Parsed `store_quilt` subcommand arguments.
    """
    files: dict[str, bytes] = {}
    for path in args.paths:
        key = os.path.basename(path)
        if key in files:
            print(f"Error: duplicate patch key {key!r}", file=sys.stderr)
            sys.exit(1)
        try:
            files[key] = await asyncio.to_thread(read_file_bytes, path)
        except OSError as exc:
            print(f"Error reading file {path}: {exc}", file=sys.stderr)
            sys.exit(1)
    for key, path in args.file:
        if key in files:
            print(f"Error: duplicate patch key {key!r}", file=sys.stderr)
            sys.exit(1)
        try:
            files[key] = await asyncio.to_thread(read_file_bytes, path)
        except OSError as exc:
            print(f"Error reading file {path}: {exc}", file=sys.stderr)
            sys.exit(1)
    for key, text in args.content:
        if key in files:
            print(f"Error: duplicate patch key {key!r}", file=sys.stderr)
            sys.exit(1)
        files[key] = text.encode("utf-8")

    if not files:
        print(
            "Error: at least one --paths, --file, or --content patch is required",
            file=sys.stderr,
        )
        sys.exit(1)

    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        recipient = args.recipient or client.pysui_client.config.active_address
        result = await client.execute(
            command=StoreQuilt(
                files=files,
                epochs=args.epochs,
                send_object_to=recipient,
                permanent=args.permanent,
            )
        )
    if not result.is_ok():
        print(f"Error storing quilt: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))
