# gc-profiled

Run your cleanup scripts on a schedule and report their results.
Python 3.11+ on Linux; no third-party Python dependencies.
TOML profiles define commands, intervals and timeouts; cleanup policy stays in your
scripts. Up to four profiles run together without overlapping themselves.

![Results from harmless example profiles](docs/screenshots/profiles.png)

Temporary daemon with harmless profiles; [capture details](docs/usage.md#screenshots-and-diagram-maintenance).

## Setup and use

```sh
make check  # Compilation, scheduler/process tests and whitespace checks
python3 scripts/install.py --enable
gc-profiled validate
gc-profiled list
gc-profiled status
gc-profiled logs build-cache
```

The installer enables a systemd user service with no enabled cleanup profiles;
re-run it to update the installed copy. Copy the disabled [example](examples/build-cache.toml) to
`~/.config/gc-profiled/profiles/`, set your command and working directory, and
validate before enabling. A newly enabled profile runs immediately.

```sh
# Stop scheduling and terminate active jobs.
systemctl --user disable --now gc-profiled.service
```

## Documentation

- [Safe first run and troubleshooting](docs/usage.md) · [Profile and status reference](docs/reference.md)
- [Architecture](docs/architecture.md) · [PlantUML source](docs/architecture.puml)
- [Releases](https://github.com/Sage-Cat/gc-profiled/releases) · [Publication process](https://github.com/Sage-Cat/workspace-state/blob/main/docs/publication.md)
- [Full desktop lifecycle validation](https://github.com/Sage-Cat/desktop-workspace/blob/main/docs/validation.md) documents integration tests and their limits, not VM coverage of every cleanup policy.
