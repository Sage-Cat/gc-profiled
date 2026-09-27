# Safe first run and troubleshooting

[Overview](../README.md) · [Architecture](architecture.md)

## Try scheduling without deleting files

Use Python 3.11+ on Linux. This walkthrough creates its own temporary profile and
state directories and only prints a message. It does not install a service or
read your normal profiles.

From the repository root, in a POSIX shell:

```sh
demo_dir=$(mktemp -d)
mkdir -p "$demo_dir/profiles"
cat > "$demo_dir/profiles/example.toml" <<'TOML'
description = "Harmless scheduler example"
enabled = true
command = ["/usr/bin/printf", "Example task completed\n"]
cwd = "/tmp"
every = "1h"
timeout = "5s"
TOML
python3 gc_profiled.py --config-dir "$demo_dir/profiles" \
  --state-dir "$demo_dir/state" validate
python3 gc_profiled.py --config-dir "$demo_dir/profiles" \
  --state-dir "$demo_dir/state" daemon
```

The daemon runs in the foreground. In another shell, assign `demo_dir` the exact
path created above, then inspect it:

```sh
python3 gc_profiled.py --config-dir "$demo_dir/profiles" \
  --state-dir "$demo_dir/state" list
python3 gc_profiled.py --config-dir "$demo_dir/profiles" \
  --state-dir "$demo_dir/state" logs example
```

The first run should finish with state `ok`. Use the same command prefix with
`status` to see `next_run_at`, approximately one hour after completion. Stop the foreground daemon with Ctrl+C. Its final status reports `stopped`.
Remove the temporary directory when you no longer need the demonstration output.

## Install and add your own policy

```sh
make check
python3 scripts/install.py --enable
gc-profiled validate
gc-profiled list
```

Installation enables the scheduler service but creates no enabled cleanup
profiles. Begin with the disabled [example profile](../examples/build-cache.toml).
Set your own executable and working directory, test that script independently,
then validate the profile before enabling it. Enabling a new profile makes it due
immediately; validation alone never executes its command.

The [profile reference](reference.md#profiles) documents all profile fields and scheduling
rules. To queue a configured enabled profile manually, use `gc-profiled run NAME`
and then inspect `gc-profiled list` or `gc-profiled status` for its final result.

## Troubleshooting

| Symptom | Check and action |
|---|---|
| Invalid profile | Run `gc-profiled validate`; fix the reported field. Unknown fields and relative executable/working-directory paths are rejected. |
| Queued but not completed | `run` only queues. Read status and profile logs; other jobs may occupy all four slots. |
| Script did not start | Check executable permissions, shebang, absolute executable path and existing working directory. |
| Exit failure or timeout | Read `gc-profiled logs NAME`. Test the command separately; change timeout only when its workload warrants it. |
| Old successful rows, daemon unavailable | Inspect `systemctl --user status gc-profiled` and `journalctl --user -u gc-profiled`. A stale heartbeat is not a live scheduler. |
| Second daemon cannot start | Another process owns that state directory. Use a separate state directory for a demonstration rather than sharing the live one. |
| Disabled profile still finishing | Disabling prevents future runs; an existing job finishes. Stopping the daemon terminates active process groups. |
| Updated checkout, old installed behavior | Re-run the installer to copy the updated runtime. Source checkout and installed service are separate. |

To stop scheduling through the installed user service:

```sh
systemctl --user disable --now gc-profiled.service
```

## Screenshots and diagram maintenance

The README screenshot shows actual CLI output from isolated harmless example
profiles. Recreate examples in temporary directories when updating it. Do not
publish real profile names, private paths, script output, or runtime log files.

The diagram is editable PlantUML with a committed SVG for GitHub rendering:

```sh
plantuml -tsvg docs/architecture.puml
```

Reproduce the CLI screenshot with `python3 docs/capture-cli.py` from the repository
root. This requires Chrome or Chromium for an isolated headless render. The script
starts and stops its own temporary daemon with harmless example profiles; the
installed service and normal profiles are not used.
