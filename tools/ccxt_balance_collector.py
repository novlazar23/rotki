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


def read_json_file(path: Path | str) -> dict[str, Any]:
    json_path = Path(path)
    with json_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Config file {json_path} must contain a JSON object")
    return data


def env_value(name: str | None) -> str | None:
    if name is None:
        return None
    value = os.environ.get(name)
    if value == "":
        return None
    return value


def configured_env_name(exchange_config: Mapping[str, Any], key: str) -> str | None:
    value = exchange_config.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"Exchange config key {key!r} must be a non-empty string when set")
    return value.strip()


def missing_configured_env_vars(exchange_config: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for config_key in ("api_key_env", "secret_env", "password_env"):
        env_name = configured_env_name(exchange_config, config_key)
        if env_name is not None and env_value(env_name) is None:
            missing.append(env_name)
    return missing


def exchange_constructor_config(exchange_config: Mapping[str, Any]) -> dict[str, Any]:
    """Build the ccxt constructor config without ever storing secrets in files."""
    missing_env_vars = missing_configured_env_vars(exchange_config)
    if missing_env_vars:
        exchange_id = exchange_config.get("id", "<unknown>")
        raise ValueError(
            f"Missing environment variables for exchange {exchange_id!r}: "
            + ", ".join(missing_env_vars),
        )

    config = dict(exchange_config.get("constructor", {}))
    api_key_env = configured_env_name(exchange_config, "api_key_env")
    secret_env = configured_env_name(exchange_config, "secret_env")
    password_env = configured_env_name(exchange_config, "password_env")
    api_key = env_value(api_key_env)
    secret = env_value(secret_env)
    password = env_value(password_env)

    if api_key is not None:
        config["apiKey"] = api_key
    if secret is not None:
        config["secret"] = secret
    if password is not None:
        config["password"] = password
    config["enableRateLimit"] = True

    constructor_options = exchange_config.get("options")
    if constructor_options is not None:
        config["options"] = constructor_options

    urls = exchange_config.get("urls")
    if urls is not None:
        config["urls"] = urls

    return config


def create_exchange(exchange_config: Mapping[str, Any]) -> Any:
    if ccxt is None:
        raise RuntimeError("Missing optional dependency ccxt. Install it with: pip install ccxt")

    exchange_id = exchange_config.get("id")
    if not isinstance(exchange_id, str) or not exchange_id:
        raise ValueError("Each exchange config needs a non-empty string id, for example 'bybit'")
    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        raise ValueError(f"ccxt does not provide an exchange named {exchange_id!r}")

    exchange = exchange_class(exchange_constructor_config(exchange_config))
    sandbox = exchange_config.get("sandbox")
    if sandbox is not None:
        exchange.set_sandbox_mode(bool(sandbox))
    return exchange


def account_name(exchange_config: Mapping[str, Any]) -> str:
    name = exchange_config.get("name")
    if isinstance(name, str) and name:
        return name
    exchange_id = exchange_config.get("id")
    if isinstance(exchange_id, str) and exchange_id:
        return exchange_id
    return "exchange"


def should_keep_balance(total: Decimal, *, min_total: Decimal, include_zero: bool) -> bool:
    if total == 0 and not include_zero:
        return False
    return abs(total) >= min_total


def normalize_balance_rows(
        *,
        exchange_name: str,
        account: str,
        raw_balance: Mapping[str, Any],
        timestamp: str,
        min_total: Decimal,
        include_zero: bool,
) -> tuple[list[CollectedBalance], int, list[str]]:
    meta_keys = {"free", "used", "total", "info", "timestamp", "datetime"}
    balances: list[CollectedBalance] = []
    skipped_zero = 0
    unmapped_symbols: list[str] = []

    for symbol, value in raw_balance.items():
        if symbol in meta_keys or not isinstance(value, Mapping):
            continue

        free = decimal_from_value(value.get("free"))
        used = decimal_from_value(value.get("used"))
        total = decimal_from_value(value.get("total"))
        if total == 0 and (free != 0 or used != 0):
            total = free + used

        if not should_keep_balance(total, min_total=min_total, include_zero=include_zero):
            skipped_zero += 1
            continue

        asset_identifier = map_symbol(str(symbol))
        if asset_identifier is None:
            unmapped_symbols.append(str(symbol))

        balances.append(CollectedBalance(
            exchange=exchange_name,
            account=account,
            symbol=str(symbol),
            asset_identifier=asset_identifier,
            free=str(free),
            used=str(used),
            total=str(total),
            timestamp=timestamp,
        ))

    return balances, skipped_zero, sorted(set(unmapped_symbols))


def collect_balances(config: Mapping[str, Any]) -> CollectorResult:
    exchanges = config.get("exchanges")
    if not isinstance(exchanges, list) or len(exchanges) == 0:
        raise ValueError("Config needs a non-empty exchanges list")

    min_total = decimal_from_value(config.get("min_total", "0"))
    include_zero = bool(config.get("include_zero", False))
    timestamp = utc_now_iso()

    all_balances: list[CollectedBalance] = []
    total_skipped_zero = 0
    all_unmapped_symbols: list[str] = []

    for exchange_config in exchanges:
        if not isinstance(exchange_config, Mapping):
            raise ValueError("Each exchange config must be an object")
        exchange_name = account_name(exchange_config)
        exchange = create_exchange(exchange_config)
        raw_balance = exchange.fetch_balance()
        balances, skipped_zero, unmapped_symbols = normalize_balance_rows(
            exchange_name=exchange_name,
            account=account_name(exchange_config),
            raw_balance=raw_balance,
            timestamp=timestamp,
            min_total=min_total,
            include_zero=include_zero,
        )
        all_balances.extend(balances)
        total_skipped_zero += skipped_zero
        all_unmapped_symbols.extend(unmapped_symbols)

    return CollectorResult(
        balances=all_balances,
        skipped_zero_balances=total_skipped_zero,
        unmapped_symbols=sorted(set(all_unmapped_symbols)),
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
    text = json.dumps(data, indent=2, sort_keys=True)
    if output_path is None:
        print(text)
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect CCXT balances and map them to rotki identifiers.")
    parser.add_argument("--config", required=True, type=Path, help="Collector config JSON")
    parser.add_argument("--output", type=Path, help="Output JSON path. Defaults to stdout.")
    parser.add_argument("--fail-on-unmapped", action="store_true", help="Return non-zero if any balance symbol can not be mapped")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = read_json_file(args.config)
        result = collect_balances(config)
        data = result_to_jsonable(result)
        write_output(args.output, data)
    except Exception as e:  # noqa: BLE001 - CLI diagnostics
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.fail_on_unmapped and result.unmapped_symbols:
        print("ERROR: Unmapped symbols: " + ", ".join(result.unmapped_symbols), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
