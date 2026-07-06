#!/usr/bin/env python3
"""Automate CCXT history collection, conversion and rotki CSV import.

The pipeline is intentionally conservative:

1. collect CCXT history into JSON
2. convert JSON into rotki generic trades/events CSV files
3. import CSV files into rotki only with --apply
4. on import errors, detect unknown assets, skip them, regenerate CSVs and retry
5. write a full run report
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import uuid
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from tools.ccxt_history_collector import collect_history
    from tools.ccxt_history_to_rotki_csv import EVENT_FIELDS, TRADE_FIELDS, convert_history, write_csv
    from tools.rotki_manual_balance_importer import RotkiAPIClient, RotkiAPIError, extract_result
except ImportError:  # allows direct execution when copied next to the helpers
    from ccxt_history_collector import collect_history  # type: ignore[no-redef]
    from ccxt_history_to_rotki_csv import EVENT_FIELDS, TRADE_FIELDS, convert_history, write_csv  # type: ignore[no-redef]
    from rotki_manual_balance_importer import RotkiAPIClient, RotkiAPIError, extract_result  # type: ignore[no-redef]

UNKNOWN_ASSET_PATTERN = re.compile(r"Unknown asset ([A-Za-z0-9_:\-/x]+)")


@dataclass(frozen=True)
class ImportAttempt:
    source: str
    csv_path: str
    attempt: int
    success: bool
    correction_assets: list[str]
    message: str


@dataclass(frozen=True)
class PipelineFiles:
    history_json: Path
    trades_csv: Path
    events_csv: Path
    skipped_json: Path
    report_json: Path


def read_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"JSON file {path} must contain an object")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def pipeline_files(workdir: Path) -> PipelineFiles:
    return PipelineFiles(
        history_json=workdir / "ccxt-history.json",
        trades_csv=workdir / "rotki-trades.csv",
        events_csv=workdir / "rotki-events.csv",
        skipped_json=workdir / "rotki-history-skipped.json",
        report_json=workdir / "ccxt-history-import-report.json",
    )


def extract_unknown_assets_from_text(text: str) -> list[str]:
    return sorted(set(UNKNOWN_ASSET_PATTERN.findall(text)))


def encode_multipart_formdata(
        fields: dict[str, str],
        *,
        file_field: str,
        file_path: Path,
) -> tuple[bytes, str]:
    boundary = f"----rotki-ccxt-{uuid.uuid4().hex}"
    body_parts: list[bytes] = []

    for name, value in fields.items():
        body_parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ])

    filename = file_path.name
    content_type = mimetypes.guess_type(filename)[0] or "text/csv"
    body_parts.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        file_path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(body_parts), boundary


def post_csv_import(client: RotkiAPIClient, *, source: str, csv_path: Path) -> dict[str, Any]:
    body, boundary = encode_multipart_formdata(
        {
            "async_query": "false",
            "source": source,
        },
        file_field="file",
        file_path=csv_path,
    )
    request = urllib.request.Request(
        url=client.base_url + "/import",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with client.opener.open(request, timeout=client.timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RotkiAPIError(
            f"rotki API POST /import failed with HTTP {e.code}: {error_body}",
            status_code=e.code,
            body=error_body,
        ) from e
    except urllib.error.URLError as e:
        raise RotkiAPIError(f"rotki API POST /import failed: {e.reason}") from e

    if response_body == "":
        return {}
    data = json.loads(response_body)
    if not isinstance(data, dict):
        raise RuntimeError("rotki API POST /import returned non-object JSON")
    result = extract_result(data)
    if result is False:
        raise RuntimeError(json.dumps(data, sort_keys=True))
    return data


def regenerate_csvs(
        *,
        history_data: dict[str, Any],
        files: PipelineFiles,
        allow_symbol_fallback: bool,
        skip_assets: set[str],
) -> dict[str, Any]:
    trade_rows, event_rows, skipped = convert_history(
        history_data,
        allow_symbol_fallback=allow_symbol_fallback,
        skip_assets=skip_assets,
    )
    write_csv(files.trades_csv, TRADE_FIELDS, trade_rows)
    write_csv(files.events_csv, EVENT_FIELDS, event_rows)
    skipped_report = {
        "summary": {
            "trades": len(trade_rows),
            "events": len(event_rows),
            "skipped": len(skipped),
            "skip_assets": sorted(skip_assets),
        },
        "skipped": skipped,
    }
    write_json(files.skipped_json, skipped_report)
    return skipped_report


def import_with_corrections(
        *,
        client: RotkiAPIClient,
        source: str,
        csv_path_getter: Any,
        history_data: dict[str, Any],
        files: PipelineFiles,
        allow_symbol_fallback: bool,
        skip_assets: set[str],
        max_corrections: int,
) -> tuple[list[ImportAttempt], set[str], dict[str, Any]]:
    attempts: list[ImportAttempt] = []
    skipped_report: dict[str, Any] = {}

    for attempt_number in range(1, max_corrections + 2):
        csv_path = csv_path_getter(files)
        try:
            response = post_csv_import(client, source=source, csv_path=csv_path)
            attempts.append(ImportAttempt(
                source=source,
                csv_path=str(csv_path),
                attempt=attempt_number,
                success=True,
                correction_assets=[],
                message=json.dumps(response, sort_keys=True, default=str),
            ))
            return attempts, skip_assets, skipped_report
        except RotkiAPIError as e:
            unknown_assets = extract_unknown_assets_from_text(e.body or str(e))
            message = str(e)
        except RuntimeError as e:
            unknown_assets = extract_unknown_assets_from_text(str(e))
            message = str(e)

        if not unknown_assets:
            attempts.append(ImportAttempt(
                source=source,
                csv_path=str(csv_path),
                attempt=attempt_number,
                success=False,
                correction_assets=[],
                message=message,
            ))
            return attempts, skip_assets, skipped_report

        new_assets = set(unknown_assets) - skip_assets
        attempts.append(ImportAttempt(
            source=source,
            csv_path=str(csv_path),
            attempt=attempt_number,
            success=False,
            correction_assets=sorted(new_assets),
            message=message,
        ))
        if not new_assets or attempt_number > max_corrections:
            return attempts, skip_assets, skipped_report

        skip_assets.update(new_assets)
        skipped_report = regenerate_csvs(
            history_data=history_data,
            files=files,
            allow_symbol_fallback=allow_symbol_fallback,
            skip_assets=skip_assets,
        )

    return attempts, skip_assets, skipped_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect, convert and import CCXT history into rotki.")
    parser.add_argument("--config", required=True, type=Path, help="CCXT history collector config JSON")
    parser.add_argument("--workdir", type=Path, default=Path("out/ccxt-history-import"), help="Directory for generated files")
    parser.add_argument("--url", default="http://127.0.0.1:4242", help="rotki base URL")
    parser.add_argument("--apply", action="store_true", help="Actually import into rotki. Without this only files and report are generated")
    parser.add_argument("--reuse-history", action="store_true", help="Reuse existing history JSON instead of collecting again")
    parser.add_argument("--allow-symbol-fallback", action="store_true", help="Map unknown symbols to themselves during CSV conversion")
    parser.add_argument("--skip-asset", action="append", default=[], help="Asset symbol or identifier to skip. Can be repeated")
    parser.add_argument("--max-corrections", type=int, default=3, help="Maximum unknown-asset correction rounds")
    parser.add_argument("--timeout", type=float, default=60.0, help="rotki API timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    files = pipeline_files(args.workdir)
    args.workdir.mkdir(parents=True, exist_ok=True)
    skip_assets = {str(asset).strip().upper() for asset in args.skip_asset if str(asset).strip()}
    attempts: list[ImportAttempt] = []

    try:
        if args.reuse_history:
            history_data = read_json_file(files.history_json)
        else:
            config = read_json_file(args.config)
            history_data = collect_history(config)
            write_json(files.history_json, history_data)

        skipped_report = regenerate_csvs(
            history_data=history_data,
            files=files,
            allow_symbol_fallback=args.allow_symbol_fallback,
            skip_assets=skip_assets,
        )

        if args.apply:
            client = RotkiAPIClient(args.url, timeout=args.timeout)
            trade_attempts, skip_assets, skipped_report = import_with_corrections(
                client=client,
                source="rotki trades",
                csv_path_getter=lambda generated_files: generated_files.trades_csv,
                history_data=history_data,
                files=files,
                allow_symbol_fallback=args.allow_symbol_fallback,
                skip_assets=skip_assets,
                max_corrections=args.max_corrections,
            )
            attempts.extend(trade_attempts)

            event_attempts, skip_assets, skipped_report = import_with_corrections(
                client=client,
                source="rotki events",
                csv_path_getter=lambda generated_files: generated_files.events_csv,
                history_data=history_data,
                files=files,
                allow_symbol_fallback=args.allow_symbol_fallback,
                skip_assets=skip_assets,
                max_corrections=args.max_corrections,
            )
            attempts.extend(event_attempts)

        report = {
            "mode": "apply" if args.apply else "dry_run",
            "files": {key: str(value) for key, value in asdict(files).items()},
            "history_summary": history_data.get("summary", {}),
            "conversion_summary": skipped_report.get("summary", {}),
            "final_skip_assets": sorted(skip_assets),
            "import_attempts": [asdict(attempt) for attempt in attempts],
            "success": all(attempt.success for attempt in attempts) if attempts else True,
        }
        write_json(files.report_json, report)
        print(json.dumps(report["history_summary"], indent=2, sort_keys=True))
        print(f"Report: {files.report_json}")
        if report["success"] is False:
            return 1
    except Exception as e:  # noqa: BLE001 - CLI diagnostics
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
