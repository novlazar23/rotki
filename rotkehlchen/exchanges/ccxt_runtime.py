"""Runtime adapter for querying CCXT-backed exchange profiles."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from rotkehlchen.exchanges.ccxt_integration import (
    CCXTExchangeProfile,
    deduplicate_history_entries,
    is_transient_ccxt_history_error,
    windowed_history_ranges,
)
from rotkehlchen.types import TimestampMS

try:
    import ccxt  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - optional dependency
    ccxt = None  # type: ignore[assignment]


@dataclass(frozen=True)
class CCXTQueryError:
    profile: str
    method: str
    message: str


@dataclass(frozen=True)
class CCXTHistoryResult:
    trades: list[dict[str, Any]]
    movements: list[dict[str, Any]]
    errors: list[CCXTQueryError]

    def serialize(self) -> dict[str, Any]:
        return {
            'trades': self.trades,
            'movements': self.movements,
            'errors': [asdict(error) for error in self.errors],
            'summary': {
                'trades': len(self.trades),
                'movements': len(self.movements),
                'errors': len(self.errors),
            },
        }


def create_ccxt_exchange(
        profile: CCXTExchangeProfile,
        api_key: str,
        api_secret: str | None,
        password: str | None = None,
) -> Any:
    if ccxt is None:
        raise RuntimeError('Missing optional dependency ccxt')

    exchange_class = getattr(ccxt, profile.exchange_id, None)
    if exchange_class is None:
        raise ValueError(f'CCXT does not provide exchange {profile.exchange_id!r}')

    constructor: dict[str, Any] = {
        'apiKey': api_key,
        'enableRateLimit': True,
        'options': profile.options,
    }
    if api_secret is not None:
        constructor['secret'] = api_secret
    if password is not None:
        constructor['password'] = password
    return exchange_class(constructor)


def _safe_timestamp(entry: Mapping[str, Any]) -> int:
    value = entry.get('timestamp')
    return int(value) if isinstance(value, int | float) else 0


def _annotate(profile: CCXTExchangeProfile, method_name: str, row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result['exchange'] = profile.name
    result['source_method'] = method_name
    return result


def _endpoint_params(profile: CCXTExchangeProfile, method_name: str) -> dict[str, Any]:
    method_params = profile.history_params.get(method_name)
    merged = {
        key: value
        for key, value in profile.history_params.items()
        if not isinstance(value, dict)
    }
    if isinstance(method_params, dict):
        merged.update(method_params)
    return merged


def _windowed_params(params: dict[str, Any], end_ms: int | None) -> dict[str, Any]:
    if end_ms is None:
        return params
    result = dict(params)
    result.setdefault('endTime', end_ms)
    result.setdefault('end_time', end_ms)
    return result


def _fetch_paginated(
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
        rows = method(symbol=symbol, since=next_since, limit=limit, params=params) if symbol else method(
            since=next_since,
            limit=limit,
            params=params,
        )
        if not rows:
            break

        page_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
        if until is not None:
            page_rows = [row for row in page_rows if _safe_timestamp(row) <= until]
        collected.extend(page_rows)

        timestamps = [_safe_timestamp(row) for row in page_rows if _safe_timestamp(row) > 0]
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


def _fetch_windowed(
        exchange: Any,
        method_name: str,
        *,
        since_ms: TimestampMS,
        until_ms: TimestampMS | None,
        limit: int,
        max_pages: int,
        params: dict[str, Any],
        window_days: int,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for start_ms, end_ms in windowed_history_ranges(
        start_ms=since_ms,
        end_ms=until_ms,
        window_days=window_days,
    ):
        collected.extend(_fetch_paginated(
            exchange,
            method_name,
            since=int(start_ms),
            until=int(end_ms),
            limit=limit,
            max_pages=max_pages,
            params=_windowed_params(params, int(end_ms)),
        ))
    return collected


def query_ccxt_profile_history(
        profile: CCXTExchangeProfile,
        exchange: Any,
        *,
        start_ms: TimestampMS,
        end_ms: TimestampMS | None,
        limit: int = 200,
        max_pages: int = 10,
) -> CCXTHistoryResult:
    trades: list[dict[str, Any]] = []
    movements: list[dict[str, Any]] = []
    errors: list[CCXTQueryError] = []

    if profile.collect_trades:
        for symbol in profile.symbols:
            try:
                rows = _fetch_paginated(
                    exchange,
                    'fetch_my_trades',
                    since=int(start_ms),
                    until=int(end_ms) if end_ms is not None else None,
                    limit=limit,
                    max_pages=max_pages,
                    params=_endpoint_params(profile, 'fetch_my_trades'),
                    symbol=symbol,
                )
                trades.extend(_annotate(profile, 'fetch_my_trades', row) for row in rows)
            except Exception as e:  # noqa: BLE001
                errors.append(CCXTQueryError(profile=profile.name, method=f'fetch_my_trades:{symbol}', message=str(e)))

    if profile.collect_movements:
        for method_name in ('fetch_deposits', 'fetch_withdrawals'):
            try:
                rows = _fetch_windowed(
                    exchange,
                    method_name,
                    since_ms=start_ms,
                    until_ms=end_ms,
                    limit=limit,
                    max_pages=max_pages,
                    params=_endpoint_params(profile, method_name),
                    window_days=profile.movement_window_days,
                )
                movements.extend(_annotate(profile, method_name, row) for row in rows)
            except Exception as e:  # noqa: BLE001
                errors.append(CCXTQueryError(profile=profile.name, method=method_name, message=str(e)))

    if profile.collect_ledger:
        try:
            rows = _fetch_windowed(
                exchange,
                'fetch_ledger',
                since_ms=start_ms,
                until_ms=end_ms,
                limit=limit,
                max_pages=max_pages,
                params=_endpoint_params(profile, 'fetch_ledger'),
                window_days=profile.ledger_window_days,
            )
            movements.extend(_annotate(profile, 'fetch_ledger', row) for row in rows)
        except Exception as e:  # noqa: BLE001
            if is_transient_ccxt_history_error(e) and profile.ledger_window_days > 1:
                try:
                    rows = _fetch_windowed(
                        exchange,
                        'fetch_ledger',
                        since_ms=start_ms,
                        until_ms=end_ms,
                        limit=limit,
                        max_pages=max_pages,
                        params=_endpoint_params(profile, 'fetch_ledger'),
                        window_days=1,
                    )
                    movements.extend(_annotate(profile, 'fetch_ledger', row) for row in rows)
                except Exception as retry_error:  # noqa: BLE001
                    errors.append(CCXTQueryError(profile=profile.name, method='fetch_ledger', message=str(retry_error)))
            else:
                errors.append(CCXTQueryError(profile=profile.name, method='fetch_ledger', message=str(e)))

    return CCXTHistoryResult(
        trades=deduplicate_history_entries(trades),
        movements=deduplicate_history_entries(movements),
        errors=errors,
    )
