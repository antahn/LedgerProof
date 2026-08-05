"""Phase 0 sanity: the package imports and pytest runs."""

import ledgerproof


def test_package_imports() -> None:
    assert ledgerproof is not None
