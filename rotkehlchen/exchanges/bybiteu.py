from typing import TYPE_CHECKING, Any

from rotkehlchen.assets.asset import AssetWithOracles
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
        super().first_connection()
        self.is_unified_account = True
        self.first_connection_made = True

    def _query_funding_balances(self) -> tuple[dict[AssetWithOracles, FVal], str | None]:
        """Bybit EU is UNIFIED-only; the original FUND balance query fails there."""
        return {}, None

    def _query_balances_or_error(
            self,
            path: Any,
            options: dict[str, str],
    ) -> tuple[dict[str, Any], str | None]:
        if path == 'account/wallet-balance':
            options = {**options, 'accountType': 'UNIFIED'}

        return super()._query_balances_or_error(path=path, options=options)
