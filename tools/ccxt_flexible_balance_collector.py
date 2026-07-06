#!/usr/bin/env python3
"""Flexible CCXT balance collector with runtime asset mapping fallback.

This collector reuses the connection helpers from ``ccxt_balance_collector`` and
adds flexible asset resolution on top:

1. local config mappings from ``asset_mappings``
2. built-in mappings from ``ccxt_rotki_symbol_map``
3. optional symbol fallback, e.g. ``FOO`` -> ``FOO``

Use explicit config mappings for chain-specific tokens. Symbol fallback is useful
for native assets that rotki stores directly by symbol.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from tools.ccxt_balance_collector import (
        account_name,
        bool_config,
        create_exchange,
        decimal_from_value,
        read_json_file,
        should_keep_balance,
        utc_now_iso,
        write_output,
    )
    from tools.ccxt_rotki_symbol_map import normalize_extra_mappings, resolve_symbol
except ImportError:  # allows direct execution when copied next to the helpers
    from ccxt_balance_collector import (  # type: ignore[no-redef]
        account_name,
        bool_config,
        create_exchange,
        decimal_from_value,
        read_json_file,
        should_keep_balance,
        utc_now_iso,
        write_output,
    )
    from ccxt_rotki_symbol_map import normalize_extra_mappings, resolve_symbol  # type: ignore[no-redef]


@dataclass(frozen=True)
class FlexibleCollectedBalance:
    exchange: str
    account: str
    symbol: str
    asset_identifier: str | None
    mapping_source: str | None
    free: str
    used: str
    total: str
    timestamp: str


@dataclass(frozen=True)
class FlexibleCollectorResult:
    balances: list[FlexibleCollectedBalance]
    skipped_zero_balances: int
    unmapped_symbols: list[str]
    auto_mapped_symbols: list[str]


def config_asset_mappings(config: Mapping[str, Any]) -> dict[str, str]:
    mappings = config.get("asset_mappings")
    if mappings is None:
        return {}
    if not isinstance(mappings, Mapping):
        raise ValueError("Config key 'asset_mappings' must be an object")
    return normalize_extra_mappings(mappings)


def normalize_balance_rows(
        exchange_name: str,
        raw_balance: Mapping[str, Any],
        *,
        min_total: Any,
        include_zero: bool,
        timestamp: str,
        asset_mappings: Mapping[str, str],
        allow_symbol_fallback: bool,
        validate_symbol_fallback: bool,
) -> tuple[list[FlexibleCollectedBalance], int, set[str], set[str]]:
    rows: list[FlexibleCollectedBalance] = []
    skipped_zero = 0
    unmapped_symbols: set[str] = set()
    auto_mapped_symbols: set[str] = set()

    for symbol, value in raw_balance.items():
        if symbol in {"free", "used", "total", "info", "timestamp", "datetime"}:
            continue
        if not isinstance(value, Mapping):
            continue

        free = decimal_from_value(value.get("free"))
        used = decimal_from_value(value.get("used"))
        total = decimal_from_value(value.get("total"))
        if total == 0 and (free != 0 or used != 0):
            total = free + used

        if not should_keep_balance(total=total, min_total=min_total, include_zero=include_zero):
            skipped_zero += 1
            continue

        normalized_symbol = symbol.upper()
        resolution = resolve_symbol(
            normalized_symbol,
            extra_mappings=asset_mappings,
            allow_symbol_fallback=allow_symbol_fallback,
            validate_symbol_fallback=validate_symbol_fallback,
        )
        if resolution.identifier is None:
            unmapped_symbols.add(normalized_symbol)
        elif resolution.source == "symbol_fallback":
            auto_mapped_symbols.add(normalized_symbol)

        rows.append(FlexibleCollectedBalance(
            exchange=exchange_name,
            account=exchange_name,
            symbol=normalized_symbol,
            asset_identifier=resolution.identifier,
            mapping_source=resolution.source,
            free=str(free),
            used=str(used),
            total=str(total),
            timestamp=timestamp,
        ))

    return rows, skipped_zero, unmapped_symbols, auto_mapped_symbols


def collect_balances(config: Mapping[str, Any]) -> FlexibleCollectorResult:
    exchanges = config.get("exchanges")
    if not isinstance(exchanges, list) or len(exchanges) == 0:
        raise ValueError("Config needs a non-empty 'exchanges' list")

    min_total = decimal_from_value(config.get("min_total", "0"))
    include_zero = bool_config(config, "include_zero", False)
    allow_symbol_fallback = bool_config(config, "allow_symbol_fallback", False)
    validate_symbol_fallback = bool_config(config, "validate_symbol_fallback", False)
    asset_mappings = config_asset_mappings(config)
    timestamp = utc_now_iso()

    collected: list[FlexibleCollectedBalance] = []
    skipped_zero_balances = 0
    unmapped_symbols: set[str] = set()
    auto_mapped_symbols: set[str] = set()

    for exchange_config in exchanges:
        if not isinstance(exchange_config, Mapping):
            raise ValueError("Each entry in 'exchanges' must be an object")

        name = account_name(exchange_config)
        exchange = create_exchange(exchange_config)
        raw_balance = exchange.fetch_balance()
        rows, skipped_zero, exchange_unmapped, exchange_auto_mapped = normalize_balance_rows(
            exchange_name=name,
            raw_balance=raw_balance,
            min_total=min_total,
            include_zero=include_zero,
            timestamp=timestamp,
            asset_mappings=asset_mappings,
            allow_symbol_fallback=allow_symbol_fallback,
            validate_symbol_fallback=validate_symbol_fallback,
        )
        collected.extend(rows)
        skipped_zero_balances += skipped_zero
        unmapped_symbols.update(exchange_unmapped)
        auto_mapped_symbols.update(exchange_auto_mapped)

    return FlexibleCollectorResult(
        balances=collected,
        skipped_zero_balances=skipped_zero_balances,
        unmapped_symbols=sorted(unmapped_symbols),
        auto_mapped_symbols=sorted(auto_mapped_symbols),
    )


def result_to_jsonable(result: FlexibleCollectorResult) -> dict[str, Any]:
    return {
        "balances": [asdict(balance) for balance in result.balances],
        "summary": {
            "balances": len(result.balances),
            "skipped_zero_balances": result.skipped_zero_balances,
            "unmapped_symbols": result.unmapped_symbols,
            "auto_mapped_symbols": result.auto_mapped_symbols,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect CCXT balances with flexible rotki mapping.")
    parser.add_argument("--config", required=True, type=Path, help="Path to collector config JSON")
    parser.add_argument("--output", type=Path, help="Optional output JSON path. Defaults to stdout.")
    parser.add_argument(
        "--fail-on-unmapped",
        action="store_true",
        help="Return a non-zero exit code if any collected balance has no rotki asset mapping.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = read_json_file(args.config)
        result = collect_balances(config)
        write_output(args.output, result_to_jsonable(result))
    except Exception as e:  # noqa: BLE001 - CLI must print concise diagnostics
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.fail_on_unmapped and result.unmapped_symbols:
        print(
            "ERROR: Unmapped symbols: " + ", ".join(result.unmapped_symbols),
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
