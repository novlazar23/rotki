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
    "ALGO": "ALGO",
    "ARB": "eip155:42161/erc20:0x912CE59144191C1204E64559FE8253a0e49E6548",
    "CHIP": "CHIP",
    "CSPR": "CSPR",
    "DOGE": "DOGE",
    "GALA": "eip155:1/erc20:0xd1d2Eb1B1e90B638588728b4130137D262C87cae",
    "GRAM": "GRAM",
    "HBAR": "HBAR",
    "HYPE": "HYPE",
    "KAS": "KAS",
    "LINK": "eip155:1/erc20:0x514910771AF9Ca656af840dff83E8264EcF986CA",
    "MMT": "MMT",
    "NEAR": "NEAR",
    "POL": "eip155:137/erc20:0x0000000000000000000000000000000000001010",
    "RESOLV": "RESOLV",
    "ROOT": "ROOT",
    "SHIB": "eip155:1/erc20:0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE",
    "SOL": "SOL",
    "USDT": "eip155:1/erc20:0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "XRP": "XRP",
}


@dataclass(frozen=True)
class SymbolResolution:
    """Resolved rotki identifier plus the source of that resolution."""

    identifier: str | None
    source: str | None


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


def normalize_extra_mappings(extra_mappings: Mapping[str, str] | None) -> dict[str, str]:
    """Normalize user supplied symbol mappings from config files."""
    if extra_mappings is None:
        return {}
    return {
        normalize_symbol(symbol): identifier.strip()
        for symbol, identifier in extra_mappings.items()
        if isinstance(symbol, str) and isinstance(identifier, str) and identifier.strip() != ""
    }


def map_symbol(symbol: str) -> str | None:
    """Return the built-in rotki asset identifier for a CCXT/exchange symbol."""
    return CCXT_TO_ROTKI_ASSET_IDS.get(normalize_symbol(symbol))


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


def resolve_symbol(
        symbol: str,
        *,
        extra_mappings: Mapping[str, str] | None = None,
        allow_symbol_fallback: bool = False,
        validate_symbol_fallback: bool = False,
) -> SymbolResolution:
    """Resolve an exchange symbol to a rotki asset identifier.

    Resolution order:
    1. user supplied extra mappings, e.g. from the collector config
    2. built-in mappings in ``CCXT_TO_ROTKI_ASSET_IDS``
    3. optional symbol identity fallback, e.g. ``ALGO`` -> ``ALGO``

    The fallback is intentionally optional because some exchange symbols are
    ambiguous or chain-specific. Use explicit mappings for token contracts.
    """
    normalized_symbol = normalize_symbol(symbol)
    normalized_extra_mappings = normalize_extra_mappings(extra_mappings)

    if normalized_symbol in normalized_extra_mappings:
        return SymbolResolution(
            identifier=normalized_extra_mappings[normalized_symbol],
            source="config",
        )

    identifier = map_symbol(normalized_symbol)
    if identifier is not None:
        return SymbolResolution(identifier=identifier, source="builtin")

    if allow_symbol_fallback:
        if validate_symbol_fallback and not validate_identifier(normalized_symbol):
            return SymbolResolution(identifier=None, source=None)
        return SymbolResolution(identifier=normalized_symbol, source="symbol_fallback")

    return SymbolResolution(identifier=None, source=None)


def require_symbol_mapping(
        symbol: str,
        *,
        extra_mappings: Mapping[str, str] | None = None,
        allow_symbol_fallback: bool = False,
        validate_symbol_fallback: bool = False,
) -> str:
    """Return a rotki identifier or raise a precise error for strict importers."""
    normalized_symbol = normalize_symbol(symbol)
    resolution = resolve_symbol(
        normalized_symbol,
        extra_mappings=extra_mappings,
        allow_symbol_fallback=allow_symbol_fallback,
        validate_symbol_fallback=validate_symbol_fallback,
    )
    if resolution.identifier is None:
        raise MissingAssetMappingError(
            f"No rotki asset identifier mapping configured for exchange symbol {normalized_symbol!r}",
        )
    return resolution.identifier


def add_rotki_identifier_to_balance(
        balance: Mapping[str, Any],
        *,
        symbol_key: str = "asset",
        identifier_key: str = "asset_identifier",
        mapping_source_key: str | None = None,
        strict: bool = False,
        extra_mappings: Mapping[str, str] | None = None,
        allow_symbol_fallback: bool = False,
        validate_symbol_fallback: bool = False,
) -> dict[str, Any] | None:
    """Return a balance copy with a rotki identifier added.

    Args:
        balance: External balance row/dict from CCXT or a local importer.
        symbol_key: Key containing the exchange symbol. Typical values are
            ``asset``, ``symbol`` or ``currency``.
        identifier_key: Key to write the rotki identifier to.
        mapping_source_key: Optional key for writing the mapping source.
        strict: If true, raise on missing symbols/mappings. If false, return
            ``None`` so the caller can skip and log the row.
        extra_mappings: User supplied symbol mappings, e.g. from config.
        allow_symbol_fallback: Map unknown symbols to themselves.
        validate_symbol_fallback: Validate symbol fallback via rotki if possible.
    """
    symbol = balance.get(symbol_key)
    if not isinstance(symbol, str) or symbol.strip() == "":
        if strict:
            raise MissingBalanceSymbolError(
                f"Balance entry has no usable {symbol_key!r} field: {balance!r}",
            )
        return None

    resolution = resolve_symbol(
        symbol,
        extra_mappings=extra_mappings,
        allow_symbol_fallback=allow_symbol_fallback,
        validate_symbol_fallback=validate_symbol_fallback,
    )
    if resolution.identifier is None:
        if strict:
            require_symbol_mapping(
                symbol,
                extra_mappings=extra_mappings,
                allow_symbol_fallback=allow_symbol_fallback,
                validate_symbol_fallback=validate_symbol_fallback,
            )
        return None

    mapped_balance = dict(balance)
    mapped_balance[identifier_key] = resolution.identifier
    if mapping_source_key is not None:
        mapped_balance[mapping_source_key] = resolution.source
    return mapped_balance


def add_rotki_identifiers_to_balances(
        balances: Iterable[Mapping[str, Any]],
        *,
        symbol_key: str = "asset",
        identifier_key: str = "asset_identifier",
        mapping_source_key: str | None = None,
        strict: bool = False,
        extra_mappings: Mapping[str, str] | None = None,
        allow_symbol_fallback: bool = False,
        validate_symbol_fallback: bool = False,
) -> BalanceMappingResult:
    """Map a sequence of balance rows and split mapped and skipped entries."""
    mapped: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for balance in balances:
        mapped_balance = add_rotki_identifier_to_balance(
            balance,
            symbol_key=symbol_key,
            identifier_key=identifier_key,
            mapping_source_key=mapping_source_key,
            strict=strict,
            extra_mappings=extra_mappings,
            allow_symbol_fallback=allow_symbol_fallback,
            validate_symbol_fallback=validate_symbol_fallback,
        )
        if mapped_balance is None:
            skipped.append(dict(balance))
        else:
            mapped.append(mapped_balance)

    return BalanceMappingResult(mapped=mapped, skipped=skipped)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Map CCXT symbols to rotki asset identifiers.")
    parser.add_argument("symbols", nargs="*", default=sorted(CCXT_TO_ROTKI_ASSET_IDS))
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate identifiers through rotki Asset.check_existence() when possible.",
    )
    parser.add_argument(
        "--allow-symbol-fallback",
        action="store_true",
        help="Map unknown symbols to themselves, e.g. ALGO -> ALGO.",
    )
    parser.add_argument(
        "--validate-symbol-fallback",
        action="store_true",
        help="Validate symbol fallback through rotki when possible.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result: dict[str, dict[str, str | bool | None]] = {}
    exit_code = 0

    for raw_symbol in args.symbols:
        symbol = normalize_symbol(raw_symbol)
        resolution = resolve_symbol(
            symbol,
            allow_symbol_fallback=args.allow_symbol_fallback,
            validate_symbol_fallback=args.validate_symbol_fallback,
        )
        valid = None if resolution.identifier is None or not args.validate else validate_identifier(resolution.identifier)
        if resolution.identifier is None or valid is False:
            exit_code = 1
        result[symbol] = {
            "identifier": resolution.identifier,
            "source": resolution.source,
            "valid": valid,
        }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for symbol, data in result.items():
            identifier = data["identifier"] or "<missing mapping>"
            source = data["source"] or "missing"
            suffix = f"  # source={source}"
            if data["valid"] is False:
                suffix += " missing in this rotki global DB"
            print(f"{symbol:<8} -> {identifier}{suffix}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
