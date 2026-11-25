# Trade Republic to Wealthfolio

Imports a Trade Republic transaction export (CSV) into Wealthfolio through Wealthfolio's
MCP server. Pi runs it with your local model. The model sees only a redacted summary,
and never the token.

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

3. Turn on Wealthfolio's MCP server. Set `WF_MCP_ENABLED=true` on the server, then
   restart it. Wealthfolio's own login (`WF_AUTH_PASSWORD_HASH` or OIDC) must be on.

4. Create a token. In Wealthfolio, open Settings > AI agent access > Personal access
   tokens > Create token. Give it only these scopes:

   | Scope                     | Why                                                  |
   | ------------------------- | ---------------------------------------------------- |
   | Accounts (read)           | Finds the account by name                            |
   | Activities (read)         | Detects the activities that Wealthfolio already has  |
   | Activities - draft        | Previews the rows                                    |
   | Activities - commit/write | Imports the rows, with `--commit` only               |
   | Accounts - write          | Creates a missing account. Remove it after the first |

   Pick an expiry, such as 90 days. Wealthfolio shows the token once.

   The token can add activities, and the commit scope can also change and delete them.
   The script only adds. You can revoke the token, or edit its scopes, under the same
   settings page.

5. Store the token in Proton Pass. In the Proton Pass app, create a login item named
   `Wealthfolio MCP` in a vault named `Finance`, and paste the token into its password.
   Do not pass the token as a `pass-cli` argument: the shell history keeps it. Then log
   in once with `pass-cli login`.

6. Copy the example config, then set your values in it:

   ```console
   $ cp .agents/skills/trade-republic/config.example.toml config.local.toml
   ```

   | Key                           | Value                                                    |
   | ----------------------------- | -------------------------------------------------------- |
   | `[trade_republic.accounts]`   | Wealthfolio account name per Trade Republic account type |
   | `[wealthfolio] mcp_url`       | Your server's address with `/mcp`, over https            |
   | `[wealthfolio] token_command` | A command that prints only the token, as a list of words |

   The script runs `token_command` without a shell and keeps its output to itself. It
   does not follow redirects, so the token goes only to `mcp_url`.

7. Save a launcher as `~/.local/bin/pi-finance` and make it executable. This example
   uses Pi's llama.cpp provider:

   ```sh
   #!/bin/sh
   set -eu
   export FINANCE_DIR="${FINANCE_DIR:-$HOME/Data/Finance}"
   export PI_CODING_AGENT_DIR="$FINANCE_DIR/.pi-agent"
   cd "$FINANCE_DIR"
   exec pi --no-extensions -e builtin:llama.cpp --no-skills --skill .agents/skills "$@"
   ```

   - `FINANCE_DIR` tells the skill where the data directory is. Set it to use another
     one.
   - `PI_CODING_AGENT_DIR` gives Pi a separate config directory. Your global extensions,
     skills, credentials and sessions stay out of it.
   - Sessions hold your data, so they stay in the data directory.
   - `--no-extensions` keeps other extensions out, MCP ones too: only the skill's script
     talks to Wealthfolio. `-e` loads only the model provider.
   - `--skill .agents/skills` loads each finance skill installed in the data directory.

8. Run `pi-finance`, then `/login llama.cpp` to connect your server. Set the default
   model with `/model`.

9. Check the setup in a terminal:

   ```console
   $ uv run .agents/skills/trade-republic/scripts/trade_republic.py check
   token command: ok
   server: wealthfolio 3.13.1
   tools the token reaches:
     get_accounts: yes
     ...
   accounts in the config:
     DEFAULT: missing
   ```

   Each tool must say `yes`. `missing` is fine: the first import creates the account.
   When every account says `found`, remove Accounts - write from the token.

## Use

1. In the Trade Republic app, open Profile > Account statements > Transaction export.
   Save the CSV as `inbox/trade-republic/<export date>.csv`, for example
   `inbox/trade-republic/2026-05-01.csv`.
2. Ask Pi to import it, for example "Import inbox/trade-republic/2026-05-01.csv into
   Wealthfolio". Pi shows the summary and Wealthfolio's preview. Nothing changes in
   Wealthfolio yet.
3. When you agree, Pi imports with `--commit`. Wealthfolio's MCP server has no approval
   queue, so the preview is the review step. The imported activities count at once.
4. Wealthfolio saves a row as a draft only when it cannot set the row's cash amount.
   Approve each draft in Wealthfolio.
5. Move the export to `archive/trade-republic/`. Keep the ledger,
   `out/trade-republic/ledger.json`, where it is.

Each trade carries the cash that Trade Republic booked, charges included. Wealthfolio
can mark a fractional purchase for review, because shares x price misses that total by
less than a cent. The booked total is correct.

A new export can overlap an earlier one. The skill sends only rows that are not in the
ledger. Each row's comment holds its Trade Republic ID, and Wealthfolio skips a row that
it already has. A second run on the same export creates nothing.

To undo an import, delete its activities in Wealthfolio, then rerun with `--all`.

## Privacy

- The script prints only counts, totals, codes and value shapes. It never prints a row,
  an account name or the token. It hides quoted text and digits in Wealthfolio's
  messages.
- It writes into Wealthfolio, `out/trade-republic/ledger.json` included, only with
  `--commit`, after you agree. It only adds activities and the configured account.
- Do not give an export or the ledger to a hosted model.

## Limits

- Wealthfolio's MCP server cannot set an account's cost basis method. A new account gets
  FIFO, Wealthfolio's default. Change it in the app if you want another.
- Splits, saveback, gifts, bonds and crypto are listed as unmapped. Add them in
  Wealthfolio.
- A row with a positive fee or tax, likely a refund, is listed as unmapped.
- A stock perk is cash that buys a free share at once. It becomes a `CREDIT` with
  subtype `BONUS`, which Wealthfolio counts as new capital, like a deposit. The export
  must also hold the fee-free BUY that the perk paid for. Without it, the perk is
  unmapped.
- Trade Republic does not link a cancelled order to its purchase. The summary names each
  `BUY_CANCELLED` row. After the import, delete that purchase in Wealthfolio.
