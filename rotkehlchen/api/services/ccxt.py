from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from rotkehlchen.exchanges.ccxt_integration import expand_profiles

if TYPE_CHECKING:
    from rotkehlchen.rotkehlchen import Rotkehlchen

CCXT_FRONTEND_SETTINGS_KEY = 'ccxt_exchange_profiles'


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
