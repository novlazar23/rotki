from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from rotkehlchen.exchanges.ccxt_integration import expand_profiles

if TYPE_CHECKING:
    from rotkehlchen.rotkehlchen import Rotkehlchen

CCXT_PROFILES_SETTINGS_KEY = 'ccxt_exchange_profiles'


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

    def get_profiles(self) -> dict[str, Any]:
        with self.rotkehlchen.data.db.conn.read_ctx() as cursor:
            row = cursor.execute(
                'SELECT value FROM settings WHERE name=?;',
                (CCXT_PROFILES_SETTINGS_KEY,),
            ).fetchone()

        if row is None:
            profiles: list[dict[str, Any]] = []
        else:
            try:
                loaded = json.loads(row[0])
            except json.JSONDecodeError:
                loaded = []
            profiles = loaded if isinstance(loaded, list) else []

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

        current = self.get_profiles()['result']['profiles']
        updated = [entry for entry in current if entry.get('name') != name]
        updated.append(profile)
        updated.sort(key=lambda entry: str(entry.get('name', '')))

        with self.rotkehlchen.data.db.user_write() as write_cursor:
            write_cursor.execute(
                'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?);',
                (CCXT_PROFILES_SETTINGS_KEY, json.dumps(updated, sort_keys=True)),
            )

        return {'result': validated, 'message': '', 'status_code': HTTPStatus.OK}

    def delete_profile(self, name: str) -> dict[str, Any]:
        current = self.get_profiles()['result']['profiles']
        updated = [entry for entry in current if entry.get('name') != name]
        if len(updated) == len(current):
            return {
                'result': None,
                'message': f'No CCXT profile named {name} exists',
                'status_code': HTTPStatus.NOT_FOUND,
            }

        with self.rotkehlchen.data.db.user_write() as write_cursor:
            write_cursor.execute(
                'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?);',
                (CCXT_PROFILES_SETTINGS_KEY, json.dumps(updated, sort_keys=True)),
            )

        return {'result': True, 'message': '', 'status_code': HTTPStatus.OK}
