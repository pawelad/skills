# Finance skills

Rules for working on the skills in `skills/finance/`. Each skill's `SKILL.md` repeats
the rules that must hold at run time, because an install copies only the skill's
directory.

## Privacy

- Real exports and outputs live in `$FINANCE_DIR`, outside the repository. It holds
  `inbox/`, `out/`, `archive/` and `config.local.toml`. Never put it in a cloud-synced
  folder.
- Each source has its own folder in `inbox/`, `out/` and `archive/`, named like its
  skill. An export is named by its export date: `inbox/trade-republic/2026-05-01.csv`.
- Personal mappings, such as Wealthfolio account names, go in
  `$FINANCE_DIR/config.local.toml`. Each skill commits a `config.example.toml`.
- Finance skills run only in Pi with the user's local model. Hosted models, Claude
  included, see only synthetic or redacted samples.
- Never read a file in `$FINANCE_DIR`. To learn a real file's form, ask the user to run
  the skill's `inspect` command and paste its output.
- The repository is public. No real amounts, account numbers, holdings or names in code,
  docs, tests or commit messages.
- Fixtures are hand-written. Use placeholder IDs (`00000000-0000-4000-8000-…`), fake
  ISINs (`US00EXAMPLE2`) and `Example …` names. The `private-identifiers` hk step
  rejects IBAN- and card-number-like strings in `tests/`.

## Scripts

- A script prints only aggregates: counts, totals, codes, value shapes and reasons. It
  never prints a row, a name, a description or an account number.
- A script writes into its `$FINANCE_DIR/out/<source>/` only with an explicit flag, such
  as `--commit`.
- A script never writes into another app's data, with one exception. It may add
  Wealthfolio accounts and activities through Wealthfolio's MCP server, with the user's
  scoped token, and only with `--commit`. It never edits or deletes there, and never
  opens an app's database or files.
- A script reads a token only from the command in `config.local.toml`. It never prints
  the token, and never follows a redirect with it.
- A row the script cannot map is listed with its reason, never guessed.

## Layout

- When a second source imports into Wealthfolio, extract the shared part into a
  `wealthfolio-import` skill. `trade-republic/scripts/wealthfolio_mcp.py` is its start.

## Done

- [ ] Every new row type or format has a synthetic fixture and a test.
- [ ] `SKILL.md` states each run-time privacy rule the change relies on.
