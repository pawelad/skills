# Conventions

This project uses the [cubert](https://github.com/pawelad/cubert) template. This file
belongs to the template: change it in cubert, not here. Project setup lives in
[setup.md](./setup.md).

## File ownership

| Kind           | Files                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | On `copier update`            |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| Template-owned | `.config/` except `wt.toml`, `.editorconfig`, `.shellcheckrc`, `.dclintrc.yaml`, `.github/actions/setup-mise/`, `.github/workflows/cubert-check.yml`, `.github/workflows/cubert-audit.yml`, `.github/workflows/pr-title.yml`, `.github/workflows/opentofu-state-backup.yml`, `.github/zizmor.yml`, `.github/CODEOWNERS`, `docs/conventions.md`, `openspec/schemas/`                                                                                                       | Takes the template's changes  |
| Merged         | `AGENTS.md`, `README.md`, `docs/README.md`, `.config/wt.toml`, `.gitignore`, `.git-blame-ignore-revs`, `.github/dependabot.yml`, `openspec/config.yaml`                                                                                                                                                                                                                                                                                                                   | Merges your additions         |
| Project-owned  | `mise.toml`, `hk.pkl`, `dprint.json`, `.yamllint`, `biome.json`, `mise-tasks/`, `.github/workflows/ci.yml`, `.github/workflows/audit.yml`, `.worktreeinclude`, `.worktreelinks`, `docs/setup.md`, `LICENSE`, `CONTRIBUTING.md`, `pyproject.toml`, `.python-version`, `cliff.toml`, `CHANGELOG.md`, `<opentofu_dir>/versions.tf`, `<opentofu_dir>/mise.toml`, `<opentofu_dir>/fnox.toml`, `<ansible_dir>/.gitkeep`, `openspec/changes/.gitkeep`, `openspec/specs/.gitkeep` | Created once, then left alone |

Copier treats template-owned and merged files the same way: a 3-way merge of the
template's change and your edit. The kinds differ in how you edit them. A change to a
template-owned file overlaps the template's own lines, so the next update conflicts with
it. Add to a merged file below the shared part, and the merge stays clean.

Each project-owned file layers on its template-owned file, so a project change goes
there:

| Tool     | Template-owned                       | Project-owned | How the project file layers               |
| -------- | ------------------------------------ | ------------- | ----------------------------------------- |
| mise     | `.config/mise/config.toml`, `tasks/` | `mise.toml`   | mise loads both; `mise.toml` wins         |
| hk       | `.config/hk/cubert.pkl`, `base.pkl`  | `hk.pkl`      | `hk.pkl` amends `cubert.pkl`              |
| dprint   | `.config/dprint.json`                | `dprint.json` | `dprint.json` extends it; excludes add up |
| Biome    | `.config/biome.json`                 | `biome.json`  | `biome.json` extends it (`typescript`)    |
| yamllint | `.config/yamllint.yaml`              | `.yamllint`   | `.yamllint` extends it; `ignore` replaces |

What an update does to a missing or removed file:

- A project-owned file that you delete comes back, with the new template's content.
- A template-owned or merged file that you delete stays deleted.
- A file that the new template drops is deleted, even if you edited it.

## Tools

- mise is the source of truth for tool versions, on every machine and in CI. CI installs
  tools with `mise install`, not with setup actions.
- `min_version` in `.config/mise/config.toml` pins mise itself. CI installs exactly that
  release. An older local mise stops and prints how to upgrade.
- A Python CLI that does not touch the project's code, such as yamllint or copier, is a
  mise `pypi:` tool, which mise installs with uv. Do not run `uvx` or `pipx` directly.
- A Python tool that only reads code, such as ruff, is a pinned mise tool. Most of these
  are Rust CLIs and need no Python environment.
- A tool that imports the code or loads plugins, such as pytest, pytest-xdist or mypy,
  goes in the uv `dev` group, and `uv.lock` pins it. It must see the project's Python
  and dependencies. This also holds in a repo that is not a package (`package = false`
  under `[tool.uv]`, or no `[build-system]`).
- mypy is the only type checker. ty is not offered: it is pre-1.0, and its 0.0.x
  releases make breaking changes. Recheck at ty 1.0. Pyrefly 1.0 is the other candidate.
- Python CLI tools select a private Python 3.14 runtime through their `python` option.
  mise installs it before the tools and limits their graphs to that Python line. The
  application's `.python-version` stays independent. This Python line is the one version
  cubert holds: CLI wheels lag a new line. A cubert release moves it in
  `.config/mise/required-tools`, and `setup` then updates each tool's option.
- A project with `npm:` tools pins `node` in `mise.toml` too.
- The `typescript` option seeds `bun`, `biome` and `npm:typescript` (the `tsc`
  compiler). Bun runs the tests and installs packages. The project does not list
  `typescript` in `package.json`.
- A GitHub Action is pinned to a commit SHA, with its version in a comment:
  `uses: "actions/checkout@<sha>" # v7.0.1`. A tag can move, and a SHA cannot.
  Dependabot updates both. zizmor enforces the pin and audits workflows for injection
  and credential leaks. jactionlint checks workflow syntax, with its `correctness`
  profile, so the two tools never report the same finding.
- An action or reusable workflow in the same repository uses `$/`, such as
  `uses: "$/.github/actions/setup-mise"`. Unlike `./`, it always loads the running
  commit, never a file that an earlier step wrote.
- mise calls the GitHub API to resolve and lock tools. GitHub limits calls without a
  token. With `gh` logged in, run
  `mise settings set github.credential_command "gh auth token"` once per machine. mise
  ignores this setting in a project's config. `audit:actions` and `audit:links` use the
  `gh` token too.
- A new project's `mise.toml` seeds `latest` (`lts` for Node). The first
  `mise run setup` pins each one to the newest release that `minimum_release_age`
  allows, so cubert holds no tool versions except that Python line. To add a tool, add
  `<tool> = "latest"` to `[tools]` and run `mise run setup`. `mise use --pin <tool>` can
  pick an installed release that the cutoff hides, then fail to lock it.
- The `pins` check requires each request to match its version in `mise.lock`. A dotted
  prefix such as `node = "24.22"` fails when it locks `24.22.2`.
- Commit `.mise/locks/` with `mise.lock`. The `lockfile` check verifies native npm and
  Python dependency graphs and their digests, even when tools are already installed.
  `check:repo` also verifies them after edits that change only a graph.

## Tasks

`mise tasks` lists every task. Names follow `<area>:<verb>`, where the area is the noun
that the task acts on (`kif:deploy`, `opentofu:init`). A wildcard then selects one area:
`mise run 'kif:*'`.

Four tasks run a group: `check`, `test`, `audit` and `clean`. A task that one of them
runs takes the verb first, with one area after it: `check:opentofu`, `test:backend`,
`clean:worktree`. Every other task keeps `<area>:<verb>`. A deeper name, such as
`check:tools:sync`, is not run.

The shared tasks are file tasks in `.config/mise/tasks/`. mise names each one from its
path, so `check/repo` is `check:repo`:

| Task                 | Does                                                                     |
| -------------------- | ------------------------------------------------------------------------ |
| `setup`              | Adds and pins tools, installs them and the git hooks, runs `install`     |
| `install`            | Nothing. The project replaces it with its dependency installs            |
| `check`              | Runs every `check:*` task                                                |
| `check:repo`         | Runs hk on every file, plus the repository guards                        |
| `check:opentofu`     | OpenTofu option: runs `tofu validate`, with no backend or credentials    |
| `opentofu:lock`      | OpenTofu option: locks the providers for macOS and Linux (`--upgrade`)   |
| `opentofu:init`      | OpenTofu option: initializes the providers and the state backend         |
| `opentofu:plan`      | OpenTofu option: shows the plan. Extra flags, such as `-out`, go to tofu |
| `opentofu:apply`     | OpenTofu option: applies a saved plan or the current configuration       |
| `ci:check`           | Runs `install`, `check`, then `test`: the CI check job                   |
| `audit`              | Runs every `audit:*` task. Needs the network, so `check` skips it        |
| `audit:actions`      | Runs zizmor's online audits, such as actions with known vulnerabilities  |
| `audit:deps`         | Scans `uv.lock`, `bun.lock` and script locks for known vulnerabilities   |
| `audit:links`        | Checks every link in the Markdown files, URLs included                   |
| `test`               | Runs every `test:*` task. Rejects file paths                             |
| `test:python`        | Python option: runs `uv run pytest` on the given files, or all tests     |
| `test:typescript`    | TypeScript option: runs each directory's test script, or `bun test`      |
| `typescript:install` | TypeScript option: runs `bun install` in each directory                  |
| `release`            | Changelog option: changelog, commit and tag, then push after you confirm |
| `release:preview`    | Changelog option: prints the entries since the last release tag          |
| `format`             | Runs the hk fixers on changed files (`--all` for every file)             |
| `clean`              | Runs every `clean:*` task, `clean:worktree` among them                   |
| `clean:worktree`     | Empties the worktrunk trash                                              |
| `upgrade`            | Bumps the tools in `mise.toml` (`mise upgrade --bump`)                   |
| `hooks:install`      | Installs the hk hooks and the `post-checkout` worktree hook              |
| `template:update`    | Updates the template-owned and merged files from cubert                  |
| `template:check`     | Fails if a newer cubert version exists                                   |
| `worktree:setup`     | Sets up a new worktree once (see below)                                  |

To add a project check, add a `check:<area>` task. `check` runs it with no other change.
`clean:<area>` works the same way. To add a test suite, add a `test:<area>` task. `test`
runs it, and so does CI. Let it take test file paths (`#USAGE arg "[files]..."`), so a
run can cover only the touched files.

The `opentofu` option adds `check:opentofu` and the `opentofu:` tasks. Each one also
answers to `tofu:`, the CLI's name, such as `tofu:init`. `opentofu:init`,
`opentofu:plan` and `opentofu:apply` run from any directory. They load
`<opentofu_dir>/mise.toml`, and read the credentials as
[Operator credentials](#operator-credentials) describes. The root `mise.toml` never
loads them, so other tasks and hooks do not see them. Extra arguments go to `tofu`, and
paths are relative to the OpenTofu directory. Name a saved plan `*.tofuplan`, which
`.gitignore` covers:

```console
$ mise run tofu:plan -out=reviewed.tofuplan
$ mise run tofu:apply reviewed.tofuplan
```

`setup` and worktree setup never run `opentofu:init`, because it contacts the state
backend and needs the operator's login. With `secrets_provider: proton-pass`, a
`[post-start]` hook in `.config/wt.toml` runs it in a new worktree. The hook skips when
`pass-cli info` finds no session. It sets `FNOX_NON_INTERACTIVE=1`, so it never waits on
a login prompt. Put a project's own `post-start` hooks in that table, because a second
`[post-start]` header is a TOML error. `check:opentofu` validates with
`init -backend=false` in its own data directory (`.terraform-validate`), so it needs no
credentials and leaves the real `.terraform` alone. Run `mise run opentofu:init` once
per checkout before a plan or apply.

`check:opentofu` never changes `.terraform.lock.hcl`, and it fails when the lock file
lacks a provider. `mise run opentofu:lock` then adds the hashes for macOS and Linux,
with no backend login. After a version constraint change, run
`mise run opentofu:lock --upgrade`. `tofu init` would lock only the local platform, and
CI would change the file. The OpenTofu tasks create the configured provider cache before
invoking OpenTofu. A project that replaces `opentofu:init` must create
`TF_PLUGIN_CACHE_DIR` too. `.config/mise/config.toml` sets `TF_PLUGIN_CACHE_DIR`, so
each worktree reuses the providers that another one downloaded.

OpenTofu hooks check formatting. Validation runs through `check:opentofu` during `check`
and CI. Keep a project hook if validation must also run before commits or pushes.

The Ansible hooks check `playbooks/`, `roles/` and `collections/` under `<ansible_dir>`.
Put playbooks in `<ansible_dir>/playbooks/`: the hooks skip a `site.yml` at the root.
Ansible syntax checks discover nested playbooks. They exclude directories named `tasks`,
`handlers`, `vars`, `group_vars`, `host_vars`, `roles`, `templates` and `files`. Those
directories hold task fragments, variables and assets rather than playbook entrypoints.
Keep playbook entrypoints outside them.

The `ansible-template` hook parses changed `.j2` files with a custom ansible-lint rule.
It uses the existing Jinja installation, Ansible's configured extensions and `#jinja2:`
delimiter overrides. It does not render values or resolve filters, so syntax checks need
no runtime variables. A passing parse does not prove that a deployment will succeed.

The `python` option adds `test:python`. It runs `uv run pytest` and treats pytest's exit
code 5 (no tests collected) as a pass, so a fresh project passes `mise run test`. A
project without pytest in its dev group skips with a message. Add it with
`uv add --dev pytest`. A full run uses pytest-xdist (`-n auto`) when the dev group has
it. A run with test files runs in one process.

The `typescript` option asks for `typescript_dirs`: the directories that hold a
`package.json`, `["."]` by default. A monorepo lists each one. Paths stay inside the
project and use ASCII letters, numbers, periods, underscores, hyphens and slashes. Write
a path as `apps/web`, with no `./`, `..`, trailing or double slash, or leading hyphen.
Internal names such as `apps/-x` are accepted. Copier rejects other names before
rendering, because task file arguments split on spaces. `typescript:install` and
`test:typescript` loop over them and skip a directory without `package.json`. Dependabot
gets the same list. `test:typescript` runs the `test` script of a `package.json` that
has one, such as Jest or Vitest. Otherwise it runs `bun test`, which passes when it
finds no tests. Given file paths, it runs only the directories that hold them. Redundant
`/` separators and `.` components are normalized in selected file paths. A selection
outside every configured package fails with a message. A test file that imports
`bun:test` needs `bun add --dev @types/bun` for `tsc`.

The `changelog` option adds git-cliff, `cliff.toml`, `CHANGELOG.md`, `release` and
`release:preview`. `release X.Y.Z` runs from a clean `main` that matches `origin/main`.
It needs `gh` with a login and a passing CI run on that commit. `--no-push` skips both
and keeps the commit and tag local. The first release has no earlier version to compare
with, so its section reads "Initial release." Each later section lists the commits since
the previous tag. The seeded `ci.yml` skips a push that changes only `CHANGELOG.md`,
such as the release commit: its parent already passed CI.

A project adds release steps with two optional tasks in `mise.toml`. Each task declares
its argument, and reads it from the matching `usage_` variable:

- `release:bump` (`arg "<version>"`, `$usage_version`) runs before the changelog, with
  the version without the `v`. It edits the version files. The release commit takes
  every tracked file that it changed. An untracked file stops the release.
- `release:publish` (`arg "<tag>"`, `$usage_tag`) runs after the push, with the tag,
  such as `v1.2.0`. It replaces the GitHub Release that `release` creates by default.
  Define it when a tag workflow deploys and publishes, and point it at that run.

Each entry links its pull request when the subject ends in `(#N)`, as a GitHub squash
merge writes it. Otherwise it links the commit. `cliff.toml` holds the repository URL
from the `github_owner` and `project_name` answers.

git-cliff pipes its output through dprint, so the changelog keeps the 88-column limit.
Keep the `cliff.toml` header as dprint wraps it. `--prepend` keeps one header only when
the file starts with the exact header text.

`areas` in `cliff.toml` lists the scopes that users see, such as `map` or `spots`, in
display order. Each changelog group then lists its entries by area, and the rest under
Other. With areas, the `changelog-scope` commit hook and the PR title job reject a
`feat`, `fix`, `perf` or breaking commit without an area scope or a tooling scope. A
breaking commit has a `!` or a `BREAKING CHANGE:` footer, of any type. The tooling
scopes are the ones that the `commit_parsers` skip. An empty list, the default, checks
nothing.

### Operator credentials

The `secrets_provider` answer sets where the OpenTofu operator credentials live:

| Answer        | Store                                         | Loaded by                     |
| ------------- | --------------------------------------------- | ----------------------------- |
| `proton-pass` | The Proton Pass vault `secrets_vault`         | `fnox exec`, from `fnox.toml` |
| `age`         | Ciphertext in `fnox.toml`, for the age key    | `fnox exec`, from `fnox.toml` |
| `none`        | A gitignored `.env` in the OpenTofu directory | `_.file` in its `mise.toml`   |

With fnox, `<opentofu_dir>/fnox.toml` lists every value that the tasks need, with a
description. `fnox list` shows them. No `.env.example` exists. The file sets:

- `if_missing = "error"`: a missing value stops the task and names it.
- `env = "exec"`: `fnox activate` never exports the values into a shell.
- `redact_exec = true`: piped output shows `[REDACTED]` for each value, so an agent that
  reads a plan does not see it. A terminal shows the output unchanged.
- `[daemon]`: a per-user daemon caches the values for one idle hour. Run
  `fnox daemon clear` after you rotate a value.

When `<opentofu_dir>/fnox.toml` exists, the tasks wrap only the commands that need
credentials in `fnox exec --`, never a whole script. mise `[env]` never loads them. A
command whose output is saved or compared passes `--no-redact`, because a mask would
change it. `tofu` runs with `-input=false`, so a variable without a value fails with its
name and does not wait on a prompt.

Proton Pass holds one vault per project, and one custom item per service. A field name
is the variable name without `TF_VAR_` and without the service prefix:
`TF_VAR_cloudflare_api_token` is the `api_token` field of the `Cloudflare` item. Secrets
use hidden fields. Other values, such as an account ID, use text fields. OpenTofu's own
values, such as the state passphrase, go in the `OpenTofu` item. Log in once per machine
with `pass-cli login`.

With age, `fnox set <NAME>` encrypts a value into `fnox.toml` for the
`secrets_age_recipient` key. Decryption reads the identity in `~/.config/fnox/age.txt`.
To give another operator access, add their public key to `recipients`, then run
`fnox reencrypt`.

mise installs fnox and, for Proton Pass, `pass-cli`. `required-tools` requires fnox
1.50.0 or later, because 1.48.0 fails to launch on macOS 15. Commit the `mise.lock`
entries that `mise run setup` writes for both.

The tasks have one body for every answer, so a project can switch later. The update
brings the tools, rules and worktree hook:

```console
$ mise run template:update -d secrets_provider=proton-pass -d secrets_vault=<vault>
```

`<opentofu_dir>/mise.toml` and `fnox.toml` are project-owned, so create, edit or delete
them by hand.

### OpenTofu state backup

The `opentofu_state_backup` option keeps an offsite copy of the encrypted state. The
state lives in the R2 bucket `opentofu_state_bucket`, as `terraform.tfstate`. The
`opentofu-state-backup.yml` workflow copies it to the B2 bucket `<bucket>-backups`. It
runs on Mondays at 04:00 UTC, and `opentofu:apply` dispatches it after a state change.
The workflow reads ciphertext only and never gets the state passphrase.

With this option, `opentofu:apply` runs these steps:

1. Reads the ETag of the raw state object, without decrypting it.
2. Saves the encrypted state to `tmp/opentofu-state/`. It stops if the state is not
   encrypted, because the backup would then leak it.
3. Runs `tofu apply` with your arguments, such as a saved plan.
4. Dispatches the workflow with `gh` when the ETag changed. A failed dispatch only
   warns, because the apply succeeded. The weekly run copies the state too.

The task reads the state credentials from `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
and `AWS_ENDPOINT_URL_S3`, as the S3 backend does. The seeded `fnox.toml` lists them.
With Proton Pass, they are fields of the `Cloudflare` item: `state_access_key_id`,
`state_secret_access_key` and `state_endpoint_url`. The dispatch needs a `gh` login.

The project's OpenTofu code creates the rest, because providers and accounts differ:

- The B2 bucket `<bucket>-backups`, with a compliance file lock. A key for it without
  `deleteFiles`, so the workflow can hide versions but never purge them.
- A Cloudflare token with read access to the state bucket only.
- A Healthchecks.io check with the schedule `0 4 * * 1` in UTC, and a grace period.
- The `infrastructure` GitHub Actions environment, limited to `main`, with these
  secrets: `HEALTHCHECKS_PING_URL`, `RCLONE_CONFIG_B2_ACCOUNT`, `RCLONE_CONFIG_B2_KEY`,
  `RCLONE_CONFIG_R2_ACCESS_KEY_ID`, `RCLONE_CONFIG_R2_SECRET_ACCESS_KEY` and
  `RCLONE_CONFIG_R2_ENDPOINT`.

GitHub disables scheduled workflows in a public repository after 60 days without
activity. The Healthchecks.io check then reports the missed run. Enable the workflow
again with `gh workflow enable opentofu-state-backup.yml`.

### Project tasks

- A short task is a TOML task in `mise.toml`.
- A task with logic, or with more than one line, is a file task in `mise-tasks/`. hk
  then runs shellcheck and shfmt on it. Header lines declare its metadata:
  `#MISE description="..."` and `#USAGE arg "<name>"`.
- File tasks run in the project root, from any directory.
- mise passes `#USAGE` args only as `usage_*` environment variables, not as `$@`.

### Overriding a shared task

A TOML task in `mise.toml` merges with the file task of the same name. Each field that
the TOML task sets wins, and the rest come from the file. Give an override its own
`run`: an override with only `depends` still runs the shared body after them. Replace
`install` this way, or `setup` if the project needs more steps. A file task in
`mise-tasks/` does not replace a shared task: the `.config/mise/tasks/` file wins.

mise runs only one `postinstall` hook, and the hook in `.config/mise/config.toml` wins.
Put project install steps in the `install` task.

### Scripts

Pick the lightest tier that fits:

1. **Bash**: a short sequence of CLI calls with no data processing.
2. **Standalone Python** (`uv run --script`, PEP 723 inline dependencies): parsing, API
   calls or real branching.

With the `python` option, the `mypy` hk step checks each PEP 723 script in its own uv
environment, which holds only the inline dependencies. An import that the block misses
then fails the check, as it would for the person who runs the script. mypy checks the
script for the oldest Python that its `requires-python` allows, or mypy's own oldest
target when that is newer (3.10 for mypy 2.x). The project's `[tool.mypy]` settings
still apply.

- The inline block holds only the libraries that the script imports at runtime. Tools
  such as pytest and mypy come from the project's dev group.
- The one exception is stub packages, such as `types-requests`. The mypy step finds
  stubs only in the script's environment, so add them to the inline block.
- Test a script the way its users run it: `uv run <script>` in a subprocess.
- The first check of a script needs the network to install its dependencies.
- For the project's 7-day cooldown, add `exclude-newer = "7 days"` under `[tool.uv]` in
  the block.
- To pin what users install, run `uv lock --script <script>` and commit `<script>.lock`.
  `uv run` then installs from it, and `audit:deps` scans it. The check fails while the
  lock is stale.
- ruff formats for the project's `requires-python`. For a script that allows an older
  Python, add it to `per-file-target-version` under `[tool.ruff]`, such as
  `"skills/**/scripts/*.py" = "py311"`. Otherwise mypy rejects ruff's newer syntax.

`scripts/` holds only code that is not a task: check scripts that hk calls with
`{{files}}`, modules with tests, and git hook shims.

## Git hooks

hk runs the git hooks (`pre-commit`, `pre-push`, `commit-msg`). The shared steps are in
`.config/hk/base.pkl` (every project) and `.config/hk/cubert.pkl` (the steps for the
enabled features). `hk.pkl` amends `cubert.pkl`:

- Change a shared step: `steps { ["dprint"] { exclude = List("gen/**") } }`.
- Turn a shared step off: `skip_steps = List("file-length")`.
- Add a step: `steps { ["name"] = new Step { ... } }`.
- Import the hk builtins under a name of your own:
  `import "package://.../Builtins.pkl" as hkb`. hk's evaluator cannot resolve a second
  import named `Builtins`, and `base.pkl` already has one.
- hk 2.11.1 rejects a `Group` in `steps`. Give each step its own `dir` instead.

In Cubert, change the hk package pins in `template/.config/hk/base.pkl` and
`template/.config/hk/cubert.pkl.jinja` together. Projects receive those files as
`.config/hk/base.pkl` and `.config/hk/cubert.pkl`.

The prose rules (American English, no em dashes, no emojis) check Markdown and text
files in full. In code and config files they check comment lines only, and a fixer
rewrites only a comment. A string, an identifier or a config value can name an external
API or hold a fixture, so the rules leave it alone. Paths named `fixtures`, `testdata`,
`__snapshots__`, `generated` and `vendor` are skipped, by the smart-quotes fixer too.
Skip another path with
`steps { ["no-british-english"] { exclude = List("docs/legacy/**") } }`.

The `openspec` step runs `openspec validate --all --strict`. With `spec-driven`, an
`[INFO]` or `[WARNING]` line fails it too, such as a requirement over 500 characters.
`changes-only` changes always print an `[INFO]` line, so only the exit code counts
there.

Three hk steps cover what the formatters and linters miss:

- `typos` checks spelling in text files, and the prose rules keep American English. It
  only reports, with no fixer, so a false positive is never rewritten. A project adds
  words in a root `typos.toml`, which layers on `.config/typos.toml`. A line that ends
  in a `spellchecker:disable-line` comment is skipped.
- `betterleaks` scans for tokens, passwords and keys. A false positive gets a
  `betterleaks:allow` comment on the line, or a fingerprint in `.betterleaksignore`.
- `lychee` runs with `--offline`, so it checks relative links in Markdown only. A
  project excludes paths or URLs in a root `lychee.toml`, which layers on
  `.config/lychee.toml`.

`check:repo` fails when a local `core.hooksPath` is set. Git then ignores the global
config-based hooks that `hk install --global` writes, with no error. Fix it with
`git config --local --unset core.hooksPath`.

## CI

The template owns the shared jobs, and the project owns every trigger. Triggers decide
how many CI minutes a project spends, so each project sets its own.

| Workflow           | Owner    | Does                                                         |
| ------------------ | -------- | ------------------------------------------------------------ |
| `cubert-check.yml` | Template | The `Check` job, `mise run ci:check`. Runs only when called  |
| `cubert-audit.yml` | Template | The `Audit` job, `mise run audit`. Runs only when called     |
| `ci.yml`           | Project  | Calls `cubert-check.yml` on pushes to main and pull requests |
| `audit.yml`        | Project  | Calls `cubert-audit.yml` on the same events and weekly       |
| `pr-title.yml`     | Template | Checks the pull request title                                |

- Add project jobs to `ci.yml` or to another workflow. Path filters, draft rules,
  concurrency and a `workflow_call` trigger for a deploy gate go there too.
- The weekly schedule is in `audit.yml`, so it does not start the CI jobs.
- A caller sets `permissions`. A called workflow can only narrow them.
- `ci:check` runs `install`, `check`, then `test`. When other jobs run the slow suites
  with their services, replace it in `mise.toml`:
  `[tasks."ci:check"] run = "mise run check:repo"`. Include `mise run install` in an
  override when its checks need project dependencies. `mise run ci:check` runs the same
  job locally.
- `ci:check` sets `UV_LOCKED=1`, so a stale `uv.lock` fails CI. Bun installs with
  `--frozen-lockfile` everywhere. An override sets `UV_LOCKED=1` itself.

## Audits

`mise run audit` runs the checks that need the network: osv-scanner on `uv.lock`,
`bun.lock` and script locks, zizmor's online audits, and lychee on every URL. The zizmor
hk step runs offline, so only the audit finds actions with known vulnerabilities,
impostor commits and version comments that do not match the pinned SHA. Hooks and
`mise run check` skip them, so they stay fast and work offline. A new advisory then
fails the audit and never an unrelated commit. CI runs it from `audit.yml`, on each push
and pull request and once a week. Run it locally before a release or after a dependency
change. Every project gets osv-scanner, because the Python tools also have `uv.lock`
files in `.mise/locks/`. The scan includes those files and application lockfiles,
including nested backends.

## Formatting

dprint formats Markdown, TOML, JSON and YAML. yamllint requires double-quoted strings.
With the `typescript` option, Biome formats and lints JavaScript, TypeScript and CSS. It
does not format JSON, so dprint stays the only JSON formatter. The `biome` step covers
format, lint and import order, so there is no separate format step. hk runs `tsc` once
per nearest `tsconfig.json`, so a monorepo needs no setup. `mise run format` skips
`tsc`, which cannot fix anything. The hk builtins check trailing whitespace, final
newlines and line endings from `.editorconfig`.

Every file type has the same maximum line length: 88 columns, the Black and ruff
default. dprint wraps Markdown prose and formats the other files to it, and yamllint
fails a longer YAML line. Workflows and actions are exempt from yamllint's limit,
because an action pin keeps its version comment on the same line. ruff uses its default,
and `.editorconfig` sets `max_line_length` for editors. Set a new formatter to 88 too.

A commit that only reformats code goes in `.git-blame-ignore-revs`. GitHub reads that
file, and `mise run setup` sets `blame.ignoreRevsFile` for local blame.

## Agent skills

Each person picks the agents that get skills, so no task installs them. The agent dirs
are gitignored. With the OpenSpec option, install its skills once in each clone, and
update them after an OpenSpec bump:

```console
$ openspec init --tools claude,pi,antigravity
$ openspec update
```

A new worktree links the agent dirs from the primary checkout, so it needs neither step.
`openspec init` writes skills from the pinned CLI. Before you add another skill
installer, check what it installs: `impeccable update` takes the latest skills, not the
version that `mise.toml` pins.

In a repository that publishes skills, `npx skills add ./ --list` also lists the skills
in the agent dirs, such as OpenSpec's. A clone has no agent dirs, so
`npx skills add <owner>/<repo>` offers only the published skills. To test locally, point
the CLI at the skills dir: `npx skills add ./skills`. A `skills.sh.json` cannot help:
its `paths` add to the standard locations and do not replace them (skills 1.9.0).

## Worktrees

Every way to create a worktree runs `mise run worktree:setup`:

- `wt switch --create` runs it from the `[pre-start]` hook in `.config/wt.toml`. That
  file is merged, so a project adds its commit guidance at the end of `template-append`.
  Worktrunk reads that guidance from no other file.
- Plain `git worktree add`, and agent tools that use it (Superset, Jean, herdr, Orca),
  fire the `post-checkout` hook, which runs it.

`worktree:setup` exits in the primary checkout. In a new worktree it:

1. Links `tmp/` and the agent dirs (`.claude`, `.codex`, ...) to the primary checkout,
   so agent output outlives the worktree.
2. Links each path in `.worktreelinks` to the primary checkout. Use it for operator
   credentials, which are the same on every branch (`<opentofu_dir>/.env` with
   `secrets_provider: none`, and `<ansible_dir>/.vault_pass`).
3. Copies the gitignored files listed in `.worktreeinclude` (default `.env`).
4. Runs `mise run install`.
5. Writes the marker `cubert-worktree-setup-done` in the worktree's git dir.

Notes:

- The marker makes setup run once. The `post-checkout` all-zero ref does not work: Orca
  pre-warms detached worktrees and finalizes them later with a plain `git checkout`.
- The hook is a script in the hooks dir, not a config-based hook. GUI apps on macOS run
  Apple Git, which has no config-based hooks.
- An existing project hook stays in place. `hooks:install` writes `post-checkout.cubert`
  and prints the delegation command to add. `check:repo` accepts that command, with
  optional `exec`, when the delegated copy is executable and current.
- A real directory blocks its link. Move its contents to the primary checkout, then run
  `mise run worktree:setup --force`.

## Upgrades

- `mise run upgrade` bumps the tools in `mise.toml`.
- Dependabot bumps GitHub Actions and the package ecosystems monthly. It has no
  ecosystem for `mise.toml`.
- mise skips releases younger than 7 days (`minimum_release_age`), which matches the
  Dependabot cooldown. Installs from `mise.lock` and exact pins are not filtered.
- `typescript:install` passes `--minimum-release-age=604800` (7 days) to `bun install`.
  Bun reads `bunfig.toml` only from the working directory, so a flag covers every
  directory. Add a `bunfig.toml` with `[install] minimumReleaseAge = 604800` to a
  directory where you also run `bun add` or `bun update`.
- The dprint plugin versions belong to the template. cubert bumps them.

## Template updates

`mise run template:update` runs `copier update` from the latest cubert tag. It needs a
clean tree, keeps your earlier answers, and stops with a list of files that have
conflict markers. `-d <question>=<answer>` changes an answer, and you can repeat it.
Resolve them, run `mise run setup` (the update can change tools and hooks) and
`mise run check`, then commit the result as one commit.

An update cannot add a tool to the project-owned `mise.toml`.
`.config/mise/required-tools` lists the tools that the shared steps and tasks call.
`mise run setup` adds each missing one to `mise.toml`, pinned. The `required-tools` hk
step fails until it does. A line such as `hk>=2.11.1` sets a minimum version: an older
hk cannot load the shared config, so setup raises the pin to the minimum and the step
names the tool when it is too old. Other pins above the minimum stay as they are. A line
such as `node@lts` adds a missing tool from that channel, as a new project seeds it. A
selector such as `yamllint[python=3.14]` also repairs the tool's private runtime without
changing its configured version. `setup` preserves other inline tool options.

Run Copier only through this task. Copier commits in temporary repos, and global hk
hooks there drop `mise.lock` from the update. The task runs Copier with `HK=0`.
