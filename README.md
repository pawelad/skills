# skills

Agent Skills for Pi, Codex, Claude Code and Antigravity.

## Skills

| Skill                              | Category  | Does                                                 |
| ---------------------------------- | --------- | ---------------------------------------------------- |
| [`trade-republic`][trade-republic] | `finance` | Trade Republic transaction export to Wealthfolio CSV |

Each skill lives in `skills/<category>/<name>/`, with a `README.md` for users.

## Install

Install a skill into the current project with the [`skills`][skills-cli] CLI. Pin a
release tag and name the agents:

```console
$ npx skills add pawelad/skills@<tag> --skill <name> -a pi -a codex -a claude-code -a antigravity
```

The install copies only the skill's directory, so each skill is self-contained.

The repository also works as a [Pi package][pi-packages]: Pi finds every skill under
`skills/`, the finance skills too. `-l` installs it into the project, like the command
above:

```console
$ pi install -l git:github.com/pawelad/skills@<tag>
```

Finance skills run only in Pi with a local model, on data kept outside the repository.
Install them in the data directory, for Pi alone, so no hosted agent loads them:

```console
$ npx skills add pawelad/skills@<tag> --skill trade-republic -a pi
```

The [skill's README][trade-republic] covers the data directory and the Pi command.

## Development

[`mise`][mise] runs every task. Set up a fresh checkout:

```console
$ mise trust
$ mise run setup
```

`mise tasks` lists everything else. See the [setup guide] for details.

## Documentation

- [`docs/`][docs]: how the project works and why

## Authors

Developed and maintained by [Paweł Adamczak][author].

Released under the [Mozilla Public License 2.0][license].

[author]: https://pawelad.me/
[docs]: ./docs/README.md
[license]: ./LICENSE
[mise]: https://mise.jdx.dev/
[pi-packages]: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/packages.md
[skills-cli]: https://github.com/vercel-labs/skills
[trade-republic]: ./skills/finance/trade-republic/README.md
[setup guide]: ./docs/setup.md
