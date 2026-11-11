"""Real checks against the installed, signed-in Claude Code and Codex. Opt-in: set LIVE=1. Also skipped when a CLI is missing."""
import json
import os
import shutil
import subprocess
import time

import pytest

from usage_tray import limits as L

pytestmark = pytest.mark.skipif(os.environ.get('LIVE') != '1', reason='live CLI tests: set LIVE=1 to run')


def leaked_probes(since):
    """Probe processes (this app's: `app-server` / `--no-session-persistence`) started after `since` that are still
    running with a dead parent or with this test as parent. Other programs' probes (such as T3 Code's) have a live parent."""
    out = subprocess.run(['powershell', '-NoProfile', '-Command',
                          "Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, CommandLine, "
                          "@{n='t';e={[DateTimeOffset]$_.CreationDate | % ToUnixTimeSeconds}} | ConvertTo-Json"],
                         capture_output=True, text=True).stdout.strip()
    rows = json.loads(out)
    alive = {r['ProcessId'] for r in rows}
    return [r for r in rows if r['t'] and r['t'] >= since
            and any(s in (r['CommandLine'] or '') for s in ('app-server', '--no-session-persistence'))
            and (r['ParentProcessId'] not in alive or r['ParentProcessId'] == os.getpid())]


def tree(pid):
    """Names of every process started (directly or not) by `pid`."""
    rows = json.loads(subprocess.run(['powershell', '-NoProfile', '-Command',
                                      'Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name | ConvertTo-Json'],
                                     capture_output=True, text=True).stdout)
    kids, todo = [], [pid]
    while todo:
        p = todo.pop()
        for r in rows:
            if r['ParentProcessId'] == p:
                kids.append(r['Name'])
                todo.append(r['ProcessId'])
    return kids


@pytest.mark.skipif(not (shutil.which('claude') and shutil.which('codex')), reason='needs both CLIs')
def test_kept_running_copies_stay_small():
    """Started with the user's plugins, both CLIs clone marketplaces with dozens of git processes. Codex starts at
    once; Claude's plugin auto-update waits a random time, too long to watch here, so its switch is checked."""
    assert L.CLAUDE.extra_env.get('DISABLE_AUTOUPDATER') == '1'
    L.close_all()
    L.fetch_claude(), L.fetch_codex()
    seen = []
    for _ in range(10):
        seen += tree(L.CLAUDE.p.pid) + tree(L.CODEX.p.pid)
        time.sleep(1)
    L.close_all()
    assert not [n for n in seen if n.lower().startswith('git')]
    assert len(set(seen)) <= 4


@pytest.mark.skipif(not shutil.which('claude'), reason='Claude Code not installed')
def test_claude_live():
    t0 = time.time()
    rows = L.fetch_claude()
    assert time.time() - t0 < 15
    assert rows and all(0 <= r.used <= 100 for r in rows)
    assert L.tray_row('claude', rows) is not None


@pytest.mark.skipif(not shutil.which('codex'), reason='Codex not installed')
def test_codex_live():
    t0 = time.time()
    rows = L.fetch_codex()
    assert time.time() - t0 < 15
    assert rows and all(0 <= r.used <= 100 for r in rows)
    main = rows[0].key.split(':')[0]  # some plans' main limit is weekly only
    has_5h = any(r.window_mins == L.SESSION_MINS for r in rows if r.key.split(':')[0] == main)
    assert L.tray_row('codex', rows).window_mins == (L.SESSION_MINS if has_5h else rows[0].window_mins)


@pytest.mark.skipif(not (shutil.which('claude') and shutil.which('codex')), reason='needs both CLIs')
def test_repeated_checks_reuse_one_copy_and_leave_nothing_running():
    L.close_all()
    since = int(time.time())
    L.fetch_claude(), L.fetch_codex()
    pids = (L.CLAUDE.p.pid, L.CODEX.p.pid)
    t0 = time.time()
    for _ in range(5):
        L.fetch_claude()
        L.fetch_codex()
    assert (L.CLAUDE.p.pid, L.CODEX.p.pid) == pids  # no fresh start per check
    assert time.time() - t0 < 10  # five rounds on warm copies
    L.close_all()
    time.sleep(1)
    assert leaked_probes(since) == []
