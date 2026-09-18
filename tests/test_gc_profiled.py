import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

import gc_profiled


SCRIPT = (
    "import os, pathlib, sys, time; "
    "pathlib.Path(os.environ['MARKER']).write_text(os.getcwd() + '|' + os.environ['GC_TEST']); "
    "print('hello from cleanup', flush=True)"
)


class DaemonTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = root / "profiles"
        self.state = root / "state"
        self.config.mkdir()
        self.cwd = root / "work"
        self.cwd.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def profile(self, name="cleanup", **fields):
        values = {
            "command": [sys.executable, "-c", SCRIPT],
            "cwd": str(self.cwd),
            "every": "1h",
            "timeout": "15s",
            "env": {"MARKER": str(self.cwd / "marker"), "GC_TEST": "present"},
        }
        values.update(fields)
        lines = []
        for key, value in values.items():
            if isinstance(value, dict):
                continue
            if isinstance(value, list):
                lines.append(f"{key} = {json.dumps(value)}")
            elif isinstance(value, bool):
                lines.append(f"{key} = {str(value).lower()}")
            else:
                lines.append(f"{key} = {json.dumps(value)}")
        for key, value in values.items():
            if isinstance(value, dict):
                lines.append(f"[{key}]")
                for subkey, subvalue in value.items():
                    lines.append(f"{subkey} = {json.dumps(subvalue)}")
        (self.config / f"{name}.toml").write_text("\n".join(lines) + "\n")

    def daemon(self):
        return gc_profiled.Daemon(self.config, self.state)

    def wait_done(self, daemon, timeout=5):
        deadline = time.monotonic() + timeout
        while daemon.jobs and time.monotonic() < deadline:
            daemon.tick()
            time.sleep(0.03)
        self.assertFalse(daemon.jobs, "job did not finish")


class ProfileValidationTests(DaemonTestCase):
    def test_duration_and_profile_validation(self):
        self.assertEqual(gc_profiled.duration("1.5m"), 90)
        self.assertEqual(gc_profiled.duration(1), 1)
        for value in (True, 0, "2x", float("inf"), "10y"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    gc_profiled.duration(value)
        self.profile(every="30s")
        profile = gc_profiled.read_profile(self.config / "cleanup.toml")
        self.assertEqual(profile.every, 30)
        self.assertEqual(profile.cwd, str(self.cwd))
        self.assertEqual(profile.env["GC_TEST"], "present")

        (self.config / "bad.toml").write_text(
            'command = ["/bin/true"]\ncwd = "/tmp"\nevery = "1s"\nunknown = 1\n'
        )
        profiles, errors = gc_profiled.load_profiles(self.config)
        self.assertIn("cleanup", profiles)
        self.assertIn("bad", errors)

    def test_disabled_and_invalid_profiles_are_published_without_running(self):
        self.profile("disabled", enabled=False)
        (self.config / "invalid.toml").write_text(
            'command = ["/bin/true"]\ncwd = "/tmp"\nevery = "0s"\n'
        )
        daemon = self.daemon()
        try:
            daemon.tick()
            rows = {row["name"]: row for row in gc_profiled.read_status(self.state)["profiles"]}
            self.assertEqual(rows["disabled"]["state"], "disabled")
            self.assertEqual(rows["invalid"]["state"], "failed")
            self.assertIn("Invalid profile", rows["invalid"]["message"])
        finally:
            daemon.close()

    def test_invalid_utf8_profile_is_isolated_from_valid_profiles(self):
        self.profile("valid")
        (self.config / "encoding.toml").write_bytes(b"command = [\"/bin/true\"]\ncwd = \"/tmp\"\nevery = \"1s\"\n\xff\n")
        profiles, errors = gc_profiled.load_profiles(self.config)
        self.assertIn("valid", profiles)
        self.assertIn("encoding", errors)

    def test_corrupt_saved_status_is_recovered(self):
        self.state.mkdir()
        (self.state / "status.json").write_text("{not json")
        daemon = self.daemon()
        try:
            daemon.tick()
            status = gc_profiled.read_status(self.state)
            self.assertEqual(status["schema_version"], 1)
        finally:
            daemon.close()


class LifecycleTests(DaemonTestCase):
    def test_success_captures_output_cwd_and_environment(self):
        self.profile(every="1h")
        daemon = self.daemon()
        try:
            daemon.tick()
            self.wait_done(daemon)
            row = daemon.rows["cleanup"]
            self.assertEqual(row["state"], "ok")
            self.assertTrue((self.cwd / "marker").read_text().startswith(str(self.cwd) + "|present"))
            log = (self.state / "logs" / "profile-cleanup.jsonl").read_text()
            self.assertIn("hello from cleanup", log)
        finally:
            daemon.close()

    def test_failure_and_manual_request(self):
        self.profile("fail", command=[sys.executable, "-c", "import sys; print('bad'); sys.exit(3)"])
        daemon = self.daemon()
        try:
            daemon.rows["fail"] = gc_profiled.new_row("fail")
            daemon.rows["fail"]["last_finished_at"] = gc_profiled.stamp()
            request = self.state / "requests" / "request.json"
            request.write_text('{"name":"fail"}')
            daemon.tick()
            self.wait_done(daemon)
            self.assertEqual(daemon.rows["fail"]["state"], "failed")
            self.assertIn("status 3", daemon.rows["fail"]["message"])
            self.assertFalse(request.exists())
        finally:
            daemon.close()

    def test_completion_schedules_next_run_and_running_job_does_not_overlap(self):
        marker = self.cwd / "count"
        command = [sys.executable, "-c", "import pathlib,time; p=pathlib.Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); time.sleep(.2)"]
        self.profile(every="1h", command=command)
        daemon = self.daemon()
        try:
            daemon.tick()
            daemon.tick()
            self.assertEqual(len(daemon.jobs), 1)
            self.wait_done(daemon)
            self.assertEqual(marker.read_text(), "1")
            daemon.tick()
            self.assertEqual(marker.read_text(), "1")
            self.assertIsNotNone(daemon.rows["cleanup"]["next_run_at"])
        finally:
            daemon.close()

    def test_interrupted_running_state_is_failed_on_restore(self):
        status = {
            "schema_version": 1, "updated_at": gc_profiled.stamp(),
            "daemon": {"state": "stopped", "pid": 1},
            "profiles": [{**gc_profiled.new_row("cleanup"), "state": "running"}],
        }
        self.state.mkdir()
        (self.state / "status.json").write_text(json.dumps(status))
        daemon = self.daemon()
        try:
            self.assertEqual(daemon.rows["cleanup"]["state"], "failed")
            self.assertEqual(daemon.rows["cleanup"]["message"], "Previous run was interrupted")
        finally:
            daemon.close()

    def test_timeout_terminates_process_group(self):
        child_pid = self.cwd / "child.pid"
        command = [sys.executable, "-c", "import os,subprocess,time; c=subprocess.Popen([os.sys.executable,'-c','import time; time.sleep(30)']); open('child.pid','w').write(str(c.pid)); time.sleep(30)"]
        self.profile(timeout="1s", command=command)
        daemon = self.daemon()
        try:
            daemon.tick()
            deadline = time.monotonic() + 5
            while daemon.jobs and time.monotonic() < deadline:
                daemon.tick()
                time.sleep(.1)
            self.assertFalse(daemon.jobs)
            self.assertEqual(daemon.rows["cleanup"]["state"], "failed")
            self.assertEqual(daemon.rows["cleanup"]["message"], "Cleanup timed out")
            self.assertTrue(child_pid.exists())
            pid = int(child_pid.read_text())
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                # A killed child may briefly remain as a zombie while reparented.
                proc_state = Path(f"/proc/{pid}/stat").read_text().split()[2]
                self.assertEqual(proc_state, "Z")
        finally:
            daemon.close()

    def test_output_lines_are_bounded(self):
        self.profile("verbose", command=[sys.executable, "-c", "print('x' * 12000, end='')"])
        daemon = self.daemon()
        try:
            daemon.tick()
            self.wait_done(daemon)
            records = [json.loads(line) for line in (self.state / "logs" / "profile-verbose.jsonl").read_text().splitlines()]
            output = [record["message"] for record in records if record["message"].startswith("output: ")]
            self.assertGreater(len(output), 1)
            self.assertTrue(all(len(message.removeprefix("output: ")) <= 8192 for message in output))
        finally:
            daemon.close()

    def test_restart_restores_success_and_respects_next_schedule(self):
        self.profile(every="1h")
        first = self.daemon()
        first.tick()
        self.wait_done(first)
        self.assertEqual(first.rows["cleanup"]["state"], "ok")
        first.close()
        second = self.daemon()
        try:
            second.tick()
            self.assertEqual(second.rows["cleanup"]["state"], "ok")
            self.assertFalse(second.jobs)
        finally:
            second.close()

    def test_bad_cwd_and_executable_are_reported_as_start_failures(self):
        self.profile("badcwd", cwd=str(self.cwd / "missing"))
        self.profile("badexe", command=[str(self.cwd / "missing-executable")])
        daemon = self.daemon()
        try:
            daemon.tick()
            self.assertEqual(daemon.rows["badcwd"]["state"], "failed")
            self.assertEqual(daemon.rows["badexe"]["state"], "failed")
            self.assertIn("Could not start", daemon.rows["badcwd"]["message"])
            self.assertIn("Could not start", daemon.rows["badexe"]["message"])
        finally:
            daemon.close()

    def test_rotation_keeps_bounded_profile_log_backups(self):
        self.profile("rotate", command=[sys.executable, "-c", "print('x' * 1100000, end='')"])
        daemon = self.daemon()
        try:
            daemon.tick()
            self.wait_done(daemon, timeout=10)
        finally:
            daemon.close()
        logs = sorted((self.state / "logs").glob("profile-rotate.jsonl*"))
        self.assertLessEqual(len(logs), 4)
        self.assertTrue(any(path.name == "profile-rotate.jsonl.1" for path in logs))
        self.assertTrue(all(path.stat().st_size <= 1024 * 1024 for path in logs))

    def test_sigterm_resistant_job_is_killed_during_shutdown(self):
        pid_file = self.cwd / "shutdown.pid"
        command = [sys.executable, "-c", "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open('shutdown.pid','w').write(str(os.getpid())); time.sleep(30)"]
        self.profile(timeout="15s", command=command)
        daemon = self.daemon()
        daemon.tick()
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue(pid_file.exists())
        daemon.close()
        self.assertFalse(daemon.jobs)
        self.assertEqual(daemon.rows["cleanup"]["state"], "failed")


class LockAndRequestTests(DaemonTestCase):
    def test_singleton_lock_and_manual_request_validation(self):
        self.profile()
        first = self.daemon()
        try:
            with self.assertRaises(RuntimeError):
                self.daemon()
            with self.assertRaises(ValueError):
                gc_profiled.queue_run("missing", self.config, self.state)
            first.tick()
            gc_profiled.queue_run("cleanup", self.config, self.state)
            requests = list((self.state / "requests").glob("*.json"))
            self.assertEqual(len(requests), 1)
        finally:
            first.close()


if __name__ == "__main__":
    unittest.main()
