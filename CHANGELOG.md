# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - Unpublished

### Added

### Fixed

- Documentation generation fix

### Changed

- pyproject.toml licensing fix.

### Removed

## [0.2.0] - 2026-08-10

### Added

- Split `WalrusNetworkConfig.walrus_url` into `walrus_aggregator_url` and `walrus_publisher_url`, with formal `set_walrus_aggregator_url()`/`set_walrus_publisher_url()` setters on `PytuskConfiguration` for updating either field on any network, including reserved `testnet`/`mainnet`.
- `tusky` CLI for fundemental walrus blob information and changes (CRUD)
- Added documentation

### Fixed

### Changed

### Removed
