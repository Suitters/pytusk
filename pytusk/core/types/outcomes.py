#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Terminal-state enums for the native upload and relay upload pipelines.

Both enums subclass ``str`` so they compare and serialise like plain
strings, with ``__str__`` overridden back to ``str.__str__`` to avoid the
``ClassName.MEMBER`` rendering ``str, Enum`` mixins otherwise produce.
"""

import enum


class RelayOutcome(str, enum.Enum):
    """Terminal state of a relay upload attempt.

    The distinction between :attr:`RESUMABLE` and :attr:`REJECTED` is the
    load-bearing one. An HTTP status alone cannot separate "the relay never
    answered" from "the relay answered no", but the response body can, and
    pytusk preserves that distinction rather than collapsing both into a
    single failure.

    pytusk must NEVER report a paid tip as lost. The relay's freshness
    threshold (operator-configurable, not exposed via ``/v1/tip-config``) is
    unobservable from the client, so claiming loss would assert something
    that cannot be checked. :attr:`RESUMABLE` therefore surfaces the
    resumption tokens -- the Tx1 digest, the blob ID, and the nonce -- so a
    later POST can reuse the tip already paid. The relay implements no
    replay protection: re-POSTing the identical ``tx_id`` and ``nonce`` is
    accepted, and never re-charges.

    Attributes:
        CERTIFIED: The blob is registered, uploaded, and certified.
        RESUMABLE: Tx1 is final and the tip is intact, but the upload did
            not complete. Retrying the POST with the receipt's tokens costs
            nothing further.
        REJECTED: The relay refused definitively (e.g. 400/401/402).
            Resuming will not help; a fixed paid tip cannot satisfy a 402.
        NOT_STARTED: The attempt failed before Tx1 executed. Nothing was
            spent.
    """

    __str__ = str.__str__

    CERTIFIED = "certified"
    RESUMABLE = "resumable"
    REJECTED = "rejected"
    NOT_STARTED = "not_started"


class RelayUploadOutcome(str, enum.Enum):
    """Terminal state of the POST stage alone.

    Deliberately NOT :class:`RelayOutcome`: the POST stage cannot occupy
    that enum's :attr:`RelayOutcome.CERTIFIED` (Tx2 has not run) or
    :attr:`RelayOutcome.NOT_STARTED` (Tx1 has) states, so reusing it would
    let a composable caller read a value the stage can never legitimately
    produce. :mod:`pytusk.core.pipelines.write` maps these onto
    :class:`RelayOutcome` for the encapsulated receipt.

    Attributes:
        UPLOADED: The relay returned a confirmation certificate.
        UNANSWERED: No answer within the attempt budget -- transport
            failures or 5xx. The paid tip is untouched and a later POST
            with the same tokens costs nothing further.
        REFUSED: The relay answered no (400/401/402). Resuming cannot
            help; a fixed paid tip cannot satisfy a 402.
    """

    __str__ = str.__str__

    UPLOADED = "uploaded"
    UNANSWERED = "unanswered"
    REFUSED = "refused"
