#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Core Walrus protocol modules.

Domain logic for the Walrus protocol: RedStuff encoding and blob-ID
derivation (``encoding``), BLS confirmation and certificate handling
(``certification``), the committee and epoch model (``committee``),
Move-call composition and PTB submission against the Walrus contracts
(``ops``), and native upload orchestration (``native_upload``).
``encoding``, ``certification``, and ``committee`` are pure/local -- no
network calls. ``ops`` and ``native_upload`` DO make live calls
through a :class:`~pytusk.client.walrus_client.WalrusClient`, in addition
to composing pure PTBs (see each module's own docstring for the
pure-vs-live split within it).

The Walrus HTTP client itself lives in :mod:`pytusk.client`.
"""
