#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Native upload command handlers for the tusky CLI.

Handlers for storing/certifying blobs via the native Walrus upload
pipeline (reserve_space+register_blob, sliver fan-out, certify_blob), as
opposed to the Walrus HTTP publisher. Each handler takes the parsed
argparse.Namespace for its subcommand and performs the corresponding
pytusk operation. Handlers are async; tusky.py drives them via
asyncio.run.
"""

import argparse
import asyncio
import dataclasses
import functools
import io
import json
import logging
import sys
import time
from pathlib import Path

from pysui import GetObject
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk import (
    BlobTooLargeError,
    CertifyTransactionError,
    NativeUploadError,
    Registration,
    StageTimings,
    WalrusClient,
    add_certify,
    add_registration_sequence,
    blob_deletable_and_end_epoch,
    blob_id_from_object,
    certify,
    collect_confirmations,
    encode_blob,
    encoded_blob_length,
    preflight_payment,
    upload_slivers,
)
from pytusk import store_blob_native as _store_blob_native_pipeline
from pytusk.tusky.tusky_cmds_common import (
    config_from_args,
    read_file_bytes,
    resolve_sender,
    resolve_sponsor,
    simulate_cost_from_balance_changes,
    submit,
    walrus_package_id,
)

# --- tusky CLI logging configuration ------------------------------------
# pytusk (the library, everything outside pytusk/tusky/) only ever emits
# log records -- it never configures handlers, levels, or any other
# global logging state (see pytusk/__init__.py's NullHandler). tusky, as
# an APPLICATION built on top of pytusk, is entitled to configure logging,
# but only when the user explicitly asks for it via --log-file/--verbose
# on store_blob_native (the only command with progress/heartbeat
# instrumentation today -- see pytusk.core.native_upload.common's
# _HEARTBEAT_INTERVAL_SECONDS and its module-level comment), and never by
# writing to a derived or default path.
# ------------------------------------------------------------------------

_NATIVE_UPLOAD_LOGGING_CONFIGURED: bool = False


def _configure_native_upload_logging(
    *, log_file: Path | None, verbose: bool
) -> None:
    """Configure the ``pytusk`` logger hierarchy per the user's CLI request.

    This is tusky's own opt-in logging setup, not library scaffolding --
    see the module comment immediately above. Adds an INFO-level stdout
    stream handler to the ``pytusk`` logger only when ``verbose`` is True,
    and/or an INFO-level file handler at exactly ``log_file`` only when it
    is given -- NEVER a derived or default path. Does nothing at all when
    neither is requested. The ``pytusk`` logger is the parent of every
    ``pytusk.core.native_upload`` submodule's own
    ``logging.getLogger(__name__)`` (``fanout``, ``confirm``, etc.), so
    their progress/heartbeat records propagate up to whichever
    destination(s) were configured. When ``verbose`` is True, stdout is
    also reconfigured for line buffering so progress is visible live even
    when output is redirected to a file. Idempotent: a second call in the
    same process is a no-op, so handlers are never duplicated.

    Args:
        log_file (Path | None): Path to write an INFO-level log file to,
            exactly as given (no default, no derived path); ``None`` to
            skip file logging.
        verbose (bool): Whether to emit INFO-level progress to stdout.
    """
    # Idempotent one-time setup guard.
    global _NATIVE_UPLOAD_LOGGING_CONFIGURED  # pylint: disable=global-statement

    if not log_file and not verbose:
        return
    if _NATIVE_UPLOAD_LOGGING_CONFIGURED:
        return

    logger = logging.getLogger("pytusk")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    if verbose:
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(line_buffering=True)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    if log_file is not None:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _NATIVE_UPLOAD_LOGGING_CONFIGURED = True


async def store_blob_native(args: argparse.Namespace) -> None:
    """Store a blob via the native Walrus upload pipeline and print the receipt.

    Content comes from --content (UTF-8 text) or --file (raw bytes), whichever
    was given. In execute mode (the full pipeline: reserve_space+register_blob,
    sliver fan-out, confirmation collection, certify_blob) this delegates to
    the library's :func:`~pytusk.core.pipelines.write.store_blob_native`
    convenience. In simulate mode ONLY Tx1 (reserve_space+register_blob) is
    simulated -- a real simulation, unlike the rest of the pipeline, which is
    skipped because simulation never registers the blob on-chain, so storage
    nodes would reject sliver PUTs and there would be nothing to certify.

    Simulate mode prints a concise JSON cost summary by DEFAULT (blob size,
    encoded storage_amount, epochs, SUI/WAL cost, stage timings) rather than
    the full raw simulate transaction JSON, since the latter can run to
    thousands of lines (a multi-kilobyte base64 Walrus-state dump included)
    for a single-digit-line answer to "what will this cost?". Pass
    --full-json to additionally print the complete raw simulate result.

    Pass --log-file PATH to additionally write an INFO-level log of this
    run's progress to PATH, and/or --verbose to emit that same INFO-level
    progress to stdout live; neither is enabled by default (see
    _configure_native_upload_logging()).

    Args:
        args (argparse.Namespace): Parsed `store_blob_native` subcommand
            arguments.
    """
    # tusky's opt-in logging setup -- see the module comment near
    # _configure_native_upload_logging() above. Does nothing unless the
    # user passed --log-file and/or --verbose.
    _configure_native_upload_logging(log_file=args.log_file, verbose=args.verbose)
    if args.log_file is not None:
        print(f"Native upload log: {args.log_file}")

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
        try:
            sender = resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        if args.mode == "execute":
            try:
                receipt = await _store_blob_native_pipeline(
                    client=client,
                    data=data,
                    epochs=args.epochs,
                    deletable=not args.permanent,
                    sender=sender,
                    sponsor=sponsor,
                    recipient=args.recipient,
                )
            except BlobTooLargeError as exc:
                print(f"Error in encode: {exc}", file=sys.stderr)
                sys.exit(1)
            except NativeUploadError as exc:
                print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
                sys.exit(1)
            # RegistrationPendingError is no longer caught here: the
            # pipeline converts it, at its own boundary, into a
            # NativeBlobReceipt with certified=False and failed_stage set
            # ("register_finality" or "register_readback") -- handled by
            # the receipt.failed_stage branch below like any other
            # partial/failed receipt.
            except (RuntimeError, KeyError, TypeError, ValueError) as exc:
                # Everything before registration (committee/epoch reads,
                # package-ID resolution, WAL coin selection, and Tx1 itself)
                # is bucketed under "register (Tx1)" -- the library does not
                # distinguish these sub-stages the way it does for the
                # post-registration NativeUploadError family.
                print(f"Error in register (Tx1): {exc}", file=sys.stderr)
                sys.exit(1)

            if receipt.failed_stage is not None:
                print(json.dumps(dataclasses.asdict(receipt), indent=2))
                print(
                    f"Error in {receipt.failed_stage}: upload did not "
                    "complete (blob registered but not certified).",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(json.dumps(dataclasses.asdict(receipt), indent=2))
            return

        # --mode simulate: Tx1 only. Only encode and Tx1 build+simulate
        # actually run, so only those two stages get a timing -- the rest
        # are honestly reported as None (they did not run).
        pipeline_start = time.monotonic()
        try:
            committee = await client.committee()  # pylint: disable=redefined-outer-name
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"Error in register (Tx1): {exc}", file=sys.stderr)
            sys.exit(1)
        encode_start = time.monotonic()
        try:
            encoded = await asyncio.to_thread(
                functools.partial(encode_blob, data=data, n_shards=committee.n_shards)
            )
        except BlobTooLargeError as exc:
            print(f"Error in encode: {exc}", file=sys.stderr)
            sys.exit(1)
        encode_duration = time.monotonic() - encode_start

        system_obj_id, walrus_pkg = await walrus_package_id(client=client)
        try:
            resolved_wal_coin = await preflight_payment(
                client=client,
                sender=sender,
                sponsor=sponsor,
                wal_payment_coin=None,
            )
        except RuntimeError as exc:
            print(f"Error selecting WAL payment coin: {exc}", file=sys.stderr)
            sys.exit(1)

        register_tx1_start = time.monotonic()
        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        # No sponsor-signability preflight here -- matches
        # execute_reserve_and_register, which deliberately skips it: a
        # sponsor absent from the active PysuiConfiguration may sign this
        # transaction out-of-band, which is a legitimate pattern here, not
        # an error (see execute_reserve_and_register's docstring).
        # add_registration_sequence composes reserve_space+register_blob
        # and the transfer that consumes the new Blob -- tip=None, since
        # the native path never bundles a relay tip.
        await add_registration_sequence(
            txn=txn,
            encoded=encoded,
            epochs=args.epochs,
            deletable=not args.permanent,
            package_id=walrus_pkg,
            system_object=system_obj_id,
            recipient=sender,
            wal_payment_coin=resolved_wal_coin,
            tip=None,
        )
        # Tx1 always transfers the newly registered Blob to the sender --
        # matching pytusk.core.ops.blob_execute.execute_reserve_and_register.
        # --recipient (when given) is handled by Tx2 (certify_blob), which
        # this simulate mode does NOT model (see the "notice" field below),
        # so it plays no part in this Tx1-only cost estimate.
        txdict = await txn.build_and_sign()
        result = await submit(client=client, txdict=txdict, mode="simulate")
        register_tx1_duration = time.monotonic() - register_tx1_start
        if not result.is_ok():
            print(f"Error in register (Tx1): {result.result_string}", file=sys.stderr)
            sys.exit(1)

        if args.full_json:
            print(result.result_data.to_json(indent=2))

        storage_amount = encoded_blob_length(
            unencoded_length=encoded.unencoded_length, n_shards=encoded.n_shards
        )
        transaction = getattr(result.result_data, "transaction", None)
        sui_cost, wal_cost = await simulate_cost_from_balance_changes(
            client=client, transaction=transaction
        )
        timings = StageTimings(
            encode=encode_duration,
            register_tx1=register_tx1_duration,
            sliver_upload=None,
            confirmations=None,
            certify_tx2=None,
            total=time.monotonic() - pipeline_start,
        )
        summary = {
            "mode": "simulate",
            "blob_size_bytes": len(data),
            "encoded_storage_amount_bytes": storage_amount,
            "epochs": args.epochs,
            "permanent": args.permanent,
            "cost": {"sui": sui_cost, "wal": wal_cost},
            "timings": dataclasses.asdict(timings),
            "notice": (
                "Only Tx1 (reserve_space + register_blob) was simulated. "
                "Sliver fan-out and Tx2 (certify_blob) were SKIPPED because "
                "simulation does not register the blob on-chain, so storage "
                "nodes would reject sliver PUTs and there is nothing yet to "
                "certify. --recipient (if given) transfers the Blob as part "
                "of Tx2, so it is not reflected in this Tx1-only cost "
                "estimate; Tx1 always transfers the Blob to --sender."
            ),
        }
        print(json.dumps(summary, indent=2))


async def certify_blob(args: argparse.Namespace) -> None:
    """Recover the confirmation-collection and certify_blob stages for an
    already-registered blob.

    This is the recovery entry point for any upload -- native or relay --
    that completed Tx1 (reserve_space+register_blob) but died before or
    during Tx2 (certify_blob): given only the blob's Sui object ID, it
    re-derives the real Walrus blob ID from the on-chain Blob object's
    `blob_id` u256 field (see
    :func:`~pytusk.core.chain.blob_fields.blob_id_from_object`), re-collects
    a fresh quorum of storage-node confirmations, and certifies. It reads
    only on-chain state, so it is provenance-blind: a blob registered by
    the relay pipeline recovers identically to one registered natively.

    By default it does NOT re-upload slivers -- if the original sliver
    fan-out did not reach quorum, confirmation collection here will also
    fail. Passing ``--recover`` together with ``--content`` or ``--file``
    re-encodes the given source bytes and re-uploads slivers first, for a
    blob whose sliver fan-out never ran. The re-encoded ``blob_id`` must
    match the blob_id already registered on-chain for this object, or the
    command errors out before uploading anything.

    In simulate mode, confirmation collection is real (registration already
    exists on-chain) and only Tx2 itself is simulated -- unlike
    store_blob_native's simulate mode, this is a fully honest simulation.

    Args:
        args (argparse.Namespace): Parsed `certify_blob` subcommand arguments.
    """
    if args.recover and not (args.content or args.file):
        print("Error: --recover requires --content or --file.", file=sys.stderr)
        sys.exit(1)
    if not args.recover and (args.content or args.file):
        print("Error: --content/--file require --recover.", file=sys.stderr)
        sys.exit(1)

    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        pipeline_start = time.monotonic()
        try:
            sender = resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        blob_result = await client.execute(command=GetObject(object_id=args.blobid))
        if not blob_result.is_ok():
            print(
                f"Error fetching blob object: {blob_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        obj = blob_result.result_data
        if not (obj.object_type and "::blob::Blob" in obj.object_type):
            print(f"{args.blobid} is not a Walrus Blob object.", file=sys.stderr)
            sys.exit(1)
        try:
            blob_id_bytes = blob_id_from_object(obj=obj)
            deletable, end_epoch = blob_deletable_and_end_epoch(obj=obj)
        except ValueError as exc:
            print(f"Error reading blob {args.blobid}: {exc}", file=sys.stderr)
            sys.exit(1)

        try:
            committee = await client.committee()  # pylint: disable=redefined-outer-name
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"Cannot get Walrus committee: {exc}", file=sys.stderr)
            sys.exit(1)

        encode_duration: float | None = None
        sliver_upload_duration: float | None = None
        if args.recover:
            if args.file:
                try:
                    data = await asyncio.to_thread(read_file_bytes, args.file)
                except OSError as exc:
                    print(f"Error reading file {args.file}: {exc}", file=sys.stderr)
                    sys.exit(1)
            else:
                data = args.content.encode("utf-8")

            encode_start = time.monotonic()
            encoded = await asyncio.to_thread(
                functools.partial(encode_blob, data=data, n_shards=committee.n_shards)
            )
            encode_duration = time.monotonic() - encode_start

            if encoded.blob_id != blob_id_bytes:
                print(
                    "Error: the re-supplied content does not match the "
                    f"blob_id already registered for {args.blobid}.",
                    file=sys.stderr,
                )
                sys.exit(1)

            sliver_upload_start = time.monotonic()
            try:
                await upload_slivers(
                    client=client,
                    committee=committee,
                    encoded=encoded,
                )
            except NativeUploadError as exc:
                print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
                sys.exit(1)
            finally:
                sliver_upload_duration = time.monotonic() - sliver_upload_start

        registration = Registration(
            object_id=args.blobid,
            blob_id=blob_id_bytes,
            end_epoch=end_epoch,
            deletable=deletable,
            digest="",  # Tx1's digest is not known in this recovery flow.
        )
        system_obj_id, walrus_pkg = await walrus_package_id(client=client)
        staking_object = client.config.network.staking_object

        confirmations_start = time.monotonic()
        try:
            certificate = await collect_confirmations(
                client=client,
                committee=committee,
                blob_id=blob_id_bytes,
                registration=registration,
            )
        except NativeUploadError as exc:
            print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
            sys.exit(1)
        finally:
            confirmations_duration = time.monotonic() - confirmations_start

        if args.mode == "execute":
            try:
                receipt = await certify(
                    client=client,
                    committee=committee,
                    blob_id=blob_id_bytes,
                    registration=registration,
                    certificate=certificate,
                    package_id=walrus_pkg,
                    system_object=system_obj_id,
                    staking_object=staking_object,
                    sender=sender,
                    sponsor=sponsor,
                    stage_timings=StageTimings(
                        encode=encode_duration,
                        register_tx1=None,
                        sliver_upload=sliver_upload_duration,
                        confirmations=confirmations_duration,
                        certify_tx2=None,
                        total=None,
                    ),
                )
            except CertifyTransactionError as exc:
                # Tx2 (certify_blob) failed to submit or aborted on-chain.
                # Before native_upload's certify() converted this into a
                # CertifyTransactionError, it reached this handler as a
                # bare RuntimeError and was reported here with this exact
                # "(Tx2)" wording -- kept unchanged so the CLI's Tx2-failure
                # output is unaffected by that internal conversion.
                print(f"Error in certify (Tx2): {exc}", file=sys.stderr)
                sys.exit(1)
            except NativeUploadError as exc:
                print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
                sys.exit(1)

            receipt = dataclasses.replace(
                receipt,
                timings=dataclasses.replace(
                    receipt.timings, total=time.monotonic() - pipeline_start
                ),
            )
            if receipt.failed_stage is not None:
                print(json.dumps(dataclasses.asdict(receipt), indent=2))
                print(
                    f"Error in {receipt.failed_stage}: certification did "
                    "not complete.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(json.dumps(dataclasses.asdict(receipt), indent=2))
            return

        # --mode simulate: confirmations above are real; only Tx2 is simulated.
        certify_tx2_start = time.monotonic()
        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        await add_certify(
            txn=txn,
            package_id=walrus_pkg,
            system_object=system_obj_id,
            blob_object_id=args.blobid,
            certificate=certificate,
        )
        txdict = await txn.build_and_sign()
        result = await submit(client=client, txdict=txdict, mode="simulate")
        certify_tx2_duration = time.monotonic() - certify_tx2_start
        if not result.is_ok():
            print(f"Error in certify (Tx2): {result.result_string}", file=sys.stderr)
            sys.exit(1)
        print(result.result_data.to_json(indent=2))
        timings = StageTimings(
            encode=encode_duration,
            register_tx1=None,
            sliver_upload=sliver_upload_duration,
            confirmations=confirmations_duration,
            certify_tx2=certify_tx2_duration,
            total=time.monotonic() - pipeline_start,
        )
        print(json.dumps({"timings": dataclasses.asdict(timings)}, indent=2))
