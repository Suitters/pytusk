#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Command handlers for the tusky CLI.

Each handler takes the parsed argparse.Namespace for its subcommand and
performs the corresponding pytusk/pysui operation. Handlers are async;
tusky.py drives them via asyncio.run.
"""

import argparse
import base64
import sys

from pysui import GetObject, GetObjectsOwnedByAddress

from pytusk import (
    BlobData,
    PytuskConfiguration,
    ReadBlob,
    StoreBlob,
    WalrusClient,
    get_walrus_epoch,
)


def _config_from_args(args: argparse.Namespace) -> PytuskConfiguration:
    """Build a PytuskConfiguration from a subcommand's shared config arguments.

    Args:
        args (argparse.Namespace): Parsed arguments carrying the
            `_add_config_args` fields (from_cfg_path, pysui_config_path,
            pysui_group_name, pysui_profile_name, pysui_address, pysui_alias).

    Returns:
        PytuskConfiguration: Configuration for this CLI invocation.
    """
    return PytuskConfiguration(
        from_cfg_path=args.from_cfg_path,
        pysui_config_path=args.pysui_config_path,
        pysui_group_name=args.pysui_group_name,
        pysui_profile_name=args.pysui_profile_name,
        pysui_address=args.pysui_address,
        pysui_alias=args.pysui_alias,
    )


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
            with open(args.file, "rb") as f:
                data = f.read()
        except OSError as exc:
            print(f"Error reading file {args.file}: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        data = args.content.encode("utf-8")

    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        recipient = args.recipient or client.pysui_client.config.active_address
        result = await client.execute(
            command=StoreBlob(
                data=data,
                epochs=args.epochs,
                send_object_to=recipient,
                deletable=args.deletable,
                permanent=args.permanent,
            )
        )
    if not result.is_ok():
        print(f"Error storing blob: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))


async def blobs(args: argparse.Namespace) -> None:
    """List blobs owned by the active address, with optional filtering.

    Args:
        args (argparse.Namespace): Parsed `blobs` subcommand arguments,
            including `deletable` ("any"/"true"/"false") and `status`
            ("any"/"active"/"expired") filters.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        current_epoch = await get_walrus_epoch(client)
        owner = client.pysui_client.config.active_address
        objects_result = await client.execute_for_all(
            command=GetObjectsOwnedByAddress(owner=owner)
        )
    if not objects_result.is_ok():
        print(
            f"Error listing owned objects: {objects_result.result_string}",
            file=sys.stderr,
        )
        sys.exit(1)

    found = False
    for obj in objects_result.result_data.objects:
        if not (obj.object_type and "::blob::Blob" in obj.object_type):
            continue
        if not (obj.json and obj.json.struct_value):
            continue
        fields = obj.json.struct_value.fields

        end_epoch = 0
        storage_val = fields.get("storage")
        if storage_val and storage_val.struct_value:
            end_epoch_val = storage_val.struct_value.fields.get("end_epoch")
            end_epoch = int(end_epoch_val.number_value) if end_epoch_val else 0
        status = "expired" if end_epoch <= current_epoch else "active"

        deletable_val = fields.get("deletable")
        deletable = bool(deletable_val and deletable_val.bool_value)

        if args.deletable != "any" and str(deletable).lower() != args.deletable:
            continue
        if args.status != "any" and status != args.status:
            continue

        blob_id_b64 = ""
        blob_id_val = fields.get("blob_id")
        if blob_id_val and blob_id_val.string_value:
            blob_id_b64 = (
                base64.urlsafe_b64encode(
                    int(blob_id_val.string_value).to_bytes(32, byteorder="little")
                )
                .rstrip(b"=")
                .decode()
            )

        found = True
        print(
            f"{obj.object_id}  blob_id={blob_id_b64}  "
            f"deletable={deletable}  end_epoch={end_epoch}  status={status}"
        )

    if not found:
        print("No blobs found matching the given filters.")


async def blob(args: argparse.Namespace) -> None:
    """Show full on-chain details for one blob object.

    Args:
        args (argparse.Namespace): Parsed `blob` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(command=GetObject(object_id=args.blobid))
    if not result.is_ok():
        print(f"Error fetching object: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))
