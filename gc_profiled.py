#!/usr/bin/env python3
"""A small, local scheduler for externally owned garbage-collection scripts."""

from __future__ import annotations

import argparse
import codecs
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

VERSION = "0.1.0"
TICK = 2
MAX_PARALLEL = 4
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


def stamp(seconds=None):
    return datetime.fromtimestamp(time.time() if seconds is None else seconds,
                                  timezone.utc).isoformat(timespec="seconds")


def epoch(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def duration(value):
    """Accept seconds or one readable unit: 30s, 15m, 6h, 1d, 1w."""
    if isinstance(value, bool):
        raise ValueError("duration must be seconds or a number followed by s/m/h/d/w")
    if isinstance(value, (int, float)):
        try:
            result = float(value)
        except OverflowError:
            raise ValueError("duration is too large") from None
    elif isinstance(value, str) and (match := re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw])", value)):
        result = float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
    else:
        raise ValueError("duration must be seconds or a number followed by s/m/h/d/w")
    if not math.isfinite(result) or not 1 <= result <= 10 * 365 * 86400:
        raise ValueError("duration must be between 1 second and 10 years")
    return result


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic_json(path, data):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Profile:
    name: str
    command: list[str]
    cwd: str
    every: float
    timeout: float
    enabled: bool
    env: dict[str, str]


def read_profile(path):
    if not NAME.fullmatch(path.stem):
        raise ValueError("filename must be a simple profile name (letters, digits, dot, dash, underscore)")
    if path.stat().st_size > 65536:
        raise ValueError("profile exceeds 64 KiB")
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    unknown = set(data) - {"command", "cwd", "every", "timeout", "enabled", "env", "description"}
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    command = data.get("command")
    if (not isinstance(command, list) or not command or
            not all(isinstance(arg, str) and "\x00" not in arg for arg in command) or not command[0]):
        raise ValueError("command must be a nonempty array of strings")
    command = [os.path.expanduser(command[0]), *command[1:]]
    if not os.path.isabs(command[0]):
        raise ValueError("command[0] must be an absolute executable path (or start with ~/)")
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or "\x00" in cwd or not os.path.isabs(os.path.expanduser(cwd)):
        raise ValueError("cwd must be an absolute working directory (or start with ~/)")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    env = data.get("env", {})
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and k and "=" not in k and "\x00" not in k and
            isinstance(v, str) and "\x00" not in v for k, v in env.items()):
        raise ValueError("env must be a table of string values")
    return Profile(path.stem, command, os.path.expanduser(cwd), duration(data.get("every")),
                   duration(data.get("timeout", "15m")), enabled, env)


def load_profiles(directory):
    profiles, errors = {}, {}
    for path in sorted(directory.glob("*.toml")):
        try:
            profiles[path.stem] = read_profile(path)
        except (OSError, ValueError) as error:
            errors[path.stem] = str(error)
    return profiles, errors


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({"at": stamp(record.created), "level": record.levelname.lower(),
                           "message": record.getMessage()}, ensure_ascii=True)


def make_logger(path, console=False):
    logger = logging.Logger(str(path), logging.INFO)
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    os.chmod(path, 0o600)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    if console:
        stderr = logging.StreamHandler()
        stderr.setFormatter(JsonFormatter())
        logger.addHandler(stderr)
    return logger


def close_logger(logger):
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)


def new_row(name):
    return {"name": name, "enabled": True, "state": "pending", "message": "Waiting for first run",
            "last_started_at": None, "last_finished_at": None, "last_success_at": None,
            "last_failure_at": None, "next_run_at": None}


def capture_output(stream, logger):
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    try:
        while block := os.read(stream.fileno(), 4096):
            pending += decoder.decode(block)
            while "\n" in pending or len(pending) >= 8192:
                end = pending.find("\n")
                if end < 0 or end > 8192:
                    end = 8192
                    line, pending = pending[:end], pending[end:]
                else:
                    line, pending = pending[:end], pending[end + 1:]
                logger.info("output: %s", line)
        pending += decoder.decode(b"", final=True)
        if pending:
            logger.info("output: %s", pending)
    except OSError as error:
        logger.error("output read failed: %s", error)
    finally:
        stream.close()


@dataclass
class Job:
    process: subprocess.Popen
    deadline: float
    logger: logging.Logger
    reader: threading.Thread
    stopping_at: float | None = None
    failure: str | None = None


class Daemon:
    def __init__(self, config_dir, state_dir):
        self.config_dir, self.state_dir = config_dir, state_dir
        for directory in (config_dir, state_dir, state_dir / "logs", state_dir / "requests"):
            private_dir(directory)
        self.lock = (state_dir / "daemon.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("another gc-profiled daemon owns this state directory") from None
        self.logger = make_logger(state_dir / "logs" / "daemon.jsonl", console=True)
        self.rows, self.jobs, self.requested = {}, {}, set()
        self.stopping = False
        try:
            old = json.loads((state_dir / "status.json").read_text())
            if old.get("schema_version") != 1 or not isinstance(old.get("profiles"), list):
                raise ValueError("unsupported saved status")
            for row in old["profiles"]:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    continue
                saved = new_row(row["name"])
                for field in saved:
                    if field.endswith("_at"):
                        saved[field] = row.get(field) if epoch(row.get(field)) is not None else None
                saved["state"] = row.get("state", "pending")
                saved["message"] = str(row.get("message", ""))[:2048]
                if saved["state"] == "running":
                    saved.update(state="failed", message="Previous run was interrupted",
                                 last_failure_at=stamp(), last_finished_at=stamp())
                self.rows[saved["name"]] = saved
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as error:
            self.logger.error("cannot restore status: %s", error)

    def fail(self, name, message, finished=True):
        row = self.rows.setdefault(name, new_row(name))
        changed = row["state"] != "failed" or row["message"] != message
        row.update(state="failed", message=message)
        if changed or finished:
            row["last_failure_at"] = stamp()
            self.logger.error("profile=%s %s", name, message)
        if finished:
            row["last_finished_at"] = stamp()

    def publish(self, state="running"):
        atomic_json(self.state_dir / "status.json", {
            "schema_version": 1, "updated_at": stamp(),
            "daemon": {"state": state, "pid": os.getpid()},
            "profiles": [self.rows[name] for name in sorted(self.rows)],
        })

    def start(self, profile):
        row = self.rows[profile.name]
        row.update(state="running", last_started_at=stamp(), next_run_at=None, message="Cleanup running")
        # Persist intent before spawning so a daemon crash cannot leave an old OK
        # result standing in for an interrupted cleanup.
        self.publish()
        logger = make_logger(self.state_dir / "logs" / f"profile-{profile.name}.jsonl")
        logger.info("started")
        try:
            process = subprocess.Popen(profile.command, cwd=profile.cwd,
                                       env={**os.environ, **profile.env}, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        except (OSError, ValueError) as error:
            self.fail(profile.name, f"Could not start: {error}")
            logger.error("could not start: %s", error)
            close_logger(logger)
            return
        reader = threading.Thread(target=capture_output, args=(process.stdout, logger), daemon=True)
        reader.start()
        self.jobs[profile.name] = Job(process, time.monotonic() + profile.timeout, logger, reader)
        self.logger.info("profile=%s started pid=%s", profile.name, process.pid)

    @staticmethod
    def send(job, sig):
        try:
            os.killpg(job.process.pid, sig)
        except ProcessLookupError:
            pass

    def reap(self):
        now = time.monotonic()
        for name, job in list(self.jobs.items()):
            if job.stopping_at is None and (self.stopping or now >= job.deadline):
                job.failure = "Interrupted by daemon shutdown" if self.stopping else "Cleanup timed out"
                job.stopping_at = now
                self.send(job, signal.SIGTERM)
            if job.stopping_at is not None and now - job.stopping_at >= 2:
                self.send(job, signal.SIGKILL)
            code = job.process.poll()
            if code is None:
                continue
            # Scripts are foreground jobs: never leave background descendants behind.
            self.send(job, signal.SIGKILL)
            job.reader.join(timeout=2)
            row = self.rows[name]
            row["last_finished_at"] = stamp()
            if job.failure or code:
                self.fail(name, job.failure or f"Cleanup exited with status {code}")
            else:
                row.update(state="ok", message="Cleanup completed", last_success_at=stamp())
                self.logger.info("profile=%s completed", name)
            job.logger.info("finished exit_code=%s result=%s", code, row["message"])
            close_logger(job.logger)
            del self.jobs[name]

    def tick(self):
        self.reap()
        profiles, errors = load_profiles(self.config_dir)
        names = set(profiles) | set(errors) | set(self.jobs)
        self.rows = {name: row for name, row in self.rows.items() if name in names}
        for name, message in errors.items():
            if name not in self.jobs:
                self.fail(name, f"Invalid profile: {message}", finished=False)
                self.rows[name]["next_run_at"] = None
        for request in sorted((self.state_dir / "requests").glob("*.json"))[:100]:
            try:
                if request.stat().st_size > 4096:
                    raise ValueError("request too large")
                data = json.loads(request.read_text())
                name = data.get("name")
                if not isinstance(name, str) or name not in profiles:
                    raise ValueError("unknown profile")
                if profiles[name].enabled and name not in self.jobs:
                    self.requested.add(name)
                else:
                    self.logger.warning("profile=%s manual request skipped (disabled or already running)", name)
            except (OSError, ValueError, AttributeError) as error:
                self.logger.warning("invalid manual request: %s", error)
            finally:
                request.unlink(missing_ok=True)
        self.requested.intersection_update(profiles)
        for name, profile in profiles.items():
            row = self.rows.setdefault(name, new_row(name))
            row["enabled"] = profile.enabled
            if name in self.jobs:
                continue
            if not profile.enabled:
                row.update(state="disabled", message="Profile disabled", next_run_at=None)
                self.requested.discard(name)
                continue
            if row["state"] == "disabled" or row["message"].startswith("Invalid profile:"):
                row.update(state="pending", message="Waiting for next run")
            finished = epoch(row["last_finished_at"])
            due = finished + profile.every if finished is not None else time.time()
            row["next_run_at"] = stamp(due)
            if (not self.stopping and (name in self.requested or time.time() >= due)
                    and len(self.jobs) < MAX_PARALLEL):
                self.requested.discard(name)
                self.start(profile)
        self.publish()

    def run(self):
        self.logger.info("daemon started")
        try:
            while not self.stopping:
                self.tick()
                time.sleep(TICK)
        finally:
            self.close()

    def close(self):
        self.stopping = True
        while self.jobs:
            self.reap()
            time.sleep(0.1)
        try:
            self.publish("stopped")
            self.logger.info("daemon stopped")
        finally:
            close_logger(self.logger)
            self.lock.close()


def default_paths():
    home = Path.home()
    return (Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "gc-profiled" / "profiles",
            Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "gc-profiled")


def read_status(state_dir):
    with (state_dir / "status.json").open() as stream:
        return json.load(stream)


def queue_run(name, config_dir, state_dir):
    profiles, errors = load_profiles(config_dir)
    if name in errors:
        raise ValueError(errors[name])
    if name not in profiles or not profiles[name].enabled:
        raise ValueError("profile is missing or disabled")
    status = read_status(state_dir)
    updated = epoch(status.get("updated_at"))
    if (status.get("daemon", {}).get("state") != "running" or
            updated is None or not -5 <= time.time() - updated <= 30):
        raise ValueError("daemon is not running or its status is stale")
    with (state_dir / "daemon.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise ValueError("daemon is not running")
    atomic_json(state_dir / "requests" / f"{uuid.uuid4().hex}.json", {"name": name})


def main(argv=None):
    os.umask(0o077)
    config, state = default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config-dir", type=Path, default=config)
    parser.add_argument("--state-dir", type=Path, default=state)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("daemon", help="run the foreground scheduler")
    commands.add_parser("validate", help="validate profiles without executing them")
    commands.add_parser("status", help="print the current JSON status")
    commands.add_parser("list", help="show profiles and last result")
    run = commands.add_parser("run", help="request an enabled profile now (requires the daemon)")
    run.add_argument("name")
    logs = commands.add_parser("logs", help="show the tail of a profile log")
    logs.add_argument("name", help="profile name, or @daemon")
    args = parser.parse_args(argv)
    try:
        if args.action == "daemon":
            daemon = Daemon(args.config_dir, args.state_dir)
            def stop(_signum, _frame):
                daemon.stopping = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            daemon.run()
        elif args.action == "validate":
            if not args.config_dir.is_dir():
                raise ValueError(f"profile directory does not exist: {args.config_dir}")
            profiles, errors = load_profiles(args.config_dir)
            for name in profiles:
                print(f"OK {name}")
            for name, error in errors.items():
                print(f"ERROR {name}: {error}", file=sys.stderr)
            print(f"{len(profiles)} valid, {len(errors)} invalid")
            return int(bool(errors))
        elif args.action in {"status", "list"}:
            status = read_status(args.state_dir)
            if args.action == "status":
                print(json.dumps(status, indent=2))
            else:
                updated = epoch(status.get("updated_at"))
                health = status.get("daemon", {}).get("state", "unknown")
                if updated is None or time.time() - updated > 30:
                    health = "stale"
                print(f"Daemon: {health}")
                for row in status["profiles"]:
                    print(f"{row['name']}: {row['state']} | last finish: {row['last_finished_at'] or 'never'} | {row['message']}")
        elif args.action == "run":
            queue_run(args.name, args.config_dir, args.state_dir)
            print(f"Queued {args.name}")
        elif args.action == "logs":
            if args.name != "@daemon" and not NAME.fullmatch(args.name):
                raise ValueError("invalid profile name")
            name = "daemon" if args.name == "@daemon" else f"profile-{args.name}"
            path = args.state_dir / "logs" / f"{name}.jsonl"
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                lines = stream.read().decode("utf-8", "replace").splitlines()[-100:]
            print("\n".join(lines))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"gc-profiled: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
