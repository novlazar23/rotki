from typing import TYPE_CHECKING

from rotkehlchen.assets.asset import AssetWithOracles
from rotkehlchen.errors.misc import RemoteError
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.exchanges.bybit import Bybit
from rotkehlchen.fval import FVal
from rotkehlchen.types import ApiKey, ApiSecret, Location

if TYPE_CHECKING:
    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.user_messages import MessagesAggregator


class Bybiteu(Bybit):
    def __init__(
            self,
            name: str,
            api_key: ApiKey,
            secret: ApiSecret,
            database: 'DBHandler',
            msg_aggregator: 'MessagesAggregator',
    ):
        super().__init__(
            name=name,
            api_key=api_key,
            secret=secret,
            database=database,
            msg_aggregator=msg_aggregator,
            location=Location.BYBITEU,
            uri='https://api.bybit.eu/v5',
        )
        self._ensure_location_in_db()
        self.is_unified_account = True

    def _ensure_location_in_db(self) -> None:
        """Existing user DBs don't automatically get new Location enum rows."""
        with self.db.user_write() as write_cursor:
            write_cursor.execute(
                'INSERT OR IGNORE INTO location(location, seq) VALUES(?, ?);',
                (Location.BYBITEU.serialize_for_db(), Location.BYBITEU.value),
            )

    def first_connection(self) -> None:
        """Bybit EU is UNIFIED-only; avoid user/query-api."""
        self.is_unified_account = True
        self.first_connection_made = True

    def validate_api_key(self) -> tuple[bool, str]:
        """Validate Bybit EU via the UNIFIED asset-balance endpoint.

        account/wallet-balance and user/query-api can return Bybit.eu server
        errors even when the key itself is accepted.
        """
        try:
            self._api_query(
                path='asset/transfer/query-account-coins-balance',
                options={'accountType': 'UNIFIED'},
            )
        except RemoteError as e:
            return False, str(e)

        return True, ''

    def _query_account_balances(self) -> tuple[dict[AssetWithOracles, FVal], str | None]:
        """Query Bybit EU balances from the UNIFIED account endpoint."""
        response, error = self._query_balances_or_error(
            path='asset/transfer/query-account-coins-balance',
            options={'accountType': 'UNIFIED'},
        )
        if error is not None:
            return {}, error

        try:
            return self._process_wallet_balances(response.get('balance', [])), None
        except DeserializationError as e:
            return {}, str(e)

    def _query_funding_balances(self) -> tuple[dict[AssetWithOracles, FVal], str | None]:
        """Bybit EU rejects FUND; all balances are queried through UNIFIED."""
        return {}, None
