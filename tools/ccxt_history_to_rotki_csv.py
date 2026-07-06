#!/usr/bin/env python3
"""Convert collected CCXT history JSON into rotki generic import CSV files."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    from tools.ccxt_rotki_symbol_map import resolve_symbol
except ImportError:
    from ccxt_rotki_symbol_map import resolve_symbol  # type: ignore[no-redef]

TRADE_FIELDS = [
    "Location",
    "Timestamp",
    "Spend Amount",
    "Spend Currency",
    "Receive Amount",
    "Receive Currency",
    "Fee",
    "Fee Currency",
    "Description",
]

EVENT_FIELDS = [
    "Location",
    "Timestamp",
    "Type",
    "Amount",
    "Currency",
    "Fee",
    "Fee Currency",
    "Description",
]


class ConversionSkip(Exception):
    pass


def read_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Input file {path} must contain a JSON object")
    return data


def decimal_from_value(value: Any, *, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise ConversionSkip(f"Can not convert {field_name}={value!r} to Decimal") from e


def timestamp_to_seconds(value: Any) -> str:
    if value in (None, ""):
        raise ConversionSkip("Missing timestamp")
    timestamp = int(value)
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return str(timestamp)


def iso_from_timestamp(value: Any) -> str:
    timestamp = int(timestamp_to_seconds(value))
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def split_symbol(symbol: str) -> tuple[str, str]:
    pair = symbol.split(":", maxsplit=1)[0]
    if "/" not in pair:
        raise ConversionSkip(f"Can not split trade symbol {symbol!r}")
    base, quote = pair.split("/", maxsplit=1)
    return base.strip(), quote.strip()


def resolve_asset(symbol: Any, *, allow_symbol_fallback: bool, skip_assets: set[str]) -> str:
    if not isinstance(symbol, str) or symbol.strip() == "":
        raise ConversionSkip("Missing asset symbol")
    normalized = symbol.strip().upper()
    if normalized in skip_assets:
        raise ConversionSkip(f"Asset {normalized} is configured to be skipped")

    resolution = resolve_symbol(normalized, allow_symbol_fallback=allow_symbol_fallback)
    identifier = resolution.identifier or normalized
    if identifier in skip_assets or normalized in skip_assets:
        raise ConversionSkip(f"Asset {normalized} is configured to be skipped")
    return identifier


def trade_fee(trade: Mapping[str, Any], *, allow_symbol_fallback: bool, skip_assets: set[str]) -> tuple[str, str]:
    fee = trade.get("fee")
    if not isinstance(fee, Mapping):
        return "", ""
    cost = fee.get("cost")
    currency = fee.get("currency")
    if cost in (None, "", 0, "0") or currency in (None, ""):
        return "", ""
    return str(decimal_from_value(cost, field_name="fee.cost")), resolve_asset(
        currency,
        allow_symbol_fallback=allow_symbol_fallback,
        skip_assets=skip_assets,
    )


def convert_trade(trade: Mapping[str, Any], *, allow_symbol_fallback: bool, skip_assets: set[str]) -> dict[str, str]:
    symbol = trade.get("symbol")
    if not isinstance(symbol, str):
        raise ConversionSkip("Trade has no symbol")
    base, quote = split_symbol(symbol)
    side = str(trade.get("side", "")).lower()
    amount = decimal_from_value(trade.get("amount"), field_name="amount")
    cost_value = trade.get("cost")
    if cost_value in (None, ""):
        price = decimal_from_value(trade.get("price"), field_name="price")
        cost = amount * price
    else:
        cost = decimal_from_value(cost_value, field_name="cost")

    base_asset = resolve_asset(base, allow_symbol_fallback=allow_symbol_fallback, skip_assets=skip_assets)
    quote_asset = resolve_asset(quote, allow_symbol_fallback=allow_symbol_fallback, skip_assets=skip_assets)
    fee_amount, fee_currency = trade_fee(
        trade,
        allow_symbol_fallback=allow_symbol_fallback,
        skip_assets=skip_assets,
    )

    if side == "buy":
        spend_amount, spend_currency = str(cost), quote_asset
        receive_amount, receive_currency = str(amount), base_asset
    elif side == "sell":
        spend_amount, spend_currency = str(amount), base_asset
        receive_amount, receive_currency = str(cost), quote_asset
    else:
        raise ConversionSkip(f"Unsupported trade side {side!r}")

    return {
        "Location": str(trade.get("exchange") or "external"),
        "Timestamp": timestamp_to_seconds(trade.get("timestamp")),
        "Spend Amount": spend_amount,
        "Spend Currency": spend_currency,
        "Receive Amount": receive_amount,
        "Receive Currency": receive_currency,
        "Fee": fee_amount,
        "Fee Currency": fee_currency,
        "Description": f"ccxt trade {trade.get('id') or ''} {symbol} {side}".strip(),
    }


def movement_type(movement: Mapping[str, Any]) -> str:
    method = str(movement.get("source_method") or "").lower()
    raw_type = str(movement.get("type") or "").lower()
    direction = str(movement.get("direction") or "").lower()

    if "deposit" in method or raw_type == "deposit" or direction == "in":
        return "Deposit"
    if "withdraw" in method or raw_type in {"withdrawal", "withdraw"} or direction == "out":
        return "Withdrawal"
    if raw_type in {"staking", "reward", "rewards"}:
        return "Staking"
    if raw_type in {"fee", "fees"}:
        return "Spend"
    if raw_type in {"transfer", "transaction"}:
        return "Income" if decimal_from_value(movement.get("amount", 0), field_name="amount") > 0 else "Spend"
    return "Income" if decimal_from_value(movement.get("amount", 0), field_name="amount") > 0 else "Spend"


def movement_fee(movement: Mapping[str, Any], *, allow_symbol_fallback: bool, skip_assets: set[str]) -> tuple[str, str]:
    fee = movement.get("fee")
    if isinstance(fee, Mapping):
        cost = fee.get("cost")
        currency = fee.get("currency") or movement.get("currency")
    else:
        cost = fee
        currency = movement.get("fee_currency") or movement.get("currency")
    if cost in (None, "", 0, "0") or currency in (None, ""):
        return "", ""
    return str(decimal_from_value(cost, field_name="fee")), resolve_asset(
        currency,
        allow_symbol_fallback=allow_symbol_fallback,
        skip_assets=skip_assets,
    )


def convert_movement(movement: Mapping[str, Any], *, allow_symbol_fallback: bool, skip_assets: set[str]) -> dict[str, str]:
    currency = movement.get("currency")
    if currency in (None, ""):
        info = movement.get("info")
        if isinstance(info, Mapping):
            currency = info.get("coin") or info.get("currency") or info.get("asset")
    asset = resolve_asset(currency, allow_symbol_fallback=allow_symbol_fallback, skip_assets=skip_assets)
    amount = decimal_from_value(movement.get("amount"), field_name="amount")
    event_type = movement_type(movement)
    fee_amount, fee_currency = movement_fee(
        movement,
        allow_symbol_fallback=allow_symbol_fallback,
        skip_assets=skip_assets,
    )

    return {
        "Location": str(movement.get("exchange") or "external"),
        "Timestamp": timestamp_to_seconds(movement.get("timestamp")),
        "Type": event_type,
        "Amount": str(abs(amount)),
        "Currency": asset,
        "Fee": fee_amount,
        "Fee Currency": fee_currency,
        "Description": f"ccxt {movement.get('source_method') or 'movement'} {movement.get('id') or ''}".strip(),
    }


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def convert_history(
        data: dict[str, Any],
        *,
        allow_symbol_fallback: bool,
        skip_assets: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, Any]]]:
    trades = data.get("trades", [])
    movements = data.get("movements", [])
    if not isinstance(trades, list) or not isinstance(movements, list):
        raise ValueError("History JSON needs trades and movements lists")

    trade_rows: list[dict[str, str]] = []
    event_rows: list[dict[str, str]] = []
    skipped: list[dict[str, Any]] = []

    for index, trade in enumerate(trades, start=1):
        try:
            if not isinstance(trade, Mapping):
                raise ConversionSkip("Trade row is not an object")
            trade_rows.append(convert_trade(
                trade,
                allow_symbol_fallback=allow_symbol_fallback,
                skip_assets=skip_assets,
            ))
        except ConversionSkip as e:
            skipped.append({"kind": "trade", "index": index, "reason": str(e), "row": trade})

    for index, movement in enumerate(movements, start=1):
        try:
            if not isinstance(movement, Mapping):
                raise ConversionSkip("Movement row is not an object")
            event_rows.append(convert_movement(
                movement,
                allow_symbol_fallback=allow_symbol_fallback,
                skip_assets=skip_assets,
            ))
        except ConversionSkip as e:
            skipped.append({"kind": "movement", "index": index, "reason": str(e), "row": movement})

    return trade_rows, event_rows, skipped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert CCXT history JSON to rotki generic CSV files.")
    parser.add_argument("--input", required=True, type=Path, help="CCXT history JSON")
    parser.add_argument("--trades-output", required=True, type=Path, help="rotki generic trades CSV path")
    parser.add_argument("--events-output", required=True, type=Path, help="rotki generic events CSV path")
    parser.add_argument("--skipped-output", type=Path, help="Optional JSON report for skipped rows")
    parser.add_argument("--allow-symbol-fallback", action="store_true", help="Map unknown symbols to themselves")
    parser.add_argument("--skip-asset", action="append", default=[], help="Asset symbol or identifier to skip. Can be repeated")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        data = read_json_file(args.input)
        skip_assets = {str(asset).strip().upper() for asset in args.skip_asset if str(asset).strip()}
        trade_rows, event_rows, skipped = convert_history(
            data,
            allow_symbol_fallback=args.allow_symbol_fallback,
            skip_assets=skip_assets,
        )
        write_csv(args.trades_output, TRADE_FIELDS, trade_rows)
        write_csv(args.events_output, EVENT_FIELDS, event_rows)
        if args.skipped_output is not None:
            args.skipped_output.parent.mkdir(parents=True, exist_ok=True)
            args.skipped_output.write_text(json.dumps({
                "summary": {
                    "trades": len(trade_rows),
                    "events": len(event_rows),
                    "skipped": len(skipped),
                },
                "skipped": skipped,
            }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
