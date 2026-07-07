import os
from collections import defaultdict
from collections.abc import Iterable, Iterator
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


BYBITEU_EXACT_BALANCE_COINS_ENV = 'ROTKI_BYBITEU_BALANCE_COINS'
BYBITEU_EXTRA_BALANCE_COINS_ENV = 'ROTKI_BYBITEU_EXTRA_BALANCE_COINS'
BYBITEU_DISCOVERY_QUOTES_ENV = 'ROTKI_BYBITEU_DISCOVERY_QUOTES'
BYBITEU_DISCOVERY_CATEGORIES_ENV = 'ROTKI_BYBITEU_DISCOVERY_CATEGORIES'
BYBITEU_MAX_DISCOVERED_COINS_ENV = 'ROTKI_BYBITEU_MAX_DISCOVERED_COINS'

BYBITEU_FALLBACK_BALANCE_COINS = (
    'USDT',
    'USDC',
    'BTC',
    'ETH',
    'SOL',
    'XRP',
)

BYBITEU_DEFAULT_DISCOVERY_QUOTES = (
    'USDT',
    'USDC',
    'EUR',
    'BTC',
    'ETH',
)

BYBITEU_DEFAULT_DISCOVERY_CATEGORIES = (
    'spot',
    'linear',
)

BYBITEU_SOFT_SERVER_ERROR_CODES = (
    "'retCode': 10016",
    "'retCode': 131200",
    "'retCode': 141002",
)

BYBITEU_AUTH_ERROR_CODES = (
    "'retCode': 10003",
    "'retCode': 10004",
    "'retCode': 10005",
    "'retCode': 10007",
    "'retCode': 33004",
)


def _split_env_csv(name: str) -> tuple[str, ...]:
    return tuple(
        value.strip().upper()
        for value in os.environ.get(name, '').split(',')
        if value.strip() != ''
    )


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    normalized = (
        value.strip().upper()
        for value in values
        if value.strip() != ''
    )
    return tuple(dict.fromkeys(normalized))


def _chunked(values: tuple[str, ...], size: int = 10) -> Iterator[tuple[str, ...]]:
    for idx in range(0, len(values), size):
        yield values[idx:idx + size]


def _coin_from_market_symbol(symbol: str, quote_assets: tuple[str, ...]) -> tuple[str, str] | None:
    upper_symbol = symbol.upper()
    for quote_asset in sorted(quote_assets, key=len, reverse=True):
        if upper_symbol.endswith(quote_asset) is False:
            continue

        base_asset = upper_symbol[:-len(quote_asset)]
        if base_asset == '':
            continue

        return base_asset, quote_asset

    return None


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

    @staticmethod
    def _configured_exact_balance_coins() -> tuple[str, ...]:
        """Exact override. If set, only these coins are queried."""
        return _split_env_csv(BYBITEU_EXACT_BALANCE_COINS_ENV)

    @staticmethod
    def _configured_extra_balance_coins() -> tuple[str, ...]:
        """Extra coins appended to autodiscovery/fallback candidates."""
        return _split_env_csv(BYBITEU_EXTRA_BALANCE_COINS_ENV)

    @staticmethod
    def _configured_discovery_quotes() -> tuple[str, ...]:
        configured = _split_env_csv(BYBITEU_DISCOVERY_QUOTES_ENV)
        return configured if len(configured) != 0 else BYBITEU_DEFAULT_DISCOVERY_QUOTES

    @staticmethod
    def _configured_discovery_categories() -> tuple[str, ...]:
        configured = _split_env_csv(BYBITEU_DISCOVERY_CATEGORIES_ENV)
        return tuple(value.lower() for value in configured) if len(configured) != 0 else BYBITEU_DEFAULT_DISCOVERY_CATEGORIES

    @staticmethod
    def _max_discovered_coins() -> int:
        raw_value = os.environ.get(BYBITEU_MAX_DISCOVERED_COINS_ENV, '250')
        try:
            return max(1, int(raw_value))
        except ValueError:
            return 250

    @staticmethod
    def _is_soft_bybiteu_server_error(error: str) -> bool:
        return any(code in error for code in BYBITEU_SOFT_SERVER_ERROR_CODES)

    @staticmethod
    def _is_auth_error(error: str) -> bool:
        return any(code in error for code in BYBITEU_AUTH_ERROR_CODES)

    def _discover_market_coins(self) -> tuple[str, ...]:
        """Discover balance coin candidates from public Bybit.eu market symbols.

        This is best-effort. Earn-only assets may not have markets, so
        environment extras remain supported.
        """
        coins: list[str] = []
        quote_assets = self._configured_discovery_quotes()
        max_coins = self._max_discovered_coins()

        for category in self._configured_discovery_categories():
            try:
                response = self._api_query(
                    path='market/tickers',
                    options={'category': category},
                )
            except RemoteError:
                continue

            for entry in response.get('list', []):
                symbol = entry.get('symbol')
                if not isinstance(symbol, str):
                    continue

                pair = _coin_from_market_symbol(symbol=symbol, quote_assets=quote_assets)
                if pair is None:
                    continue

                base_asset, quote_asset = pair
                coins.extend((base_asset, quote_asset))
                if len(set(coins)) >= max_coins:
                    return _deduplicate(coins)

        return _deduplicate(coins)

    def _balance_coin_candidates(self) -> tuple[str, ...]:
        """Return the flexible native Bybit.eu balance candidate list.

        Priority:
        1. ROTKI_BYBITEU_BALANCE_COINS as exact override.
        2. Market autodiscovery.
        3. Safe fallback coins.
        4. ROTKI_BYBITEU_EXTRA_BALANCE_COINS appended.
        """
        exact_coins = self._configured_exact_balance_coins()
        if len(exact_coins) != 0:
            return exact_coins

        return _deduplicate((
            *self._discover_market_coins(),
            *BYBITEU_FALLBACK_BALANCE_COINS,
            *self._configured_extra_balance_coins(),
        ))

    def validate_api_key(self) -> tuple[bool, str]:
        """Validate Bybit EU without blocking on flaky private balance endpoints.

        Bybit.eu can return server-side errors for otherwise valid private
        balance endpoints. Clear auth/signature/permission errors still reject
        the credentials.
        """
        candidate_coins = _deduplicate((
            'USDT',
            'USDC',
            'BTC',
            'ETH',
            *self._configured_extra_balance_coins(),
            *self._configured_exact_balance_coins(),
        ))
        last_error = ''

        for coin in candidate_coins:
            try:
                self._api_query(
                    path='asset/transfer/query-account-coins-balance',
                    options={
                        'accountType': 'UNIFIED',
                        'coin': coin,
                    },
                )
            except RemoteError as e:
                error = str(e)
                last_error = error

                if self._is_auth_error(error):
                    return False, error

                if self._is_soft_bybiteu_server_error(error):
                    continue

                return False, error

            return True, ''

        if last_error != '' and self._is_soft_bybiteu_server_error(last_error):
            return True, ''

        return False, last_error or 'Could not validate Bybit EU API key'

    def _query_coin_balance_chunk(
            self,
            coins: tuple[str, ...],
    ) -> tuple[dict[AssetWithOracles, FVal], list[str]]:
        """Query one 1-10 coin chunk.

        If Bybit rejects a multi-coin chunk, split it down to individual coins so
        one unsupported coin does not block all other balances.
        """
        response, error = self._query_balances_or_error(
            path='asset/transfer/query-account-coins-balance',
            options={
                'accountType': 'UNIFIED',
                'coin': ','.join(coins),
            },
        )
        if error is None:
            try:
                return self._process_wallet_balances(response.get('balance', [])), []
            except DeserializationError as e:
                return {}, [f'{",".join(coins)}: {e!s}']

        if len(coins) == 1:
            if self._is_soft_bybiteu_server_error(error):
                return {}, []

            return {}, [f'{coins[0]}: {error}']

        amounts: defaultdict[AssetWithOracles, FVal] = defaultdict(FVal)
        errors: list[str] = []
        for coin in coins:
            single_amounts, single_errors = self._query_coin_balance_chunk((coin,))
            for asset, amount in single_amounts.items():
                amounts[asset] += amount
            errors.extend(single_errors)

        return dict(amounts), errors

    def _query_account_balances(self) -> tuple[dict[AssetWithOracles, FVal], str | None]:
        """Query Bybit EU balances flexibly in UNIFIED coin chunks."""
        amounts: defaultdict[AssetWithOracles, FVal] = defaultdict(FVal)
        errors: list[str] = []

        for coins in _chunked(self._balance_coin_candidates(), size=10):
            chunk_amounts, chunk_errors = self._query_coin_balance_chunk(coins)
            for asset, amount in chunk_amounts.items():
                if amount != ZERO:
                    amounts[asset] += amount
            errors.extend(chunk_errors)

        if len(amounts) == 0 and len(errors) != 0:
            return {}, '; '.join(errors)

        return dict(amounts), None

    def _query_funding_balances(self) -> tuple[dict[AssetWithOracles, FVal], str | None]:
        """Bybit EU rejects FUND; all balances are queried through UNIFIED."""
        return {}, None
