---
name: trade-republic
description: Converts a Trade Republic transaction export (CSV) into a Wealthfolio activity import CSV, and shows a redacted summary. Use when the user drops a Trade Republic export into the inbox, or asks to import Trade Republic transactions into Wealthfolio.
compatibility: Needs uv, and FINANCE_DIR or a start in the data directory. Runs only in Pi with the user's local model.
---

# Trade Republic to Wealthfolio

The script reads the export and prints only a summary. The summary has counts per type
and month, totals, unmapped rows and possible duplicates. You never see a row.

## Privacy rules

These rules hold for every step:

- Run this skill only in Pi with the user's local model. If the session uses a hosted
  model, stop and tell the user.
- Never open, read, `cat`, `head` or `grep` an export or an output CSV. Use only the
  script below.
- Never repeat names, descriptions, counterparties or account numbers in chat, even if
  one appears in an error.
- Never write into Wealthfolio or its database. The user imports the CSV through
  Wealthfolio's import screen.
- Write into `$FINANCE_DIR/out/trade-republic/` only after the user agrees.

## Files

- `$FINANCE_DIR/inbox/trade-republic/`: exports from the app (Profile > Account
  statements > Transaction export).
- `$FINANCE_DIR/out/trade-republic/`: the Wealthfolio CSV,
  `<export
  name>.wealthfolio.csv`, and `ledger.json`, the transaction IDs of earlier
  written runs.
- `$FINANCE_DIR/archive/trade-republic/`: exports and CSVs that the user imported. The
  user moves them there.
- `$FINANCE_DIR/config.local.toml`: the Wealthfolio account name for each Trade Republic
  account type. Copy `config.example.toml` from this skill to create it.

## Steps

Run the commands from the data directory, the one that holds `config.local.toml`.
`<skill>` is the path of this skill's directory. `<export>` is the export's path in
`inbox/trade-republic/`.

1. For a new export, or after an error, show the file's form:

   ```console
   uv run <skill>/scripts/trade_republic.py inspect <export>
   ```

   It lists the columns, the codes in code columns, value shapes such as `-9,9`, and the
   signs of amounts, fees and taxes per type. Show it to the user.

2. Convert without writing:

   ```console
   uv run <skill>/scripts/trade_republic.py convert <export>
   ```

3. Show the summary to the user. Point out unmapped rows, possible duplicates, and the
   traded ISINs. Their prices are in the account currency, not the listing currency. A
   `BUY_CANCELLED` line means that the cancelled BUY is still in the CSV. Do not guess a
   mapping for an unmapped row. The `trade amounts` line tells whether the export's
   trade amounts held the fees and taxes. The script handles both. A `STOCKPERK` row
   without its fee-free BUY stays unmapped: ask for an export that covers both. A
   `taxed income rows` line means the gross-income reading is not confirmed yet. Ask the
   user to compare one such payout with the app before they write.

4. Ask the user whether to write the CSV. When they agree, run:

   ```console
   uv run <skill>/scripts/trade_republic.py convert <export> --write
   ```

5. Tell the user the CSV path. In Wealthfolio, they open Activities > Import, pick the
   file and confirm the column mapping. Wealthfolio marks rows imported before as
   duplicates, because each row's comment holds its Trade Republic ID (`tr:<id>`).

A rerun writes only rows whose ID is not in the ledger yet. The summary counts the rows
it found in the ledger. It writes nothing when no row is new. If the user wrote a CSV
but never imported it, add `--all` to include those rows again. Wealthfolio still flags
the ones it already has.

## When the script fails

- `Missing columns`: the export has another header, for example localized names. Show
  the message and the `inspect` output. Do not edit the export.
- `No config`: help the user create `config.local.toml` from `config.example.toml`.
- `Set FINANCE_DIR`: run from the data directory, or ask the user to set the variable.
- `... exists`: an earlier CSV of this export is still in `out/trade-republic/`. Ask the
  user to import it, then to move it and its export to `archive/trade-republic/`. Do not
  move or delete it yourself.
