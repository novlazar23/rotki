"""Native CCXT integration helpers for exchange history collection.

This module is the backend foundation for managing CCXT-backed exchange history
inside rotki instead of through standalone scripts. It deliberately contains no
credential storage and no GUI code. The API layer can persist/profile these
settings and instantiate CCXT clients in a later step.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from rotkehlchen.types import TimestampMS

MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1000
TRANSIENT_CCXT_ERROR_MARKERS = (
    'server timeout',
    'retcode":10000',
    "retcode': 10000",
    'request timeout',
    'timed out',
)


@dataclass(frozen=True)
class CCXTExchangeProfile:
    """One concrete CCXT query profile.

    A single API key may need multiple profiles. For Bybit, for example, spot
    trades, swap trades and unified-account movements must not all be queried
    through one implicit defaultType because that risks wrong markets or
    duplicate movements.
    """

    exchange_id: str
    name: str
    options: dict[str, Any]
    symbols: list[str]
    history_params: dict[str, Any]
    collect_trades: bool
    collect_movements: bool
    collect_ledger: bool
    movement_window_days: int
    ledger_window_days: int

    def serialize(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CCXTHistoryEntry:
    """Serialized CCXT history entry with enough data for deduplication."""

    exchange: str
    source_method: str
    data: dict[str, Any]

    def serialize(self) -> dict[str, Any]:
        serialized = dict(self.data)
        serialized['exchange'] = self.exchange
        serialized['source_method'] = self.source_method
        return serialized


def utc_now_ms() -> TimestampMS:
    return TimestampMS(int(datetime.now(tz=UTC).timestamp() * 1000))


def bool_config(config: Mapping[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
    return bool(value)


def int_config(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f'{key} must be an integer, not a boolean')
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f'{key} must be an integer') from e


def list_str_config(config: Mapping[str, Any], key: str, default: list[str] | None = None) -> list[str]:
    value = config.get(key, default or [])
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        raise ValueError(f'{key} must be a list of strings')
    return value


def dict_config(config: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f'{key} must be an object')
    return value


def deep_merge_dicts(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge_dicts(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = value
    return result


def normalize_profile_config(
        base_config: Mapping[str, Any],
        variant_config: Mapping[str, Any] | None = None,
        index: int = 1,
) -> dict[str, Any]:
    """Merge a base CCXT exchange config with one optional profile variant."""
    base_without_variants = {key: value for key, value in base_config.items() if key != 'variants'}
    if variant_config is None:
        return dict(base_without_variants)

    variant_without_name = {
        key: value for key, value in variant_config.items()
        if key not in {'name', 'full_name'}
    }
    merged = deep_merge_dicts(base_without_variants, variant_without_name)
    base_name = str(base_config.get('name') or base_config.get('id') or 'exchange')
    variant_name = str(variant_config.get('name') or f'variant-{index}')
    merged['name'] = str(variant_config.get('full_name') or f'{base_name}-{variant_name}')
    return merged


def profile_from_config(config: Mapping[str, Any]) -> CCXTExchangeProfile:
    exchange_id = config.get('id')
    if not isinstance(exchange_id, str) or exchange_id == '':
        raise ValueError('CCXT profile needs a non-empty exchange id')

    name = config.get('name')
    if not isinstance(name, str) or name == '':
        name = exchange_id

    return CCXTExchangeProfile(
        exchange_id=exchange_id,
        name=name,
        options=dict_config(config, 'options'),
        symbols=list_str_config(config, 'symbols'),
        history_params=dict_config(config, 'history_params'),
        collect_trades=bool_config(config, 'collect_trades', True),
        collect_movements=bool_config(config, 'collect_movements', True),
        collect_ledger=bool_config(config, 'collect_ledger', bool_config(config, 'include_ledger', True)),
        movement_window_days=int_config(config, 'movement_window_days', 7),
        ledger_window_days=int_config(config, 'ledger_window_days', 1),
    )


def expand_profiles(exchange_config: Mapping[str, Any]) -> list[CCXTExchangeProfile]:
    """Expand a GUI/API CCXT exchange config into concrete query profiles."""
    variants = exchange_config.get('variants')
    if variants is None:
        return [profile_from_config(normalize_profile_config(exchange_config))]
    if not isinstance(variants, list) or len(variants) == 0:
        raise ValueError('variants must be a non-empty list when set')

    profiles: list[CCXTExchangeProfile] = []
    for index, variant in enumerate(variants, start=1):
        if not isinstance(variant, Mapping):
            raise ValueError('Each CCXT variant must be an object')
        profiles.append(profile_from_config(normalize_profile_config(exchange_config, variant, index)))
    return profiles


def is_transient_ccxt_history_error(error: Exception) -> bool:
    message = str(error).lower().replace(' ', '')
    return any(marker.replace(' ', '') in message for marker in TRANSIENT_CCXT_ERROR_MARKERS)


def global_history_entry_key(entry: Mapping[str, Any]) -> str:
    """Return a key used to deduplicate history returned by multiple CCXT profiles."""
    return json.dumps({
        'source_method': entry.get('source_method'),
        'id': entry.get('id'),
        'timestamp': entry.get('timestamp'),
        'datetime': entry.get('datetime'),
        'symbol': entry.get('symbol'),
        'currency': entry.get('currency'),
        'amount': entry.get('amount'),
        'side': entry.get('side'),
        'type': entry.get('type'),
        'txid': entry.get('txid'),
        'address': entry.get('address'),
    }, sort_keys=True, default=str)


def deduplicate_history_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduplicated: list[dict[str, Any]] = []
    for entry in entries:
        key = global_history_entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(entry)
    return deduplicated


def windowed_history_ranges(
        start_ms: TimestampMS,
        end_ms: TimestampMS | None,
        window_days: int,
) -> list[tuple[TimestampMS, TimestampMS]]:
    """Split a history range into exchange-safe windows.

    Used for exchanges such as Bybit that reject large deposit/withdrawal
    windows. The end timestamp defaults to current UTC time.
    """
    if window_days < 1:
        raise ValueError('window_days must be at least 1')

    final_end = end_ms or utc_now_ms()
    window_ms = window_days * MILLISECONDS_PER_DAY
    window_start = int(start_ms)
    ranges: list[tuple[TimestampMS, TimestampMS]] = []

    while window_start <= int(final_end):
        window_end = min(window_start + window_ms - 1, int(final_end))
        ranges.append((TimestampMS(window_start), TimestampMS(window_end)))
        window_start = window_end + 1

    return ranges
