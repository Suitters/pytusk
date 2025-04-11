#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Pysui Configuration Wrapper."""

from pysui import PysuiConfiguration


class PytuskConfiguration:
    """Wrapper for PysuiConfiguration."""

    def __init__(
        self,
        *,
        pysui_config: PysuiConfiguration,
        walrus_aggregator: str | None = None,
        walrus_publisher: str | None = None,
    ):
        """Initialize core walrus properties."""
        self._pysui_config: PysuiConfiguration = pysui_config
        # Set the walrus configs publisher and aggregator
        self._aggregator: str = walrus_aggregator
        self._publisher: str = walrus_publisher

    @property
    def publisher_url(self) -> str:
        """."""
        return self._publisher

    @property
    def aggregator_url(self) -> str:
        """."""
        return self._aggregator

    @property
    def pysui_config(self) -> PysuiConfiguration:
        """Fetch pysui config."""
        return self._pysui_config
