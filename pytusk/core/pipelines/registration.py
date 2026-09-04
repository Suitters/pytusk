#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""The registration seam: how Tx1 creates the on-chain object a write proceeds against.

Every write path must, before a single byte reaches a storage node, put an
object on chain that the blob is written against and pay for the storage it
occupies. That is Tx1. What Tx1 CONTAINS, however, is not the same on every
path -- a relay write bundles its tip payment into the very same PTB, and a
future storage-pool write registers a structurally different object -- and
:class:`BlobRegistration` is the one seam that expresses the difference.

This is the sibling of :mod:`pytusk.core.pipelines.delivery`. Between them
they carve the write path at its only two real joints: how the blob gets
registered, and how it reaches storage. Everything else -- encode, the
committee read, package-ID resolution, Tx2 -- is common to every path, which
is why it lives in :mod:`pytusk.core.pipelines.write` and not behind a
contract.

FAILURE IS RAISED, NOT REPORTED on this seam -- the exact opposite of
:class:`~pytusk.core.pipelines.delivery.BlobDelivery`, and for the exact
reason that makes delivery report. Registration IS the spend. A failure here
either happened before anything was paid for, in which case there is no
on-chain state to describe and an exception discards nothing, or it is
:class:`~pytusk.core.types.RegistrationPendingError` -- Tx1 succeeded and only
its read-back lagged -- which each pipeline converts into its own receipt type
at its own boundary. That conversion deliberately stays in the pipelines:
the native and relay receipts are different shapes, and a seam that tried to
build either would have to know which pipeline called it.

POOLED REGISTRATION IS NOT IMPLEMENTED HERE. ``PooledBlob`` (backlog #5) is
the third implementation of this contract, not a layer above it and not a
subsystem beside it: it is an alternative registration path that still
register-then-certifies, paid for in WAL only, whose object carries a
``storage_pool_id`` instead of an embedded ``Storage``. It slots in as a
``PooledBlobRegistration`` next to the two classes below, and the pipelines
that call this seam need not learn anything new to reach it. Standing the
contract up now, with the two paths that exist today, is what makes that
true; the pool implementation itself is out of scope for this refactor.
"""

import dataclasses
import typing
from collections.abc import Mapping

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.encoding.redstuff import EncodedBlob
from pytusk.core.ops.blob_compose import add_registration_sequence
from pytusk.core.ops.blob_execute import (
    execute_registration_txn,
    execute_reserve_and_register,
)
from pytusk.core.types import Registration, TipComposition

__all__ = [
    "BlobRegistration",
    "PlainBlobRegistration",
    "TippedBlobRegistration",
]


@typing.runtime_checkable
class BlobRegistration(typing.Protocol):
    """How a blob is registered on chain, and what Tx1 costs and contains.

    The write path's first branch point. Path-specific configuration --
    which coin pays, who sends, whether a relay tip rides along in the same
    PTB -- belongs on the IMPLEMENTATION, supplied when it is constructed,
    exactly as it does on
    :class:`~pytusk.core.pipelines.delivery.BlobDelivery`. A tip means
    nothing to a native write and a caller-resolved WAL coin means nothing
    to a path that selects its own, so neither appears in :meth:`register`'s
    parameters. What remains is what every registration variant genuinely
    needs to describe the same blob.
    """

    async def register(
        self,
        *,
        client: WalrusClient,
        encoded: EncodedBlob,
        epochs: int,
        deletable: bool,
        package_id: str,
        system_object: str,
    ) -> Registration:
        """Register the blob on chain and return Tx1's result.

        Args:
            client (WalrusClient): Client used to build, sign, and submit Tx1.
            encoded (EncodedBlob): The RedStuff-encoded blob being registered.
                Supplies the blob ID, root hash, and the unencoded length that
                ``reserve_space`` charges storage against.
            epochs (int): Number of epochs ahead to reserve storage for.
            deletable (bool): Whether the registered blob should be deletable.
            package_id (str): The resolved Walrus package ID.
            system_object (str): Object ID of the configured Walrus System
                object.

        Returns:
            Registration: The recoverable checkpoint between Tx1 and Tx2.
                Implementations may return a subtype; a Protocol's return
                type is checked COVARIANTLY, so doing so satisfies this
                contract without widening it for anyone.

        Raises:
            RegistrationPendingError: Tx1 SUCCEEDED on chain but its read-back
                lagged. This is post-spend and recoverable, and each pipeline
                converts it into its own receipt type rather than letting it
                escape to a caller.
            NativeUploadError | RelayUploadError | RuntimeError | ValueError:
                Whatever the underlying execute layer raises for a Tx1 that
                did not land. Nothing was spent, so nothing is lost by
                raising.
        """
        ...


@dataclasses.dataclass(kw_only=True, frozen=True)
class PlainBlobRegistration:
    """Register a plain, owned ``Blob`` -- ``reserve_space`` + ``register_blob``.

    The registration every write path starts from, and the one both the
    native pipeline and a no-tip relay write use. It delegates wholly to
    :func:`~pytusk.core.ops.blob_execute.execute_reserve_and_register`,
    which already composes Tx1 through the shared
    :func:`~pytusk.core.ops.blob_compose.add_registration_sequence` seam --
    so adopting this contract adds no second way to build Tx1, it only gives
    the existing one a name the pipelines can select between.

    A relay write reaches this class whenever its tip quote requires no
    payment. That is deliberate reuse and not a coincidence of shape: with
    no tip to bundle, a relay's Tx1 IS a plain registration, composing the
    identical PTB. The one behaviour
    :func:`~pytusk.core.ops.blob_execute.execute_reserve_and_register` does
    not perform -- the sponsor-signability fail-fast check -- is already run
    by :func:`~pytusk.core.pipelines.write.store_blob_relay` at the top of
    its pipeline, before any encode or network work, so nothing is lost by
    routing through here.

    Attributes:
        payment_coin (str | None): Object ID of a ``Coin<WAL>`` to pay for
            storage with. ``None`` lets the execute layer select one, which
            costs a network read; a caller that has already resolved a coin
            (as the relay pipeline has) passes it and skips that.
        sender (str | None): Address to sign Tx1 as, and the address the new
            ``Blob`` is transferred to. ``None`` resolves to the active
            address. It is always the ``Blob``'s owner after Tx1, because Tx2
            takes it as ``&mut Blob`` and must be signed by whoever owns it.
        sponsor (str | None): Address to sponsor Tx1's gas, or ``None``.
        attributes (Mapping[str, str] | None): Key/value pairs written onto
            the new ``Blob`` as on-chain metadata inside Tx1 itself, one PTB
            command per pair. ``None`` (the default) writes none.
    """

    payment_coin: str | None = None
    sender: str | None = None
    sponsor: str | None = None
    attributes: Mapping[str, str] | None = None

    async def register(
        self,
        *,
        client: WalrusClient,
        encoded: EncodedBlob,
        epochs: int,
        deletable: bool,
        package_id: str,
        system_object: str,
    ) -> Registration:
        """Compose and submit a plain ``reserve_space`` + ``register_blob`` Tx1.

        Args:
            client (WalrusClient): Client used to build, sign, and submit Tx1.
            encoded (EncodedBlob): The RedStuff-encoded blob being registered.
            epochs (int): Number of epochs ahead to reserve storage for.
            deletable (bool): Whether the registered blob should be deletable.
            package_id (str): The resolved Walrus package ID.
            system_object (str): Object ID of the configured Walrus System
                object.

        Returns:
            Registration: Tx1's result, read back from the created ``Blob``.

        Raises:
            RegistrationPendingError: Tx1 landed but its read-back lagged.
            RuntimeError | ValueError: Tx1 did not land.
        """
        return await execute_reserve_and_register(
            client=client,
            encoded=encoded,
            epochs=epochs,
            deletable=deletable,
            package_id=package_id,
            system_object=system_object,
            payment_coin=self.payment_coin,
            sender=self.sender,
            sponsor=self.sponsor,
            attributes=self.attributes,
        )


@dataclasses.dataclass(kw_only=True, frozen=True)
class TippedBlobRegistration:
    """Register a plain ``Blob`` with a relay tip bundled into the SAME PTB.

    A relay independently re-verifies its payment on chain before it will
    accept an upload, and it reads the authentication package from PTB input
    0. Both facts force the tip into Tx1 itself rather than into a
    transaction beside it, which is the whole reason this second
    implementation exists.

    ``tip`` is REQUIRED, never an optional field defaulting to ``None``. A
    no-tip relay write is a :class:`PlainBlobRegistration` -- selected by the
    pipeline on whether the quote requires payment -- so there is no reachable
    state in which an instance of this class is "tipped" without a tip. The
    alternative, one class with an optional tip, would hand every native
    write a relay-only field to ignore.

    ``wal_payment_coin`` is REQUIRED for the same reason it is required on
    :func:`~pytusk.core.ops.blob_compose.add_registration_sequence`:
    resolving "no coin given" into a concrete coin id needs a client and a
    round trip, and the relay pipeline has already done exactly that through
    :func:`~pytusk.core.ops.blob_execute.preflight_payment` in order to check
    the tip coin at the same time.

    Attributes:
        tip (TipComposition): Everything
            :func:`~pytusk.core.ops.tip_compose.add_tip` needs to bundle the
            relay tip into this PTB, composed FIRST so the authentication
            package lands at input 0.
        wal_payment_coin (str): Object ID of a ``Coin<WAL>``, already resolved
            by the caller, paying for storage.
        sender (str): Resolved address to sign Tx1 as, and the address the new
            ``Blob`` is transferred to. Not optional here: the relay pipeline
            resolves the sender up front because its tip-coin preflight needs
            a concrete address to verify against.
        sponsor (str | None): Address to sponsor Tx1's gas, or ``None``. Must
            be signable in the active configuration; the relay pipeline
            checks that before anything is spent.
        attributes (Mapping[str, str] | None): Key/value pairs written onto
            the new ``Blob`` as on-chain metadata inside this same PTB, one
            command per pair, composed after registration and before the
            transfer that consumes the ``Blob``. ``None`` (the default)
            writes none.
    """

    tip: TipComposition
    wal_payment_coin: str
    sender: str
    sponsor: str | None = None
    attributes: Mapping[str, str] | None = None

    async def register(
        self,
        *,
        client: WalrusClient,
        encoded: EncodedBlob,
        epochs: int,
        deletable: bool,
        package_id: str,
        system_object: str,
    ) -> Registration:
        """Compose tip + ``reserve_space`` + ``register_blob`` as one Tx1.

        The order is fixed and load-bearing --
        :func:`~pytusk.core.ops.blob_compose.add_registration_sequence` owns
        it, and will refuse to compose a tip into a transaction that already
        holds an input or a command.

        Args:
            client (WalrusClient): Client used to build, sign, and submit Tx1.
            encoded (EncodedBlob): The RedStuff-encoded blob being registered.
            epochs (int): Number of epochs ahead to reserve storage for.
            deletable (bool): Whether the registered blob should be deletable.
            package_id (str): The resolved Walrus package ID.
            system_object (str): Object ID of the configured Walrus System
                object.

        Returns:
            Registration: Tx1's result, read back from the created ``Blob``.

        Raises:
            TipPaymentError: The transaction already held an input or command
                when the tip was composed.
            RegistrationPendingError: Tx1 landed but its read-back lagged.
            RuntimeError | ValueError: Tx1 did not land.
        """
        # attributes ride inside Tx1 rather than a follow-up transaction:
        # a second transaction could fail after the blob is already paid
        # for, leaving a registered blob whose type is unset.
        txn = await client.transaction(
            initial_sender=self.sender, initial_sponsor=self.sponsor
        )
        await add_registration_sequence(
            txn=txn,
            encoded=encoded,
            epochs=epochs,
            deletable=deletable,
            package_id=package_id,
            system_object=system_object,
            recipient=self.sender,
            wal_payment_coin=self.wal_payment_coin,
            tip=self.tip,
            attributes=self.attributes,
        )
        return await execute_registration_txn(
            client=client,
            txn=txn,
            encoded=encoded,
            owner=self.sender,
            label="tip/reserve_space/register_blob",
        )
