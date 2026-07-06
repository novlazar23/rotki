from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from rotkehlchen.exchanges.ccxt_integration import (
    CCXTExchangeProfile,
    TimestampMS,
    deduplicate_history_entries,
    expand_profiles,
)
from rotkehlchen.exchanges.ccxt_runtime import create_ccxt_exchange, query_ccxt_profile_history
from rotkehlchen.types import Location

if TYPE_CHECKING:
    from rotkehlchen.rotkehlchen import Rotkehlchen

CCXT_FRONTEND_SETTINGS_KEY = 'ccxt_exchange_profiles'


@dataclass(frozen=True)
class CCXTRotkiCredentials:
    key: str
    secret: str | None
    passphrase: str | None


class CCXTService:
    def __init__(self, rotkehlchen: Rotkehlchen) -> None:
        self.rotkehlchen = rotkehlchen

    @staticmethod
    def _validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
        profiles = expand_profiles(profile)
        return {
            'profile': profile,
            'expanded_profiles': [entry.serialize() for entry in profiles],
        }

    @staticmethod
    def _credential_location_from_profile(profile: CCXTExchangeProfile) -> Location:
        location_value = profile.credential_location or profile.exchange_id
        normalized = location_value.replace('-', '').replace('_', '').lower()
        if normalized == 'bybit':
            return Location.BYBIT
        if normalized == 'binance':
            return Location.BINANCE
        if normalized == 'okx':
            return Location.OKX
        if normalized == 'kucoin':
            return Location.KUCOIN
        if normalized == 'gate':
            return Location.GATE
        raise ValueError(f'Unsupported CCXT credential location {location_value!r}')

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode()
        return str(value)

    def resolve_profile_credentials(self, profile: CCXTExchangeProfile) -> CCXTRotkiCredentials:
        credential_name = profile.credential_name or profile.name.rsplit('-', 1)[0]
        location = self._credential_location_from_profile(profile)
        with self.rotkehlchen.data.db.conn.read_ctx() as cursor:
            row = cursor.execute(
                'SELECT api_key, api_secret, passphrase FROM user_credentials WHERE name=? AND location=?;',
                (credential_name, location.serialize_for_db()),
            ).fetchone()

        if row is None:
            raise ValueError(
                f'No stored rotki credentials found for {credential_name!r} at {location.serialize()!r}',
            )

        return CCXTRotkiCredentials(
            key=str(row[0]),
            secret=self._string_or_none(row[1]),
            passphrase=self._string_or_none(row[2]),
        )

    def _load_frontend_settings(self) -> dict[str, Any]:
        with self.rotkehlchen.data.db.conn.read_ctx() as cursor:
            row = cursor.execute(
                'SELECT value FROM settings WHERE name=?;',
                ('frontend_settings',),
            ).fetchone()

        if row is None or row[0] in (None, ''):
            return {}
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_frontend_settings(self, data: dict[str, Any]) -> None:
        with self.rotkehlchen.data.db.user_write() as write_cursor:
            write_cursor.execute(
                'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?);',
                ('frontend_settings', json.dumps(data, sort_keys=True)),
            )

    def _load_profiles(self) -> list[dict[str, Any]]:
        profiles = self._load_frontend_settings().get(CCXT_FRONTEND_SETTINGS_KEY, [])
        return profiles if isinstance(profiles, list) else []

    def _save_profiles(self, profiles: list[dict[str, Any]]) -> None:
        frontend_settings = self._load_frontend_settings()
        frontend_settings[CCXT_FRONTEND_SETTINGS_KEY] = profiles
        self._save_frontend_settings(frontend_settings)

    def get_profiles(self) -> dict[str, Any]:
        profiles = self._load_profiles()
        return {
            'result': {
                'profiles': profiles,
                'expanded_profiles': [
                    expanded.serialize()
                    for profile in profiles
                    for expanded in expand_profiles(profile)
                ],
            },
            'message': '',
            'status_code': HTTPStatus.OK,
        }

    def _get_profile_by_name(self, name: str) -> dict[str, Any] | None:
        for profile in self._load_profiles():
            if profile.get('name') == name:
                return profile
        return None

    def upsert_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        try:
            validated = self._validate_profile(profile)
        except ValueError as e:
            return {'result': None, 'message': str(e), 'status_code': HTTPStatus.BAD_REQUEST}

        name = profile.get('name')
        if not isinstance(name, str) or name == '':
            return {
                'result': None,
                'message': 'CCXT profile needs a non-empty name',
                'status_code': HTTPStatus.BAD_REQUEST,
            }

        current = self._load_profiles()
        updated = [entry for entry in current if entry.get('name') != name]
        updated.append(profile)
        updated.sort(key=lambda entry: str(entry.get('name', '')))
        self._save_profiles(updated)

        return {'result': validated, 'message': '', 'status_code': HTTPStatus.OK}

    def preview_stored_profile_history(
            self,
            name: str,
            start_ms: int,
            end_ms: int | None,
            limit: int,
            max_pages: int,
    ) -> dict[str, Any]:
        profile = self._get_profile_by_name(name)
        if profile is None:
            return {
                'result': None,
                'message': f'No CCXT profile named {name} exists',
                'status_code': HTTPStatus.NOT_FOUND,
            }

        try:
            expanded_profiles = expand_profiles(profile)
        except ValueError as e:
            return {'result': None, 'message': str(e), 'status_code': HTTPStatus.BAD_REQUEST}

        all_trades: list[dict[str, Any]] = []
        all_movements: list[dict[str, Any]] = []
        all_errors: list[dict[str, Any]] = []
        profile_results: list[dict[str, Any]] = []
        discovered_symbols: dict[str, list[str]] = {}

        for expanded in expanded_profiles:
            try:
                credentials = self.resolve_profile_credentials(expanded)
                exchange = create_ccxt_exchange(
                    profile=expanded,
                    api_key=credentials.key,
                    api_secret=credentials.secret,
                    password=credentials.passphrase,
                )
                result = query_ccxt_profile_history(
                    profile=expanded,
                    exchange=exchange,
                    start_ms=TimestampMS(start_ms),
                    end_ms=TimestampMS(end_ms) if end_ms is not None else None,
                    limit=limit,
                    max_pages=max_pages,
                ).serialize()
            except Exception as e:  # noqa: BLE001
                result = {
                    'trades': [],
                    'movements': [],
                    'errors': [{
                        'profile': expanded.name,
                        'method': 'profile_query',
                        'message': str(e),
                    }],
                    'discovered_symbols': {},
                    'summary': {
                        'trades': 0,
                        'movements': 0,
                        'errors': 1,
                        'discovered_symbols': 0,
                    },
                }

            all_trades.extend(result['trades'])
            all_movements.extend(result['movements'])
            all_errors.extend(result['errors'])
            discovered_symbols.update(result['discovered_symbols'])
            profile_results.append({
                'profile': expanded.serialize(),
                'summary': result['summary'],
                'errors': result['errors'],
                'discovered_symbols': result['discovered_symbols'],
            })

        deduplicated_trades = deduplicate_history_entries(all_trades)
        deduplicated_movements = deduplicate_history_entries(all_movements)
        return {
            'result': {
                'profile': profile,
                'profiles': profile_results,
                'trades': deduplicated_trades,
                'movements': deduplicated_movements,
                'errors': all_errors,
                'discovered_symbols': discovered_symbols,
                'summary': {
                    'profiles': len(expanded_profiles),
                    'trades': len(deduplicated_trades),
                    'movements': len(deduplicated_movements),
                    'errors': len(all_errors),
                    'discovered_symbols': sum(len(symbols) for symbols in discovered_symbols.values()),
                },
            },
            'message': '',
            'status_code': HTTPStatus.OK,
        }

    def delete_profile(self, name: str) -> dict[str, Any]:
        current = self._load_profiles()
        updated = [entry for entry in current if entry.get('name') != name]
        if len(updated) == len(current):
            return {
                'result': None,
                'message': f'No CCXT profile named {name} exists',
                'status_code': HTTPStatus.NOT_FOUND,
            }

        self._save_profiles(updated)
        return {'result': True, 'message': '', 'status_code': HTTPStatus.OK}
