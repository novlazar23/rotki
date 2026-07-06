#!/usr/bin/env python3
"""Map CCXT balance symbols to rotki asset identifiers.

This helper is intentionally read-only. It prints the rotki identifiers to use
when importing manual balances from CCXT/exported exchange data.
"""

from __future__ import annotations

import argparse
import json
from typing import Final

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


def map_symbol(symbol: str) -> str | None:
    """Return the rotki asset identifier for a CCXT/exchange symbol."""
    return CCXT_TO_ROTKI_ASSET_IDS.get(symbol.upper())


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
        symbol = raw_symbol.upper()
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
