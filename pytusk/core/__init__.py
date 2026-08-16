#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Core Walrus protocol modules.

Domain logic that does not depend on a live connection: RedStuff encoding
and blob-ID derivation (``encoding``), BLS confirmation and certificate
handling (``certification``), the committee and epoch model (``committee``),
Move-call composition against the Walrus contracts (``system_ops``), and
native upload orchestration (``native_upload``).

The Walrus HTTP client itself lives in :mod:`pytusk.client`.
"""
