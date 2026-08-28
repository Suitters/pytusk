#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Shared types and errors used by two or more upload relay pipeline stages.

See :mod:`pytusk.core.relay_upload` (the package's ``__init__.py``) for the
full relay upload description -- stage ordering, why the tip and the
registration must share a single transaction, and why a failed POST is
reported as an outcome rather than raised.
"""

import dataclasses

from pytusk.core.types import (  # noqa: F401 -- AuthPackage is a back-compat re-export; moved to pytusk.core.types
    AuthPackage,
    TipKind,
)

# RelayOutcome moved to pytusk.core.types (see pytusk.core.types.outcomes).
# RelayStageTimings, RelayBlobReceipt, and RelayUploadResult moved to
# pytusk.core.types (see pytusk.core.types.receipts). RelayUploadError,
# TipConfigError, TipPaymentError, and RelayCertificateParseError moved to
# pytusk.core.types (see pytusk.core.types.errors). AuthPackage moved to
# pytusk.core.types (see pytusk.core.types.tips) so that
# pytusk.core.ops.tip_compose.add_tip -- which needs the type but must stay
# import-cycle-free of pytusk.core.relay_upload -- can reach it; re-imported
# here (rather than re-defined) for backward compatibility, since
# pytusk.core.relay_upload.__init__ and pytusk.core.relay_upload.tip both
# import it from this module.


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
