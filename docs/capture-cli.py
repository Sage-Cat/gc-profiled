#!/usr/bin/env python3
"""Capture real CLI output with an isolated headless browser; no live UI changes."""
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=15, **kwargs).strip()


def render(title, subtitle, content, destination):
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        raise SystemExit('Install Chrome or Chromium to render the documentation capture.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='documentation-browser-') as temporary:
        root = Path(temporary)
        page = root / 'capture.html'
        height = max(440, 220 + 24 * len(content.splitlines()))
        page.write_text('<!doctype html><meta charset="utf-8"><style>'
            'body{margin:0;background:#0c1420;color:#e2eaf2;font-family:monospace;padding:40px}'
            'h1{font:600 30px sans-serif;color:#69e0cf;margin:0 0 12px}'
            'p{font:16px sans-serif;color:#a8b6ca;margin-bottom:28px}'
            'pre{font:17px/24px monospace;white-space:pre-wrap;overflow-wrap:anywhere;'
            'padding:24px;background:#132234;border:1px solid #294157;border-radius:12px}'
            '</style><h1>' + html.escape(title) + '</h1><p>' + html.escape(subtitle)
            + '</p><pre>' + html.escape(content) + '</pre>')
        subprocess.run([chrome, '--headless', '--disable-gpu', '--disable-background-networking',
            '--no-first-run', '--no-default-browser-check', '--hide-scrollbars',
            '--user-data-dir=' + str(root / 'profile'), '--screenshot=' + str(destination),
            '--window-size=1200,' + str(height), '--timeout=10000', page.as_uri()],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    print(destination)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='profile-documentation-') as temporary:
        root = Path(temporary)
        config, state = root / 'profiles', root / 'state'
        config.mkdir()
        for name, enabled in [('example-task', True), ('paused-task', False)]:
            (config / (name + '.toml')).write_text(
                'description = "Harmless documentation example"\n'
                + 'enabled = ' + str(enabled).lower() + '\n'
                + 'command = ["/usr/bin/printf", "Example task completed\\n"]\n'
                + 'cwd = ' + json.dumps(str(root)) + '\n'
                + 'every = "1h"\ntimeout = "5s"\n')
        cli = ['python3', str(ROOT / 'gc_profiled.py'), '--config-dir', str(config), '--state-dir', str(state)]
        validated = run(cli + ['validate'])
        daemon = subprocess.Popen(cli + ['daemon'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if daemon.poll() is not None:
                    raise RuntimeError('Demo daemon stopped unexpectedly')
                if not (state / 'status.json').exists():
                    time.sleep(.2)
                    continue
                report = json.loads(run(cli + ['status']))
                if any(p['name'] == 'example-task' and p['state'] == 'ok' for p in report.get('profiles', [])):
                    break
                time.sleep(.2)
            else:
                raise RuntimeError('Demo task did not finish')
            listing = run(cli + ['list'])
            content = '$ gc-profiled validate\n' + validated + '\n\n$ gc-profiled list\n' + listing
            # Temp paths are presentation-only; daemon always uses explicit private directories.
            content = content.replace(str(root), '<demo-directory>')
            render('gc-profiled · scheduled profile results',
                'Real CLI output · isolated daemon · printf-only task, no cleanup',
                content, ROOT / 'docs/screenshots/profiles.png')
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=8)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=3)
