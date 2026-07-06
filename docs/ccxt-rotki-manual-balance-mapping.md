# CCXT balance symbols and rotki manual balances

This note documents the local mapping needed when importing CCXT or exchange-exported balances into rotki manual balances.

## Problem

Rotki history imports and exchange transaction imports do not automatically create current balances. Current balances come from live exchange queries, blockchain accounts, or manual balances.

If an external importer logs messages such as:

```text
Skipping balances without Rotki asset identifier mapping: ['CHIP', 'CSPR', 'GRAM', 'HYPE', 'MMT', 'POL', 'RESOLV', 'ROOT']
```

then the importer has current balances but cannot convert the exchange symbols into rotki asset identifiers.

## Mapping file

The helper file is:

```text
tools/ccxt_rotki_symbol_map.py
```

Run it from the repository root:

```bash
python tools/ccxt_rotki_symbol_map.py
```

Validate against the local rotki Asset resolver, when the rotki environment is configured:

```bash
python tools/ccxt_rotki_symbol_map.py --validate
```

Return JSON for use by another script:

```bash
python tools/ccxt_rotki_symbol_map.py --json
```

## Balance collector

The collector file is:

```text
tools/ccxt_balance_collector.py
```

It collects current balances via CCXT, applies the symbol mapping and writes a JSON file. It does not store secrets, mutate rotki databases or call the rotki API directly.

Install the optional CCXT dependency in your local Python environment:

```bash
python3 -m pip install ccxt
```

Create a local config from the example:

```bash
cp docs/examples/ccxt-balance-collector.config.example.json ccxt-balance-collector.config.json
```

Set API credentials through environment variables. Do not put keys into Git:

```bash
export BYBIT_API_KEY="..."
export BYBIT_API_SECRET="..."
```

Run the collector:

```bash
python3 tools/ccxt_balance_collector.py \
  --config ccxt-balance-collector.config.json \
  --output out/ccxt-balances.json \
  --fail-on-unmapped
```

The output JSON has this shape:

```json
{
  "balances": [
    {
      "exchange": "bybit-main",
      "account": "bybit-main",
      "symbol": "POL",
      "asset_identifier": "eip155:137/erc20:0x0000000000000000000000000000000000001010",
      "free": "1.23",
      "used": "0",
      "total": "1.23",
      "timestamp": "2026-07-06T00:00:00+00:00"
    }
  ],
  "summary": {
    "balances": 1,
    "skipped_zero_balances": 0,
    "unmapped_symbols": []
  }
}
```

## Initial mappings

| Exchange/CCXT symbol | rotki identifier |
| --- | --- |
| CHIP | `CHIP` |
| CSPR | `CSPR` |
| GRAM | `GRAM` |
| HYPE | `HYPE` |
| MMT | `MMT` |
| POL | `eip155:137/erc20:0x0000000000000000000000000000000000001010` |
| RESOLV | `RESOLV` |
| ROOT | `ROOT` |

## Important notes

`POL` must not be treated as old `MATIC`. In this rotki branch, Polygon PoS native POL is represented as:

```text
eip155:137/erc20:0x0000000000000000000000000000000000001010
```

`HYPE` is present as a rotki constant in this branch.

The identity mappings for `CHIP`, `CSPR`, `GRAM`, `MMT`, `RESOLV`, and `ROOT` still require the asset to exist in the local rotki global database. If `--validate` marks one of them as missing, add the asset in rotki first or update the global asset database before importing the balance.

## Drop-in integration for an external importer

The external importer should map every raw CCXT balance before it creates the rotki manual balance payload.

For a single row:

```python
from tools.ccxt_rotki_symbol_map import add_rotki_identifier_to_balance

mapped_balance = add_rotki_identifier_to_balance(
    balance,
    symbol_key="asset",              # use "symbol" or "currency" if your row uses that field
    identifier_key="asset_identifier",
)
if mapped_balance is None:
    # log and skip only genuinely unknown symbols
    ...
```

For a batch:

```python
from tools.ccxt_rotki_symbol_map import add_rotki_identifiers_to_balances

mapping_result = add_rotki_identifiers_to_balances(
    balances,
    symbol_key="asset",
    identifier_key="asset_identifier",
)

mapped_balances = mapping_result.mapped
skipped_balances = mapping_result.skipped
```

Strict mode is useful for CI or local debugging because it raises immediately on an unmapped symbol:

```python
mapping_result = add_rotki_identifiers_to_balances(
    balances,
    symbol_key="asset",
    identifier_key="asset_identifier",
    strict=True,
)
```

## Patch target in `portfolio_tracker.rotki.manual_balances`

The mapping must happen before the existing code filters or logs missing rotki identifiers. The intended shape is:

```python
from tools.ccxt_rotki_symbol_map import add_rotki_identifiers_to_balances

mapping_result = add_rotki_identifiers_to_balances(
    balances,
    symbol_key="asset",
    identifier_key="asset_identifier",
)

for skipped in mapping_result.skipped:
    logger.warning("Skipping balance without Rotki asset identifier mapping: %s", skipped)

balances = mapping_result.mapped
```

If your importer uses another field name, adjust `symbol_key`. Common values are `asset`, `symbol`, and `currency`.

This keeps the importer deterministic and prevents silently skipping balances that are known but require explicit rotki identifiers.
