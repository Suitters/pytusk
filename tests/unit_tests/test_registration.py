#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for the registration seam.

Conformance is asserted TWICE, deliberately. ``isinstance`` against a
``runtime_checkable`` Protocol proves only that the member NAMES exist -- it
does not check a single signature, so an implementation whose ``register``
took the wrong arguments entirely would still pass it. The static-proof
functions close that gap: they are never called at runtime and exist so the
type checker validates that each implementation's ``register`` actually
matches the contract's parameters and return type. Either half alone would
keep passing while the seam was broken in the way the other one catches.

This mirrors the two patterns already established in the suite --
``tests/unit_tests/test_delivery.py`` for the delivery seam's runtime checks
and ``tests/unit_tests/test_receipt_protocols.py`` for the receipt
protocols' static proofs.
"""

import pytest

from pytusk.core.pipelines.registration import (
    BlobRegistration,
    PlainBlobRegistration,
    TippedBlobRegistration,
)
from pytusk.core.relay_upload.tip import FROM_GAS, build_auth_package
from pytusk.core.types import TipComposition


def _assert_plain_conforms(registration: PlainBlobRegistration) -> BlobRegistration:
    """Static proof: ``PlainBlobRegistration`` satisfies ``BlobRegistration``."""
    return registration


def _assert_tipped_conforms(registration: TippedBlobRegistration) -> BlobRegistration:
    """Static proof: ``TippedBlobRegistration`` satisfies ``BlobRegistration``."""
    return registration


def _tip() -> TipComposition:
    """Build a minimal tip composition for constructing the tipped variant.

    A real authentication package is built rather than a stand-in: it is a
    local, synchronous computation, and it keeps the fixture honest about the
    type the live pipeline actually passes.

    Returns:
        TipComposition: A tip composition payable from the gas coin.
    """
    return TipComposition(
        relay_address="0xrelay",
        tip_amount=1000,
        auth_package=build_auth_package(data=b"x"),
        payment_coin=FROM_GAS,
    )


class TestProtocolConformance:
    """Both registration variants must satisfy the declared seam."""

    def test_plain_registration_satisfies_protocol(self) -> None:
        """PlainBlobRegistration is a BlobRegistration."""
        assert isinstance(PlainBlobRegistration(), BlobRegistration)

    def test_tipped_registration_satisfies_protocol(self) -> None:
        """TippedBlobRegistration is a BlobRegistration."""
        assert isinstance(
            TippedBlobRegistration(
                tip=_tip(),
                wal_payment_coin="0xwal",
                sender="0xsender",
            ),
            BlobRegistration,
        )

    def test_plain_registration_defaults_are_all_optional(self) -> None:
        """The plain variant is constructible with no configuration at all.

        Native's Tx1 supplies sender, sponsor, and payment coin only when the
        caller chose them; every one of them resolves downstream when absent.
        """
        plain = PlainBlobRegistration()
        assert plain.payment_coin is None
        assert plain.sender is None
        assert plain.sponsor is None

    def test_tipped_registration_requires_a_tip(self) -> None:
        """The tipped variant cannot be constructed without a tip.

        A no-tip relay write is a ``PlainBlobRegistration``, so there is no
        reachable state in which this class is "tipped" without a tip. The
        required field is what enforces that.
        """
        with pytest.raises(TypeError):
            TippedBlobRegistration(  # type: ignore[call-arg]
                wal_payment_coin="0xwal",
                sender="0xsender",
            )
