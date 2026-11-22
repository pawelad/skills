# Setup

## Fresh checkout

Install [mise](https://mise.jdx.dev/), then run:

```console
$ mise trust
$ mise run setup
```

`mise run setup` installs the pinned tools and the git hooks, then runs
`mise run install` for the project dependencies. Run it again at any time.

The shared tooling (tasks, hooks, worktrees, upgrades) is described in
[conventions.md](./conventions.md).

## Project tasks

`mise tasks` lists every task. This section describes the tasks that need more than
their one-line description.
