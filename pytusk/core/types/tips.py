#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Tip formula and tip-policy types shared by relay transport and
orchestration.

They are pure data with no behaviour beyond deriving from their own fields,
which is what makes them safe to share across the boundary between
:mod:`pytusk.commands.relay_commands` (transport) and
:mod:`pytusk.core.relay_upload` (orchestration).

:class:`AuthPackage`, :data:`FROM_GAS`, and :class:`TipComposition` also
live here -- rather than in :mod:`pytusk.core.relay_upload.common`, their
original home -- for the same reason :class:`~pytusk.core.types.TipPaymentError`
and friends already moved to :mod:`pytusk.core.types`: this package is a
LEAF that both :mod:`pytusk.core.ops` (specifically
:func:`~pytusk.core.ops.tip_compose.add_tip`, which composes a tip payment
into a caller's transaction with no client) and
:mod:`pytusk.core.relay_upload` can import from without a cycle.
:mod:`pytusk.core.relay_upload.common` re-exports :class:`AuthPackage` for
backward compatibility, and :mod:`pytusk.core.relay_upload.tip` re-exports
:data:`FROM_GAS` the same way.
"""

import base64
import dataclasses


@dataclasses.dataclass(kw_only=True, frozen=True)
class ConstTip:
    """A flat tip, charged per upload regardless of blob size.

    Mirrors Walrus's ``TipKind::Const(u64)``, whose wire form is serde's
    externally tagged ``{"const": <amount>}``.

    Attributes:
        amount (int): Tip in MIST, charged unconditionally.
    """

    amount: int


@dataclasses.dataclass(kw_only=True, frozen=True)
class LinearTip:
    """A tip that scales with the ENCODED size of the blob.

    The tip is ``base + encoded_size_mul_per_kib * ceil(encoded_length /
    1024)``, where ``encoded_length`` is the fully RS-encoded blob length
    (per-shard metadata plus every shard's slivers) -- NOT the unencoded
    byte count the caller supplied. The unencoded length is only an INPUT
    to the encoded-length calculation.

    Walrus's own doc comment for this variant writes the rounding as a
    floor (``encoded_size_in_bytes / 1024``), but its implementation calls
    ``div_ceil(1024)``. The implementation is authoritative: pytusk rounds
    UP, so an encoded length of exactly 1024 bytes bills one KiB and 1025
    bytes bills two. Mirrors Walrus's ``TipKind::Linear``, whose wire form
    is ``{"linear": {"base": N, "encoded_size_mul_per_kib": N}}``.

    Attributes:
        base (int): Flat component in MIST, charged on every upload.
        encoded_size_mul_per_kib (int): MIST charged per KiB (rounded up)
            of encoded blob length, added to ``base``.
    """

    base: int
    encoded_size_mul_per_kib: int


# How a relay computes its tip. Mirrors Walrus's ``TipKind`` enum; the two
# members are exhaustive, so a match over them needs no fallback arm.
TipKind = ConstTip | LinearTip


@dataclasses.dataclass(kw_only=True, frozen=True)
class TipConfig:
    """A relay's advertised tipping policy, from ``GET /v1/tip-config``.

    Mirrors Walrus's ``TipConfig`` enum. Both Walrus enums are serde
    externally tagged with ``rename_all = "snake_case"``, so a relay that
    charges nothing serialises as the BARE JSON STRING ``"no_tip"`` -- not
    an object -- and is represented here with both fields ``None``. A relay
    that charges serialises as ``{"send_tip": {"address": ..., "kind":
    ...}}``.

    Attributes:
        address (str | None): Sui address the tip is transferred to, or
            ``None`` when the relay requires no tip.
        kind (TipKind | None): How the tip amount is computed, or ``None``
            when the relay requires no tip.
    """

    address: str | None
    kind: TipKind | None

    @property
    def requires_payment(self) -> bool:
        """Whether this relay requires a tip before it will accept an upload.

        Returns:
            bool: True when a tip must be paid, False for a ``no_tip`` relay.
        """
        return self.kind is not None


@dataclasses.dataclass(kw_only=True, frozen=True)
class TipQuote:
    """A relay's tip policy resolved against one specific blob.

    :class:`TipConfig` says how a relay charges; this says what THIS upload
    will actually cost, so a caller can show or approve the figure before
    any transaction is built. It is also what tusky's simulate summary
    reports, which needs no chain state and no registration.

    Attributes:
        address (str | None): Sui address the tip is transferred to, or
            ``None`` when the relay requires no tip.
        amount (int | None): The computed tip in MIST, or ``None`` when the
            relay requires no tip.
        kind (TipKind | None): The formula the amount came from, kept so a
            caller can explain the figure rather than just quote it.
    """

    address: str | None
    amount: int | None
    kind: TipKind | None

    @property
    def requires_payment(self) -> bool:
        """Whether this upload must pay a tip.

        Returns:
            bool: True when a tip must be paid, False for a ``no_tip`` relay.
        """
        return self.kind is not None


FROM_GAS: str = "from_gas"
"""Sentinel for a tip's ``payment_coin``: split the tip from whatever funds
the transaction.

Deliberately a sentinel rather than passing pysui's gas argument directly.
Under sponsorship the gas coin belongs to the SPONSOR, so defaulting to it
silently bills a third party for the caller's tip. Naming ``FROM_GAS`` makes
that an explicit choice; an explicit coin id is the alternative.
"""


@dataclasses.dataclass(kw_only=True, frozen=True)
class AuthPackage:
    """The authentication package binding a tip payment to one blob upload.

    Walrus calls this ``HashedAuthPackage``. Its BCS encoding is the FIRST
    input of the register+tip transaction -- ``add_tip`` refuses to compose
    into a transaction that already has inputs, because the relay looks for
    the package at input 0 and nowhere else.

    The package commits to the blob and to a nonce, NOT to the tip amount.
    That is what makes an already-paid tip reusable: the relay implements no
    replay protection, so re-POSTing with the same ``tx_id`` and ``nonce`` is
    accepted and never re-charges. A resumed upload must therefore reuse the
    ORIGINAL package -- building a fresh one abandons the tip already paid.

    Attributes:
        nonce (bytes): 32 random bytes, generated per attempt via
            :func:`secrets.token_bytes`. Sent to the relay as
            :attr:`nonce_base64url`; the transaction commits only to its
            digest, never to the nonce itself.
        blob_digest (bytes): SHA-256 of the unencoded blob bytes.
        nonce_digest (bytes): SHA-256 of :attr:`nonce`.
        unencoded_length (int): Length in bytes of the unencoded blob.
    """

    nonce: bytes
    blob_digest: bytes
    nonce_digest: bytes
    unencoded_length: int

    @property
    def bcs(self) -> bytes:
        """BCS encoding of the package: always exactly 72 bytes.

        BCS writes a fixed-size byte array with no length prefix and a
        ``u64`` little-endian, so the layout is ``blob_digest`` (32 bytes)
        || ``nonce_digest`` (32 bytes) || ``unencoded_length`` (8 bytes).

        Returns:
            bytes: The 72-byte encoding used as transaction input 0.
        """
        return (
            self.blob_digest
            + self.nonce_digest
            + self.unencoded_length.to_bytes(8, "little")
        )

    @property
    def nonce_base64url(self) -> str:
        """The nonce as URL-safe, UNPADDED base64, as the relay expects it.

        Returns:
            str: Base64url encoding of :attr:`nonce` with ``=`` padding
                stripped.
        """
        return base64.urlsafe_b64encode(self.nonce).rstrip(b"=").decode("ascii")


@dataclasses.dataclass(kw_only=True, frozen=True)
class TipComposition:
    """Everything :func:`~pytusk.core.ops.tip_compose.add_tip` needs to
    compose a relay tip into a caller's transaction, bundled so
    :func:`~pytusk.core.ops.blob_compose.add_registration_sequence` can take
    a single optional ``tip`` argument instead of four separate ones.

    Attributes:
        relay_address (str): Sui address to transfer the tip to, from the
            relay's tip config (:attr:`TipQuote.address`).
        tip_amount (int): Tip in MIST, as computed by
            :func:`~pytusk.core.relay_upload.tip.compute_tip`.
        auth_package (AuthPackage): Package binding this tip to one blob.
        payment_coin (str): :data:`FROM_GAS` to split from whatever funds
            the transaction, or an explicit coin object id.
    """

    relay_address: str
    tip_amount: int
    auth_package: AuthPackage
    payment_coin: str = FROM_GAS


@dataclasses.dataclass(kw_only=True, frozen=True)
class TipResult:
    """The outcome of a standalone tip payment.

    Returned by :func:`~pytusk.core.relay_upload.tip.execute_tip`, which is
    the composable escape hatch: the encapsulated pipeline bundles the tip
    into Tx1 and never calls it. Its reason to exist is resuming -- paying a
    fresh tip for an upload whose earlier attempt is no longer usable -- so
    it carries the tokens a later POST needs, not merely a digest.

    Attributes:
        digest (str): Digest of the tip transaction, sent to the relay as
            its ``tx_id`` query parameter.
        nonce (str): Base64url nonce from the authentication package, sent
            alongside the digest. The relay checks both.
        relay_address (str): Sui address the tip was transferred to.
        tip_amount (int): Tip paid, in MIST.
    """

    digest: str
    nonce: str
    relay_address: str
    tip_amount: int
