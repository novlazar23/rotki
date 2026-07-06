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

## Integration point

External balance importers should call the mapping before constructing rotki manual balances:

```python
from tools.ccxt_rotki_symbol_map import map_symbol

rotki_identifier = map_symbol(ccxt_symbol)
if rotki_identifier is None:
    # log or skip unmapped symbol
    ...
```

This keeps the importer deterministic and prevents silently skipping balances that are known but require explicit rotki identifiers.
