import os
from collections import defaultdict
from collections.abc import Iterator
from typing import TYPE_CHECKING

from rotkehlchen.assets.asset import AssetWithOracles
from rotkehlchen.constants.misc import ZERO
from rotkehlchen.errors.misc import RemoteError
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.exchanges.bybit import Bybit
from rotkehlchen.fval import FVal
from rotkehlchen.types import ApiKey, ApiSecret, Location

if TYPE_CHECKING:
    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.user_messages import MessagesAggregator


BYBITEU_DEFAULT_BALANCE_COINS = (
    'USDT',
    'USDC',
    'BTC',
    'ETH',
    'SOL',
    'XRP',
    'POL',
    'HYPE',
    'CSPR',
    'GRAM',
    'CHIP',
    'MMT',
    'RESOLV',
    'ROOT',
    'ARB',
    'DOGE',
    'GALA',
    'HBAR',
    'KAS',
    'LINK',
    'NEAR',
    'SHIB',
    'ALGO',
)


def chunked_coins(coins: tuple[str, ...], size: int = 10) -> Iterator[tuple[str, ...]]:
    for idx in range(0, len(coins), size):
        yield coins[idx:idx + size]


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

    @staticmethod
    def _configured_balance_coins() -> tuple[str, ...]:
        """Allow extending the coin list from docker-compose without code changes.

        Example:
        ROTKI_BYBITEU_BALANCE_COINS=USDT,USDC,BTC,ETH,ROOT,RESOLV
        """
        configured = os.environ.get('ROTKI_BYBITEU_BALANCE_COINS', '')
        extra_coins = tuple(
            coin.strip().upper()
            for coin in configured.split(',')
            if coin.strip() != ''
        )
        return tuple(dict.fromkeys(BYBITEU_DEFAULT_BALANCE_COINS + extra_coins))

    def first_connection(self) -> None:
        """Bybit EU is UNIFIED-only; avoid user/query-api."""
        self.is_unified_account = True
        self.first_connection_made = True

    def validate_api_key(self) -> tuple[bool, str]:
        """Validate Bybit EU through one explicit UNIFIED coin query."""
        try:
            self._api_query(
                path='asset/transfer/query-account-coins-balance',
                options={
                    'accountType': 'UNIFIED',
                    'coin': 'USDT',
                },
            )
        except RemoteError as e:
            return False, str(e)

        return True, ''

    def _query_account_balances(self) -> tuple[dict[AssetWithOracles, FVal], str | None]:
        """Query Bybit EU balances in UNIFIED coin chunks.

        Bybit EU requires 1-10 coins per request for accountType=UNIFIED.
        """
        amounts: defaultdict[AssetWithOracles, FVal] = defaultdict(FVal)
        errors: list[str] = []

        for coins in chunked_coins(self._configured_balance_coins(), size=10):
            response, error = self._query_balances_or_error(
                path='asset/transfer/query-account-coins-balance',
                options={
                    'accountType': 'UNIFIED',
                    'coin': ','.join(coins),
                },
            )
            if error is not None:
                errors.append(f'{",".join(coins)}: {error}')
                continue

            try:
                chunk_amounts = self._process_wallet_balances(response.get('balance', []))
            except DeserializationError as e:
                errors.append(f'{",".join(coins)}: {e!s}')
                continue

            for asset, amount in chunk_amounts.items():
                if amount != ZERO:
                    amounts[asset] += amount

        if len(amounts) == 0 and len(errors) != 0:
            return {}, '; '.join(errors)

        return dict(amounts), None

    def _query_funding_balances(self) -> tuple[dict[AssetWithOracles, FVal], str | None]:
        """Bybit EU rejects FUND; all balances are queried through UNIFIED."""
        return {}, None
