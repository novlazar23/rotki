#!/usr/bin/env python3
"""Map CCXT balance symbols to rotki asset identifiers.

The module is importable by external balance importers and also usable as a
small CLI diagnostic tool.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

CCXT_TO_ROTKI_ASSET_IDS: Final[dict[str, str]] = {
    "CHIP": "CHIP",
    "CSPR": "CSPR",
    "GRAM": "GRAM",
    "HYPE": "HYPE",
    "MMT": "MMT",
    "POL": "eip155:137/erc20:0x0000000000000000000000000000000000001010",
    "RESOLV": "RESOLV",
    "ROOT": "ROOT",
}


@dataclass(frozen=True)
class BalanceMappingResult:
    """Result of mapping a batch of external balance entries."""

    mapped: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


class MissingAssetMappingError(ValueError):
    """Raised when a balance entry contains an unmapped exchange symbol."""


class MissingBalanceSymbolError(ValueError):
    """Raised when a balance entry does not contain the expected symbol field."""


def normalize_symbol(symbol: str) -> str:
    """Normalize an exchange/CCXT symbol before lookup."""
    return symbol.strip().upper()


def map_symbol(symbol: str) -> str | None:
    """Return the rotki asset identifier for a CCXT/exchange symbol."""
    return CCXT_TO_ROTKI_ASSET_IDS.get(normalize_symbol(symbol))


def require_symbol_mapping(symbol: str) -> str:
    """Return a rotki identifier or raise a precise error for strict importers."""
    normalized_symbol = normalize_symbol(symbol)
    identifier = map_symbol(normalized_symbol)
    if identifier is None:
        raise MissingAssetMappingError(
            f"No rotki asset identifier mapping configured for exchange symbol {normalized_symbol!r}",
        )
    return identifier


def add_rotki_identifier_to_balance(
        balance: Mapping[str, Any],
        *,
        symbol_key: str = "asset",
        identifier_key: str = "asset_identifier",
        strict: bool = False,
) -> dict[str, Any] | None:
    """Return a balance copy with a rotki identifier added.

    Args:
        balance: External balance row/dict from CCXT or a local importer.
        symbol_key: Key containing the exchange symbol. Typical values are
            ``asset``, ``symbol`` or ``currency``.
        identifier_key: Key to write the rotki identifier to.
        strict: If true, raise on missing symbols/mappings. If false, return
            ``None`` so the caller can skip and log the row.
    """
    symbol = balance.get(symbol_key)
    if not isinstance(symbol, str) or symbol.strip() == "":
        if strict:
            raise MissingBalanceSymbolError(
                f"Balance entry has no usable {symbol_key!r} field: {balance!r}",
            )
        return None

    identifier = map_symbol(symbol)
    if identifier is None:
        if strict:
            require_symbol_mapping(symbol)
        return None

    mapped_balance = dict(balance)
    mapped_balance[identifier_key] = identifier
    return mapped_balance


def add_rotki_identifiers_to_balances(
        balances: Iterable[Mapping[str, Any]],
        *,
        symbol_key: str = "asset",
        identifier_key: str = "asset_identifier",
        strict: bool = False,
) -> BalanceMappingResult:
    """Map a sequence of balance rows and split mapped and skipped entries."""
    mapped: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for balance in balances:
        mapped_balance = add_rotki_identifier_to_balance(
            balance,
            symbol_key=symbol_key,
            identifier_key=identifier_key,
            strict=strict,
        )
        if mapped_balance is None:
            skipped.append(dict(balance))
        else:
            mapped.append(mapped_balance)

    return BalanceMappingResult(mapped=mapped, skipped=skipped)


def validate_identifier(identifier: str) -> bool:
    """Best-effort validation when executed inside a configured rotki environment."""
    try:
        from rotkehlchen.assets.asset import Asset  # pylint: disable=import-outside-toplevel
    except Exception:  # noqa: BLE001 - optional validation helper
        return True

    try:
        Asset(identifier).check_existence()
    except Exception:  # noqa: BLE001 - keep this CLI diagnostic-focused
        return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Map CCXT symbols to rotki asset identifiers.")
    parser.add_argument("symbols", nargs="*", default=sorted(CCXT_TO_ROTKI_ASSET_IDS))
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate identifiers through rotki Asset.check_existence() when possible.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result: dict[str, dict[str, str | bool | None]] = {}
    exit_code = 0

    for raw_symbol in args.symbols:
        symbol = normalize_symbol(raw_symbol)
        identifier = map_symbol(symbol)
        valid = None if identifier is None or not args.validate else validate_identifier(identifier)
        if identifier is None or valid is False:
            exit_code = 1
        result[symbol] = {"identifier": identifier, "valid": valid}

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for symbol, data in result.items():
            identifier = data["identifier"] or "<missing mapping>"
            suffix = ""
            if data["valid"] is False:
                suffix = "  # missing in this rotki global DB"
            print(f"{symbol:<8} -> {identifier}{suffix}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
