#!/usr/bin/env python3
"""Collect CCXT trades and asset movements into an auditable JSON file.

This tool only reads from configured exchanges and writes JSON. It does not call
rotki and does not mutate any database.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from tools.ccxt_balance_collector import account_name, create_exchange, read_json_file, write_output
except ImportError:  # allows direct execution when copied next to the helper
    from ccxt_balance_collector import account_name, create_exchange, read_json_file, write_output  # type: ignore[no-redef]


@dataclass(frozen=True)
class CollectionError:
    exchange: str
    method: str
    message: str


@dataclass(frozen=True)
class HistoryResult:
    collected_at: str
    trades: list[dict[str, Any]]
    movements: list[dict[str, Any]]
    errors: list[CollectionError]


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def parse_timestamp(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value if value > 10_000_000_000 else value * 1000
    if isinstance(value, float):
        int_value = int(value)
        return int_value if int_value > 10_000_000_000 else int_value * 1000
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            int_value = int(stripped)
            return int_value if int_value > 10_000_000_000 else int_value * 1000
        dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    raise ValueError(f"Unsupported timestamp value {value!r}")


def method_supported(exchange: Any, method_name: str) -> bool:
    has = getattr(exchange, "has", {})
    return bool(has.get(method_name))


def safe_timestamp(entry: Mapping[str, Any]) -> int:
    value = entry.get("timestamp")
    return int(value) if isinstance(value, int | float) else 0


def entry_key(exchange_name: str, method: str, entry: Mapping[str, Any]) -> str:
    entry_id = entry.get("id")
    if entry_id not in (None, ""):
        return f"{exchange_name}:{method}:id:{entry_id}"
    return json.dumps({
        "exchange": exchange_name,
        "method": method,
        "timestamp": entry.get("timestamp"),
        "symbol": entry.get("symbol"),
        "currency": entry.get("currency"),
        "amount": entry.get("amount"),
        "type": entry.get("type"),
    }, sort_keys=True, default=str)


def annotate_entry(exchange_name: str, method: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    annotated = dict(entry)
    annotated["exchange"] = exchange_name
    annotated["source_method"] = method
    return annotated


def fetch_paginated(
        exchange: Any,
        method_name: str,
        *,
        since: int | None,
        until: int | None,
        limit: int,
        max_pages: int,
        params: dict[str, Any],
        symbol: str | None = None,
) -> list[dict[str, Any]]:
    method: Callable[..., Any] = getattr(exchange, method_name)
    collected: list[dict[str, Any]] = []
    next_since = since
    last_seen_timestamp = None

    for _ in range(max_pages):
        if symbol is None:
            rows = method(since=next_since, limit=limit, params=params)
        else:
            rows = method(symbol=symbol, since=next_since, limit=limit, params=params)
        if not rows:
            break

        page_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
        if until is not None:
            page_rows = [row for row in page_rows if safe_timestamp(row) <= until]
        collected.extend(page_rows)

        timestamps = [safe_timestamp(row) for row in page_rows if safe_timestamp(row) > 0]
        if not timestamps:
            break
        max_timestamp = max(timestamps)
        if last_seen_timestamp is not None and max_timestamp <= last_seen_timestamp:
            break
        last_seen_timestamp = max_timestamp
        next_since = max_timestamp + 1
        if until is not None and next_since > until:
            break
        if len(rows) < limit:
            break

    return collected


def collect_exchange_history(exchange_config: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[CollectionError]]:
    name = account_name(exchange_config)
    exchange = create_exchange(exchange_config)
    since = parse_timestamp(exchange_config.get("since"))
    until = parse_timestamp(exchange_config.get("until"))
    limit = int(exchange_config.get("limit", 200))
    max_pages = int(exchange_config.get("max_pages", 10))
    params = exchange_config.get("history_params", {})
    if not isinstance(params, dict):
        raise ValueError("history_params must be an object")

    symbols = exchange_config.get("symbols")
    if symbols is None:
        symbols_to_query: list[str | None] = [None]
    elif isinstance(symbols, list) and all(isinstance(symbol, str) for symbol in symbols):
        symbols_to_query = symbols
    else:
        raise ValueError("symbols must be a list of strings when set")

    include_ledger = bool(exchange_config.get("include_ledger", True))
    trades: list[dict[str, Any]] = []
    movements: list[dict[str, Any]] = []
    errors: list[CollectionError] = []
    seen: set[str] = set()

    for symbol in symbols_to_query:
        if not method_supported(exchange, "fetchMyTrades"):
            continue
        try:
            rows = fetch_paginated(
                exchange,
                "fetch_my_trades",
                since=since,
                until=until,
                limit=limit,
                max_pages=max_pages,
                params=params,
                symbol=symbol,
            )
            for row in rows:
                annotated = annotate_entry(name, "fetch_my_trades", row)
                key = entry_key(name, "trade", annotated)
                if key not in seen:
                    seen.add(key)
                    trades.append(annotated)
        except Exception as e:  # noqa: BLE001 - keep collecting other endpoints
            errors.append(CollectionError(exchange=name, method=f"fetch_my_trades:{symbol or '*'}", message=str(e)))

    for method_name in ("fetch_deposits", "fetch_withdrawals"):
        ccxt_has_name = "fetchDeposits" if method_name == "fetch_deposits" else "fetchWithdrawals"
        if not method_supported(exchange, ccxt_has_name):
            continue
        try:
            rows = fetch_paginated(
                exchange,
                method_name,
                since=since,
                until=until,
                limit=limit,
                max_pages=max_pages,
                params=params,
            )
            for row in rows:
                annotated = annotate_entry(name, method_name, row)
                key = entry_key(name, method_name, annotated)
                if key not in seen:
                    seen.add(key)
                    movements.append(annotated)
        except Exception as e:  # noqa: BLE001
            errors.append(CollectionError(exchange=name, method=method_name, message=str(e)))

    if include_ledger and method_supported(exchange, "fetchLedger"):
        try:
            rows = fetch_paginated(
                exchange,
                "fetch_ledger",
                since=since,
                until=until,
                limit=limit,
                max_pages=max_pages,
                params=params,
            )
            for row in rows:
                annotated = annotate_entry(name, "fetch_ledger", row)
                key = entry_key(name, "fetch_ledger", annotated)
                if key not in seen:
                    seen.add(key)
                    movements.append(annotated)
        except Exception as e:  # noqa: BLE001
            errors.append(CollectionError(exchange=name, method="fetch_ledger", message=str(e)))

    return trades, movements, errors


def collect_history(config: Mapping[str, Any]) -> dict[str, Any]:
    exchanges = config.get("exchanges")
    if not isinstance(exchanges, list) or len(exchanges) == 0:
        raise ValueError("Config needs a non-empty exchanges list")

    all_trades: list[dict[str, Any]] = []
    all_movements: list[dict[str, Any]] = []
    all_errors: list[CollectionError] = []

    for exchange_config in exchanges:
        if not isinstance(exchange_config, Mapping):
            raise ValueError("Each exchange config must be an object")
        trades, movements, errors = collect_exchange_history(exchange_config)
        all_trades.extend(trades)
        all_movements.extend(movements)
        all_errors.extend(errors)

    result = HistoryResult(
        collected_at=utc_now_iso(),
        trades=sorted(all_trades, key=safe_timestamp),
        movements=sorted(all_movements, key=safe_timestamp),
        errors=all_errors,
    )
    return {
        "collected_at": result.collected_at,
        "trades": result.trades,
        "movements": result.movements,
        "errors": [asdict(error) for error in result.errors],
        "summary": {
            "trades": len(result.trades),
            "movements": len(result.movements),
            "errors": len(result.errors),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect CCXT trades and movements into JSON.")
    parser.add_argument("--config", required=True, type=Path, help="Collector config JSON")
    parser.add_argument("--output", type=Path, help="Output JSON path. Defaults to stdout.")
    parser.add_argument("--fail-on-errors", action="store_true", help="Return non-zero if any endpoint failed")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = read_json_file(args.config)
        result = collect_history(config)
        write_output(args.output, result)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.fail_on_errors and result["summary"]["errors"]:
        print("ERROR: Some CCXT history endpoints failed. Check the errors array.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
