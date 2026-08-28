#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""End-to-end write/read pipelines composed from the layers beneath them.

This package is the TOP composition layer of :mod:`pytusk.core`. It may
import from :mod:`pytusk.core.ops`, :mod:`pytusk.core.native_upload`,
:mod:`pytusk.core.relay_upload`, :mod:`pytusk.core.types`,
:mod:`pytusk.core.chain`, :mod:`pytusk.core.encoding` and
:mod:`pytusk.client`; nothing beneath it may import from here. That one-way
rule is what keeps these pipelines free of the import cycle that would
otherwise form -- :mod:`pytusk.core.native_upload` imports
:mod:`pytusk.core.ops.blob_execute`, so a pipeline or delivery abstraction
placed in :mod:`pytusk.core.ops` would close the loop.
"""

from pytusk.core.pipelines.delivery import (
    BlobDelivery,
    DeliveryOutcome,
    DeliveryResult,
    NativeDelivery,
    NativeDeliveryResult,
    RelayDelivery,
    RelayDeliveryResult,
)
from pytusk.core.pipelines.registration import (
    BlobRegistration,
    PlainBlobRegistration,
    TippedBlobRegistration,
)
from pytusk.core.pipelines.write import store_blob_native, store_blob_relay

__all__ = [
    "BlobDelivery",
    "BlobRegistration",
    "DeliveryOutcome",
    "DeliveryResult",
    "NativeDelivery",
    "NativeDeliveryResult",
    "PlainBlobRegistration",
    "RelayDelivery",
    "RelayDeliveryResult",
    "TippedBlobRegistration",
    "store_blob_native",
    "store_blob_relay",
]
