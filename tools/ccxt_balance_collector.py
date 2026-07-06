#!/usr/bin/env python3
"""Collect current CCXT exchange balances and map them to rotki asset identifiers.

The collector intentionally writes JSON only. It does not store API keys, does
not mutate rotki databases and does not call rotki APIs directly. A separate
importer can consume the JSON output and create rotki manual balances.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    import ccxt  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - depends on optional local dependency
    ccxt = None  # type: ignore[assignment]

try:
    from tools.ccxt_rotki_symbol_map import map_symbol
except ImportError:  # allows direct execution when the file is copied elsewhere
    from ccxt_rotki_symbol_map import map_symbol  # type: ignore[no-redef]


@dataclass(frozen=True)
class CollectedBalance:
    exchange: str
    account: str
    symbol: str
    asset_identifier: str | None
    free: str
    used: str
    total: str
    timestamp: str


@dataclass(frozen=True)
class CollectorResult:
    balances: list[CollectedBalance]
    skipped_zero_balances: int
    unmapped_symbols: list[str]


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def decimal_from_value(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"Can not convert balance value {value!r} to Decimal") from e


def read_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a JSON object")
    return data


def env_value(name: str | None) -> str | None:
    if name is None:
        return None
    value = os.environ.get(name)
    if value == "":
        return None
    return value


def exchange_constructor_config(exchange_config: Mapping[str, Any]) -> dict[str, Any]:
    """Build the ccxt constructor config without ever storing secrets in files."""
    config = dict(exchange_config.get("constructor", {}))
    api_key = env_value(exchange_config.get("api_key_env"))
    secret = env_value(exchange_config.get("secret_env"))
    password = env_value(exchange_config.get("password_env"))

    if api_key is not None:
        config["apiKey"] = api_key
    if secret is not None:
        config["secret"] = secret
    if password is not None:
        config["password"] = password

    options = exchange_config.get("options")
    if isinstance(options, dict):
        config.setdefault("options", {}).update(options)

    urls = exchange_config.get("urls")
    if isinstance(urls, dict):
        config["urls"] = urls

    config.setdefault("enableRateLimit", True)
    return config


def create_exchange(exchange_config: Mapping[str, Any]) -> Any:
    if ccxt is None:
        raise RuntimeError("Missing optional dependency ccxt. Install it with: pip install ccxt")

    exchange_id = exchange_config.get("id")
    if not isinstance(exchange_id, str) or exchange_id == "":
        raise ValueError("Each exchange config needs a non-empty 'id', e.g. 'bybit'")

    try:
        exchange_class = getattr(ccxt, exchange_id)
    except AttributeError as e:
        raise ValueError(f"ccxt has no exchange class {exchange_id!r}") from e

    exchange = exchange_class(exchange_constructor_config(exchange_config))
    if exchange_config.get("sandbox") is True:
        exchange.set_sandbox_mode(True)
    return exchange


def account_name(exchange_config: Mapping[str, Any]) -> str:
    configured_name = exchange_config.get("name")
    exchange_id = exchange_config.get("id")
    return str(configured_name or exchange_id)


def should_keep_balance(total: Decimal, min_total: Decimal, include_zero: bool) -> bool:
    if include_zero:
        return True
    return total.copy_abs() > min_total


def normalize_balance_rows(
        exchange_name: str,
        raw_balance: Mapping[str, Any],
        *,
        min_total: Decimal,
        include_zero: bool,
        timestamp: str,
) -> tuple[list[CollectedBalance], int, set[str]]:
    """Normalize ccxt fetch_balance() output to flat rows."""
    rows: list[CollectedBalance] = []
    skipped_zero = 0
    unmapped_symbols: set[str] = set()

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

        asset_identifier = map_symbol(symbol)
        if asset_identifier is None:
            unmapped_symbols.add(symbol.upper())

        rows.append(CollectedBalance(
            exchange=exchange_name,
            account=exchange_name,
            symbol=symbol.upper(),
            asset_identifier=asset_identifier,
            free=str(free),
            used=str(used),
            total=str(total),
            timestamp=timestamp,
        ))

    return rows, skipped_zero, unmapped_symbols


def collect_balances(config: Mapping[str, Any]) -> CollectorResult:
    exchanges = config.get("exchanges")
    if not isinstance(exchanges, list) or len(exchanges) == 0:
        raise ValueError("Config needs a non-empty 'exchanges' list")

    min_total = decimal_from_value(config.get("min_total", "0"))
    include_zero = bool(config.get("include_zero", False))
    timestamp = utc_now_iso()
    collected: list[CollectedBalance] = []
    skipped_zero_balances = 0
    unmapped_symbols: set[str] = set()

    for exchange_config in exchanges:
        if not isinstance(exchange_config, Mapping):
            raise ValueError("Each entry in 'exchanges' must be an object")

        name = account_name(exchange_config)
        exchange = create_exchange(exchange_config)
        raw_balance = exchange.fetch_balance()
        rows, skipped_zero, exchange_unmapped = normalize_balance_rows(
            exchange_name=name,
            raw_balance=raw_balance,
            min_total=min_total,
            include_zero=include_zero,
            timestamp=timestamp,
        )
        collected.extend(rows)
        skipped_zero_balances += skipped_zero
        unmapped_symbols.update(exchange_unmapped)

    return CollectorResult(
        balances=collected,
        skipped_zero_balances=skipped_zero_balances,
        unmapped_symbols=sorted(unmapped_symbols),
    )


def result_to_jsonable(result: CollectorResult) -> dict[str, Any]:
    return {
        "balances": [asdict(balance) for balance in result.balances],
        "summary": {
            "balances": len(result.balances),
            "skipped_zero_balances": result.skipped_zero_balances,
            "unmapped_symbols": result.unmapped_symbols,
        },
    }


def write_output(output_path: Path | None, data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True)
    if output_path is None:
        print(payload)
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(payload + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect CCXT balances and map them to rotki assets.")
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
