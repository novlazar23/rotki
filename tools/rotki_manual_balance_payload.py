#!/usr/bin/env python3
"""Convert collected CCXT balances into dry-run rotki manual balance payloads.

This tool does not call rotki and does not mutate any database. It validates the
collector output and writes an auditable JSON file that can be reviewed before a
future importer writes manual balances to rotki.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ManualBalanceCandidate:
    asset: str
    amount: str
    label: str
    location: str
    tags: list[str]
    source: dict[str, Any]


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def read_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Input file {path} must contain a JSON object")
    return data


def decimal_from_string(value: Any, *, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"Can not convert {field_name} value {value!r} to Decimal") from e


def normalized_label_part(value: Any) -> str:
    return str(value).strip().replace(" ", "_")


def validate_collector_summary(data: dict[str, Any], *, allow_unmapped: bool) -> None:
    summary = data.get("summary", {})
    if not isinstance(summary, dict):
        raise ValueError("Collector JSON must contain a summary object")

    unmapped_symbols = summary.get("unmapped_symbols", [])
    if not isinstance(unmapped_symbols, list):
        raise ValueError("summary.unmapped_symbols must be a list")

    if unmapped_symbols and not allow_unmapped:
        raise ValueError(
            "Collector output still has unmapped symbols: " + ", ".join(map(str, unmapped_symbols)),
        )


def candidate_from_balance(
        balance: dict[str, Any],
        *,
        amount_field: str,
        label_prefix: str,
        location: str,
        extra_tags: list[str],
) -> ManualBalanceCandidate | None:
    asset_identifier = balance.get("asset_identifier")
    if not isinstance(asset_identifier, str) or asset_identifier.strip() == "":
        return None

    amount = decimal_from_string(balance.get(amount_field), field_name=amount_field)
    if amount == 0:
        return None

    exchange = normalized_label_part(balance.get("exchange", "exchange"))
    symbol = normalized_label_part(balance.get("symbol", asset_identifier))
    label = f"{label_prefix}:{exchange}:{symbol}"

    tags = [label_prefix, exchange, *extra_tags]
    mapping_source = balance.get("mapping_source")
    if isinstance(mapping_source, str) and mapping_source:
        tags.append(f"mapping-{mapping_source}")

    return ManualBalanceCandidate(
        asset=asset_identifier.strip(),
        amount=str(amount),
        label=label,
        location=location,
        tags=tags,
        source={
            "exchange": balance.get("exchange"),
            "account": balance.get("account"),
            "symbol": balance.get("symbol"),
            "free": balance.get("free"),
            "used": balance.get("used"),
            "total": balance.get("total"),
            "timestamp": balance.get("timestamp"),
            "mapping_source": mapping_source,
        },
    )


def build_candidates(
        data: dict[str, Any],
        *,
        amount_field: str,
        label_prefix: str,
        location: str,
        extra_tags: list[str],
        allow_unmapped: bool,
) -> list[ManualBalanceCandidate]:
    validate_collector_summary(data, allow_unmapped=allow_unmapped)

    balances = data.get("balances")
    if not isinstance(balances, list):
        raise ValueError("Collector JSON must contain a balances list")

    candidates: list[ManualBalanceCandidate] = []
    skipped_missing_identifier = 0
    skipped_zero_amount = 0

    for raw_balance in balances:
        if not isinstance(raw_balance, dict):
            raise ValueError("Each balance row must be an object")
        candidate = candidate_from_balance(
            raw_balance,
            amount_field=amount_field,
            label_prefix=label_prefix,
            location=location,
            extra_tags=extra_tags,
        )
        if candidate is None:
            if raw_balance.get("asset_identifier") in (None, ""):
                skipped_missing_identifier += 1
            else:
                skipped_zero_amount += 1
            continue
        candidates.append(candidate)

    if skipped_missing_identifier and not allow_unmapped:
        raise ValueError(f"Skipped {skipped_missing_identifier} balances without asset_identifier")

    return candidates


def output_payload(
        *,
        input_path: Path,
        data: dict[str, Any],
        candidates: list[ManualBalanceCandidate],
        amount_field: str,
        label_prefix: str,
        location: str,
) -> dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "source_file": str(input_path),
        "mode": "dry_run",
        "settings": {
            "amount_field": amount_field,
            "label_prefix": label_prefix,
            "location": location,
        },
        "collector_summary": data.get("summary", {}),
        "summary": {
            "manual_balance_candidates": len(candidates),
        },
        "manual_balance_candidates": [asdict(candidate) for candidate in candidates],
    }


def write_output(output_path: Path | None, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    if output_path is None:
        print(text)
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create dry-run rotki manual balance payloads.")
    parser.add_argument("--input", required=True, type=Path, help="Collector JSON input path")
    parser.add_argument("--output", type=Path, help="Output JSON path. Defaults to stdout.")
    parser.add_argument(
        "--amount-field",
        choices=("total", "free"),
        default="total",
        help="Balance field to use as manual balance amount.",
    )
    parser.add_argument("--label-prefix", default="ccxt", help="Prefix used for labels and tags.")
    parser.add_argument("--location", default="external", help="Target manual balance location label.")
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Extra tag to attach to every generated candidate. Can be repeated.",
    )
    parser.add_argument(
        "--allow-unmapped",
        action="store_true",
        help="Allow rows without asset identifiers to be skipped instead of failing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        data = read_json_file(args.input)
        candidates = build_candidates(
            data,
            amount_field=args.amount_field,
            label_prefix=args.label_prefix,
            location=args.location,
            extra_tags=args.tag,
            allow_unmapped=args.allow_unmapped,
        )
        payload = output_payload(
            input_path=args.input,
            data=data,
            candidates=candidates,
            amount_field=args.amount_field,
            label_prefix=args.label_prefix,
            location=args.location,
        )
        write_output(args.output, payload)
    except Exception as e:  # noqa: BLE001 - concise CLI diagnostics
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
