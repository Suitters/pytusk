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
        """Initializer.

        Args:
            pysui_config (PysuiConfiguration): Base pysui configuration object
            walrus_aggregator (str | None, optional): Identifies the walrus aggregator URL. Defaults to None.
            walrus_publisher (str | None, optional): Identifies the walrus publisher URL. Defaults to None.
        """
        self._pysui_config: PysuiConfiguration = pysui_config
        # Set the walrus configs publisher and aggregator
        self._aggregator: str = walrus_aggregator
        self._publisher: str = walrus_publisher

    @property
    def publisher_url(self) -> str:
        """Returns the publisher URL.

        Publishers `put` blobs on Walrus

        Returns:
            str: Publisher URL
        """
        return self._publisher

    @property
    def aggregator_url(self) -> str:
        """Returns the aggregator URL.

        Aggregators `get` blobs on Walrus

        Returns:
            str: Aggregator URL
        """
        return self._aggregator

    @property
    def pysui_config(self) -> PysuiConfiguration:
        """Get the underlyting pysui configuration

        Returns:
            PysuiConfiguration: Underlyting keys and provider URLs
        """
        return self._pysui_config
