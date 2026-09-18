#!/usr/bin/env python3
"""Install the daemon and a systemd user service without installing profiles."""

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def unit_quote(value):
    return '"' + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable", action="store_true", help="enable and start the installed user service")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or later is required")
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "gc-profiled"
    profiles = config / "gc-profiled/profiles"
    library = home / ".local/lib/gc-profiled"
    binary = home / ".local/bin/gc-profiled"
    units = config / "systemd/user"
    for directory in (library, binary.parent, units, profiles, state):
        directory.mkdir(parents=True, exist_ok=True)
    profiles.chmod(0o700)
    state.chmod(0o700)
    source = Path(__file__).resolve().parent.parent / "gc_profiled.py"
    target = library / "gc_profiled.py"
    shutil.copyfile(source, target.with_suffix(".new"))
    target.with_suffix(".new").replace(target)
    binary.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(target))} \"$@\"\n")
    binary.chmod(0o755)
    command = " ".join(unit_quote(arg) for arg in (
        sys.executable, target, "--config-dir", profiles, "--state-dir", state, "daemon"))
    (units / "gc-profiled.service").write_text(
        "[Unit]\nDescription=Scheduled garbage-collection profiles\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=10\n"
        "KillMode=control-group\nUMask=0077\nNoNewPrivileges=yes\n\n"
        "[Install]\nWantedBy=default.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if args.enable:
        subprocess.run(["systemctl", "--user", "enable", "gc-profiled.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "gc-profiled.service"], check=True)
    print(f"Installed {binary}\nProfiles: {profiles}\nState and logs: {state}")
    print("No cleanup profiles were installed or enabled.")
    if not args.enable:
        print("Start with: systemctl --user enable --now gc-profiled.service")


if __name__ == "__main__":
    main()
