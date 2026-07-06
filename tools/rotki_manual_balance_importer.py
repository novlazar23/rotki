#!/usr/bin/env python3
"""Import generated manual balance candidates into rotki.

Default mode is an offline dry-run. Use --check-existing to query a running
rotki instance during dry-run. Use --apply explicitly to write to rotki.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PlannedImport:
    endpoint: str
    add_payload: dict[str, Any]
    delete_payload: dict[str, Any] | None
    existing_labels: list[str]
    existing_check: str


def read_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Input file {path} must contain a JSON object")
    return data


def normalize_base_url(url: str) -> str:
    normalized = url.rstrip("/")
    if normalized.endswith("/api/1"):
        return normalized
    return normalized + "/api/1"


def extract_result(response: dict[str, Any]) -> Any:
    if "result" in response:
        return response["result"]
    return response


class RotkiAPIClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"rotki API {method} {path} failed with HTTP {e.code}: {error_body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"rotki API {method} {path} failed: {e.reason}") from e

        if response_body == "":
            return {}
        data = json.loads(response_body)
        if not isinstance(data, dict):
            raise RuntimeError(f"rotki API {method} {path} returned non-object JSON")
        return data

    def get_manual_balances(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/balances/manual")
        result = extract_result(response)
        if isinstance(result, dict) and "balances" in result:
            result = result["balances"]
        if result is None:
            return []
        if not isinstance(result, list):
            raise RuntimeError("Unexpected rotki manual balances response shape")
        return [entry for entry in result if isinstance(entry, dict)]

    def delete_manual_balance_ids(self, ids: list[int]) -> dict[str, Any]:
        if not ids:
            return {"result": True}
        return self.request("DELETE", "/balances/manual", {"async_query": False, "ids": ids})

    def add_manual_balances(self, balances: list[dict[str, Any]]) -> dict[str, Any]:
        if not balances:
            raise ValueError("No balances to add")
        return self.request("PUT", "/balances/manual", {"async_query": False, "balances": balances})


def candidate_to_rotki_balance(candidate: dict[str, Any], *, include_tags: bool) -> dict[str, Any]:
    asset = candidate.get("asset")
    amount = candidate.get("amount")
    label = candidate.get("label")
    location = candidate.get("location", "external")

    missing_fields = [
        name for name, value in {
            "asset": asset,
            "amount": amount,
            "label": label,
            "location": location,
        }.items()
        if not isinstance(value, str) or value.strip() == ""
    ]
    if missing_fields:
        raise ValueError(f"Manual balance candidate misses required fields: {', '.join(missing_fields)}")

    result: dict[str, Any] = {
        "asset": asset.strip(),
        "amount": amount.strip(),
        "label": label.strip(),
        "location": location.strip(),
        "balance_type": "asset",
        "tags": None,
    }

    tags = candidate.get("tags")
    if include_tags and isinstance(tags, list):
        result["tags"] = [str(tag) for tag in tags if str(tag).strip() != ""] or None

    return result


def load_rotki_balances_from_payload(payload: dict[str, Any], *, include_tags: bool) -> list[dict[str, Any]]:
    candidates = payload.get("manual_balance_candidates")
    if not isinstance(candidates, list):
        raise ValueError("Payload needs a manual_balance_candidates list")

    balances: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Each manual balance candidate must be an object")
        balance = candidate_to_rotki_balance(candidate, include_tags=include_tags)
        label = balance["label"]
        if label in seen_labels:
            raise ValueError(f"Duplicate label in payload: {label}")
        seen_labels.add(label)
        balances.append(balance)

    if not balances:
        raise ValueError("No manual balance candidates found")
    return balances


def select_existing_by_label_prefix(existing: list[dict[str, Any]], label_prefix: str) -> tuple[list[int], list[str]]:
    ids: list[int] = []
    labels: list[str] = []
    prefix = label_prefix if label_prefix.endswith(":") else label_prefix + ":"
    for entry in existing:
        label = entry.get("label")
        identifier = entry.get("identifier")
        if isinstance(label, str) and label.startswith(prefix):
            labels.append(label)
            if isinstance(identifier, int):
                ids.append(identifier)
            elif isinstance(identifier, str) and identifier.isdigit():
                ids.append(int(identifier))
            else:
                raise ValueError(f"Existing balance {label!r} has no numeric identifier")
    return ids, labels


def build_plan(
        *,
        endpoint: str,
        balances: list[dict[str, Any]],
        existing: list[dict[str, Any]],
        replace_prefix: str | None,
        existing_check: str,
) -> PlannedImport:
    delete_payload = None
    existing_labels: list[str] = []
    if replace_prefix is not None and existing_check == "online":
        delete_ids, existing_labels = select_existing_by_label_prefix(existing, replace_prefix)
        delete_payload = {"async_query": False, "ids": delete_ids} if delete_ids else None

    return PlannedImport(
        endpoint=endpoint,
        add_payload={"async_query": False, "balances": balances},
        delete_payload=delete_payload,
        existing_labels=existing_labels,
        existing_check=existing_check,
    )


def write_plan(output_path: Path | None, plan: PlannedImport) -> None:
    payload = {
        "mode": "dry_run",
        "endpoint": plan.endpoint,
        "existing_check": plan.existing_check,
        "summary": {
            "balances_to_add": len(plan.add_payload["balances"]),
            "existing_labels_to_replace": len(plan.existing_labels),
            "ids_to_delete": len(plan.delete_payload["ids"]) if plan.delete_payload else 0,
        },
        "delete_payload": plan.delete_payload,
        "add_payload": plan.add_payload,
        "existing_labels": plan.existing_labels,
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if output_path is None:
        print(text)
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import dry-run manual balance payloads into rotki.")
    parser.add_argument("--input", required=True, type=Path, help="Manual balance payload JSON")
    parser.add_argument("--url", default="http://127.0.0.1:4242", help="rotki base URL")
    parser.add_argument("--dry-run-output", type=Path, help="Write the planned API payload to a file")
    parser.add_argument("--check-existing", action="store_true", help="Query rotki during dry-run to detect existing prefixed manual balances")
    parser.add_argument("--apply", action="store_true", help="Actually write to rotki")
    parser.add_argument("--replace-prefix", default="ccxt", help="Delete existing manual balances whose label starts with this prefix before adding")
    parser.add_argument("--no-replace", action="store_true", help="Do not delete existing prefixed manual balances before adding")
    parser.add_argument("--include-tags", action="store_true", help="Send candidate tags to rotki. Tags must already exist in rotki")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = read_json_file(args.input)
        balances = load_rotki_balances_from_payload(payload, include_tags=args.include_tags)
        client = RotkiAPIClient(args.url, timeout=args.timeout)

        existing: list[dict[str, Any]] = []
        existing_check = "offline"
        if args.apply or args.check_existing:
            existing = client.get_manual_balances()
            existing_check = "online"

        replace_prefix = None if args.no_replace else args.replace_prefix
        plan = build_plan(
            endpoint=client.base_url + "/balances/manual",
            balances=balances,
            existing=existing,
            replace_prefix=replace_prefix,
            existing_check=existing_check,
        )

        if not args.apply:
            write_plan(args.dry_run_output, plan)
            return 0

        if plan.delete_payload is not None:
            client.delete_manual_balance_ids(plan.delete_payload["ids"])
        response = client.add_manual_balances(plan.add_payload["balances"])
        print(json.dumps({"result": extract_result(response)}, indent=2, sort_keys=True))
    except Exception as e:  # noqa: BLE001 - concise CLI diagnostics
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
