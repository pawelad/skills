# Trade Republic to Wealthfolio

Converts a Trade Republic transaction export (CSV) into a Wealthfolio activity import
CSV. Pi runs it with your local model. The model sees only a redacted summary.

## Set up

1. Create a data directory outside the repository and outside cloud-synced folders:

   ```console
   $ mkdir -p ~/Data/Finance/{inbox,out,archive}/trade-republic
   ```

2. Install the skill there, for Pi only:

   ```console
   $ cd ~/Data/Finance
   $ npx skills add pawelad/skills@<tag> --skill trade-republic -a pi
   ```

3. Copy the example config, then set the name of your Wealthfolio account in it:

   ```console
   $ cp .agents/skills/trade-republic/config.example.toml config.local.toml
   ```

4. Start Pi in the data directory with your local model and only this skill:

   ```console
   $ pi --model <provider>/<model> --no-skills --skill .agents/skills/trade-republic \
       --no-extensions -e <provider extension, if any>
   ```

   `--no-extensions` keeps other extensions out of the session. For example, Pi's
   llama.cpp provider needs `-e builtin:llama.cpp`.

## Use

1. In the Trade Republic app, open Profile > Account statements > Transaction export.
   Save the CSV as `inbox/trade-republic/<export date>.csv`, for example
   `inbox/trade-republic/2026-05-01.csv`.
2. Ask Pi to import it, for example "Import inbox/trade-republic/2026-05-01.csv into
   Wealthfolio". Pi shows a summary and asks before it writes
   `out/trade-republic/2026-05-01.wealthfolio.csv`.
3. In Wealthfolio, open Activities > Import and pick that file.
4. After the import, move the export and the CSV to `archive/trade-republic/`. Keep the
   ledger, `out/trade-republic/ledger.json`, where it is.

Each trade carries the cash that Trade Republic booked, charges included. Wealthfolio
can mark a fractional purchase for review, because shares x price misses that total by
less than a cent. The booked total is correct.

A new export can overlap an earlier one. The skill writes only rows that are new since
the last written run, and Wealthfolio flags rows it already has.

## Privacy

- The script prints only counts, totals, codes and value shapes. It never prints a row.
- It writes only into `out/trade-republic/`, and only after you agree.
- Do not give an export or an output CSV to a hosted model.

## Limits

- Splits, saveback, gifts, bonds and crypto are listed as unmapped. Add them in
  Wealthfolio.
- A row with a positive fee or tax, likely a refund, is listed as unmapped.
- A stock perk is cash that buys a free share at once. It becomes a `CREDIT` with
  subtype `BONUS`, which Wealthfolio counts as new capital, like a deposit. The export
  must also hold the fee-free BUY that the perk paid for. Without it, the perk is
  unmapped.
- Trade Republic does not link a cancelled order to its purchase. The summary names each
  `BUY_CANCELLED` row. Remove that purchase from the CSV before you import it.
