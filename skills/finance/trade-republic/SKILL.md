---
name: trade-republic
description: Imports a Trade Republic transaction export (CSV) into Wealthfolio through its MCP server, after a redacted summary and Wealthfolio's own preview. Use when the user drops a Trade Republic export into the inbox, or asks to import Trade Republic transactions into Wealthfolio.
compatibility: Needs uv, a Wealthfolio MCP token in config.local.toml, and FINANCE_DIR or a start in the data directory. Runs only in Pi with the user's local model.
---

# Trade Republic to Wealthfolio

The script reads the export, sends the new rows to Wealthfolio's MCP server and prints
only a summary. The summary has counts per type and month, totals, unmapped rows,
possible duplicates and Wealthfolio's verdict. You never see a row or the token.

## Privacy rules

These rules hold for every step:

- Run this skill only in Pi with the user's local model. If the session uses a hosted
  model, stop and tell the user.
- Never open, read, `cat`, `head` or `grep` an export or the ledger. Use only the script
  below.
- Never repeat names, descriptions, counterparties or account numbers in chat, even if
  one appears in an error.
- Never call Wealthfolio yourself: no MCP tool, `curl` or other request, and no access
  to its database or files. Only the script talks to Wealthfolio.
- Never run the `token_command` from `config.local.toml`, and never read, print or store
  the token. If the command fails, ask the user to run it outside Pi.
- Pass `--commit` only after the user agrees to that run's preview.
- Never change or delete data in Wealthfolio. The script only adds activities and the
  configured account. The user edits or deletes in the Wealthfolio app.

## Files

- `$FINANCE_DIR/inbox/trade-republic/`: exports from the app (Profile > Account
  statements > Transaction export).
- `$FINANCE_DIR/out/trade-republic/ledger.json`: the transaction IDs that Wealthfolio
  holds from earlier imports.
- `$FINANCE_DIR/archive/trade-republic/`: exports that the user imported. The user moves
  them there.
- `$FINANCE_DIR/config.local.toml`: the Wealthfolio account name for each Trade Republic
  account type, the MCP URL and the token command. Copy `config.example.toml` from this
  skill to create it.

## Steps

Run the commands from the data directory, the one that holds `config.local.toml`.
`<skill>` is the path of this skill's directory. `<export>` is the export's path in
`inbox/trade-republic/`.

1. On the first run, or after a connection error, test the setup:

   ```console
   uv run <skill>/scripts/trade_republic.py check
   ```

   It lists the tools that the token reaches and whether each configured account exists.
   Show it to the user.

2. For a new export, or after an error, show the file's form:

   ```console
   uv run <skill>/scripts/trade_republic.py inspect <export>
   ```

   It lists the columns, the codes in code columns, value shapes such as `-9,9`, and the
   signs of amounts, fees and taxes per type. Show it to the user.

3. Preview the import. This run changes nothing in Wealthfolio:

   ```console
   uv run <skill>/scripts/trade_republic.py import <export>
   ```

4. Show the summary to the user. Point out:
   - Unmapped rows and possible duplicates. Do not guess a mapping for an unmapped row.
   - The traded ISINs. Their prices are in the account currency, not the listing
     currency.
   - The `Wealthfolio preview` line: invalid rows, warnings and rows that Wealthfolio
     already holds. The commit skips those.
   - A `missing` account. `--commit` creates it as Securities, Transactions mode, in the
     rows' currency. Its cost basis is FIFO, Wealthfolio's default.
   - A `BUY_CANCELLED` line: the cancelled BUY is in the run. After the import, the user
     deletes it in Wealthfolio.
   - A `taxed income rows` line: the gross-income reading is not confirmed yet. Ask the
     user to compare one such payout with the app before they commit.
   - A `STOCKPERK` row without its fee-free BUY stays unmapped. Ask for an export that
     covers both.

   The `trade amounts` line tells whether the export's trade amounts held the fees and
   taxes. The script handles both.

5. Ask the user whether to import. When they agree, run:

   ```console
   uv run <skill>/scripts/trade_republic.py import <export> --commit
   ```

   It previews again, and imports nothing if Wealthfolio finds a row invalid. Then it
   imports in batches, and writes the ledger after each batch.

6. Show the `commit:` line. Wealthfolio saves a row as a draft when it cannot set the
   row's cash amount. Tell the user to review and approve each draft in Wealthfolio.
   Then the user moves the export to `archive/trade-republic/`.

A rerun sends only rows whose ID is not in the ledger yet. It sends nothing when no row
is new. Each row's comment holds its Trade Republic ID (`tr:<id>`), so Wealthfolio also
skips a row that it holds. If the user deleted an import in Wealthfolio, add `--all` to
send the ledger's rows again.

## When the script fails

- `Missing columns`: the export has another header, for example localized names. Show
  the message and the `inspect` output. Do not edit the export.
- `No config`, or `needs [wealthfolio] ...`: help the user fill in `config.local.toml`
  from `config.example.toml`.
- `Set FINANCE_DIR`: run from the data directory, or ask the user to set the variable.
- `... from the CSV version of this skill`: ask the user to move that ledger to
  `archive/trade-republic/`. Do not move or delete it yourself.
- `Token command failed` or `HTTP 401`: ask the user to check the token outside Pi. Do
  not run the token command yourself.
- `The token cannot use ...`: the token lacks a scope. The message names it. The user
  adds it in Wealthfolio.
- `batch N of M failed`: Wealthfolio holds the earlier batches, and the ledger lists
  them. Show the report. After the user fixes the cause, rerun with `--commit`.
  Wealthfolio skips any row of the failed batch that it stored.
- `Nothing imported: Wealthfolio finds N rows invalid`: show the `invalid rows` list. Do
  not change the export or guess a fix.
