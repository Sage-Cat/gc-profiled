# gc-profiled

A small Linux daemon that runs your garbage-collection scripts on a schedule.
Each TOML file is one named profile: command, working directory, interval, and
timeout. Python 3.11+ standard library only. MIT licensed.

Cleanup policy stays in your scripts. The daemon does not decide what to delete.

## Install

```sh
make check
python3 scripts/install.py --enable
```

This installs a copy under `~/.local/lib/gc-profiled`, a `~/.local/bin/gc-profiled`
command, and `gc-profiled.service` in your systemd user manager. Ensure
`~/.local/bin` is on your PATH. It starts automatically with the user manager;
continuing after logout depends on your existing systemd lingering configuration.
Installation creates **no enabled cleanup profiles**. Re-run the installer to
upgrade the installed copy and restart the daemon.

## Profiles

Create `~/.config/gc-profiled/profiles/build-cache.toml`:

```toml
description = "Prune old build artifacts"
enabled = true
command = ["/absolute/path/to/cleanup.sh", "--older-than", "14d"]
cwd = "/absolute/path/to/build-directory"
every = "1d"
timeout = "15m"

[env]
GC_KEEP_DAYS = "14"
```

The filename is the profile name. Use letters, digits, dots, underscores, or
dashes; begin with a letter or digit. `command`, `cwd`, and `every` are required.
`enabled` defaults to true, `timeout` to 15 minutes, and `env` to an empty table.
`description` is optional. Unknown fields are rejected to catch spelling errors.
`command` is an argument array, executed without an implicit shell. The executable
and working directory must be absolute paths; `~/` is supported. Arguments are
literal: to use a Python script, specify `["/usr/bin/python3", "/path/to/script.py"]`.
Shell scripts need an executable bit and shebang, or an explicit shell command.

Durations accept seconds as a number or a single unit: `30s`, `15m`, `6h`, `1d`,
`1w`. Scheduling is interval-based, **not a wall-clock/cron calendar**:

- A new enabled profile runs immediately.
- The next run is its interval after the previous run finishes, including failure.
- After downtime, an overdue profile runs once; missed intervals are not replayed.
- Changes are picked up on the next scheduler tick (normally two seconds).
- Up to four different profiles may run concurrently. One profile never overlaps
  itself, including manual requests. When busy, due profiles wait for a slot.
- Setting `enabled = false` prevents future runs; an existing run finishes.
- Removing a profile removes its status after an existing run finishes. Logs stay.
- Scripts run in the foreground as the daemon's OS user. On timeout or shutdown,
  their process group receives TERM and then KILL. Background child processes are
  cleaned up when a script exits; detached jobs are not supported.

An invalid profile is shown as failed without preventing other valid profiles
from running. Missing executables/directories produce an execution failure.

## Commands

```sh
gc-profiled validate             # parse profiles without running anything
gc-profiled list                 # daemon health and current profile results
gc-profiled status               # full JSON status
gc-profiled run build-cache      # queue an enabled profile now
gc-profiled logs build-cache     # last 100 lines of its current log
gc-profiled logs @daemon
systemctl --user status gc-profiled
journalctl --user -u gc-profiled
```

`run` requires a live daemon; it acknowledges queuing, not cleanup completion.
Requests for a running or disabled profile are skipped and logged. To disable a
profile, edit its `enabled` field. To stop all scheduling:

```sh
systemctl --user disable --now gc-profiled.service
```

For isolated testing or an independently managed system service:

```sh
python3 gc_profiled.py --config-dir /path/to/profiles --state-dir /path/to/state daemon
```

The daemon has no privilege switching. A system service can select an OS user
and explicit directories; its scripts have that user's permissions. The provided
installer uses your regular account, which is also what the HUD integration reads.

## Logging and status

Private state is stored under `~/.local/state/gc-profiled/`:

| File | Purpose |
|---|---|
| `status.json` | Atomic snapshot of daemon heartbeat and every profile |
| `logs/daemon.jsonl` | Scheduler starts, completions, and errors |
| `logs/profile-NAME.jsonl` | Timestamped run boundaries and merged stdout/stderr |
| `daemon.lock` | Single-instance lock |
| `requests/` | Pending manual requests |

Each JSON-lines log rotates at approximately 1 MiB with three backups. Output is
streamed in bounded chunks, including scripts that produce no newlines. Logs and
state are private to the daemon user; script output may contain sensitive data,
so keep it out of repositories. Log disk usage is bounded per profile; logs of
removed profiles are retained until you remove them explicitly.

Status records last start, finish, success, failure, next scheduled run, and a
short result message. Restart preserves these timestamps. An interrupted run is
reported as failed rather than successful. Exit code zero indicates success;
your script must return nonzero when cleanup fails.

`XDG_CONFIG_HOME` and `XDG_STATE_HOME` override the defaults. The installer pins
the directories selected at installation into the service command.

## HUD integration

The companion [Login HUD](https://github.com/Sage-Cat/login-hud) can read
`$XDG_STATE_HOME/gc-profiled/status.json` (default above) and show a third **GC**
tab. Each profile has a result icon and timestamps. Success is green, failure
red, running/pending/disabled neutral. A stopped or stale daemon is explicitly
reported; old successful rows are not evidence that scheduling is still alive.
The HUD only reads status and never starts cleanup scripts.

Status schema version 1:

```json
{
  "schema_version": 1,
  "updated_at": "2026-01-01T12:00:00+00:00",
  "daemon": {"state": "running", "pid": 1234},
  "profiles": [{
    "name": "build-cache", "enabled": true, "state": "ok",
    "last_started_at": "2026-01-01T11:59:00+00:00",
    "last_finished_at": "2026-01-01T12:00:00+00:00",
    "last_success_at": "2026-01-01T12:00:00+00:00",
    "last_failure_at": null,
    "next_run_at": "2026-01-02T12:00:00+00:00",
    "message": "Cleanup completed"
  }]
}
```

`updated_at` is refreshed normally every two seconds. Readers should treat a
heartbeat older than 30 seconds as unavailable. Profile states are `pending`,
`running`, `ok`, `failed`, and `disabled`; daemon states are `running` and `stopped`.

## Development

Run `make check`. Tests execute harmless scripts in temporary directories.
Keep real profiles, logs, secrets, and machine-specific scripts outside Git;
ignored `local/`, `private/`, and `profiles/` directories are available for local
work. Public examples live in `examples/` and are disabled by default.
