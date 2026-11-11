import ctypes
import json
import os
import sys
import textwrap
import threading
import time

import pytest

from usage_tray import limits as L

FX = os.path.join(os.path.dirname(__file__), 'fixtures')
NOW = 1_800_000_000.0


def fixture(name):
    with open(os.path.join(FX, name), encoding='utf-8') as f:
        return json.load(f)


# ---------- parsing saved answers ----------

def test_claude_fixture_matches_t3_rows():
    rows = L.parse_claude(fixture('claude_usage.json'))
    assert [r.key for r in rows][:1] == ['seven_day']  # five_hour is null in this fixture, so no Session row
    assert any(r.label == 'Weekly · Sonnet' for r in rows)
    for r in rows:
        assert 0 <= r.used <= 100 and r.window_mins == L.WEEK_MINS and r.resets_at > 1.7e9


def test_codex_fixture_session_then_weekly():
    rows = L.parse_codex(fixture('codex_ratelimits.json'))
    assert [r.label for r in rows] == ['Session', 'Weekly']
    assert [r.window_mins for r in rows] == [300, 10080]


def claude_payload(**rl):
    return {'rate_limits_available': True, 'rate_limits': rl}


def test_claude_session_row_first_when_present():
    rows = L.parse_claude(claude_payload(
        seven_day={'utilization': 37, 'resets_at': '2030-01-08T12:00:00.79+00:00'},
        five_hour={'utilization': 40.5, 'resets_at': '2030-01-01T20:00:00Z'}))
    assert [(r.label, r.used) for r in rows] == [('Session', 40.5), ('Weekly', 37)]
    assert rows[0].resets_at == pytest.approx(1893528000.0)


@pytest.mark.parametrize('payload', [
    {}, None, [], {'rate_limits_available': False, 'rate_limits': {'seven_day': {'utilization': 5}}},
    {'rate_limits_available': True, 'rate_limits': None},
    claude_payload(seven_day=None, five_hour={'utilization': 'x'}),
])
def test_claude_without_limits_is_an_error(payload):
    with pytest.raises(L.ProbeError):
        L.parse_claude(payload)


def test_claude_spend_row():
    rows = L.parse_claude(claude_payload(
        seven_day={'utilization': 1},
        spend={'enabled': True, 'used': {'amount_minor': 2500, 'currency': 'USD', 'exponent': 2},
               'limit': {'amount_minor': 10000, 'currency': 'USD', 'exponent': 2}}))
    assert (rows[-1].label, rows[-1].used, rows[-1].resets_at) == ('Monthly spend', 25.0, None)


def test_claude_spend_mismatched_currency_falls_back_to_extra_usage():
    rows = L.parse_claude(claude_payload(
        seven_day={'utilization': 1},
        spend={'enabled': True, 'used': {'amount_minor': 1, 'currency': 'USD', 'exponent': 2},
               'limit': {'amount_minor': 2, 'currency': 'EUR', 'exponent': 2}},
        extra_usage={'is_enabled': True, 'used_credits': 30, 'monthly_limit': 40}))
    assert rows[-1].used == 75.0


def test_claude_utilization_is_clamped_and_bools_ignored():
    rows = L.parse_claude(claude_payload(seven_day={'utilization': 140}, five_hour={'utilization': True}))
    assert [(r.key, r.used) for r in rows] == [('seven_day', 100.0)]


def test_codex_extra_limit_gets_suffix_and_main_comes_first():
    snap = lambda used: {'primary': {'usedPercent': used, 'windowDurationMins': 300, 'resetsAt': NOW + 60}}
    rows = L.parse_codex({
        'rateLimits': {'limitId': 'codex', **snap(10)},
        'rateLimitsByLimitId': {'gpt_mini': {'limitName': 'Mini', **snap(50)}, 'codex': snap(10)}})
    assert [r.label for r in rows] == ['Session', 'Session · Mini']


def test_codex_only_main_snapshot_and_odd_window():
    rows = L.parse_codex({'rateLimits': {'primary': {'usedPercent': 5, 'windowDurationMins': 1440, 'resetsAt': None}}})
    assert [(r.label, r.resets_at) for r in rows] == [('1d', None)]


@pytest.mark.parametrize('result', [None, {}, {'rateLimits': {'primary': None}}, {'rateLimitsByLimitId': {'codex': 3}}])
def test_codex_without_limits_is_an_error(result):
    with pytest.raises(L.ProbeError):
        L.parse_codex(result)


# ---------- display math ----------

def row(used, secs_left, mins=300):
    return L.Row('k', 'Session', used, NOW + secs_left if secs_left is not None else None, mins)


@pytest.mark.parametrize('secs, text', [
    (27 * 60 + 30, '27m'), (30, '1m'), (3720, '1h 2m'), (5 * 86400 + 9 * 3600 + 59, '5d 9h'), (0, 'now'), (-5, 'now'),
])
def test_countdown(secs, text):
    assert L.countdown(row(1, secs), NOW) == text


def test_countdown_none_without_reset():
    assert L.countdown(row(1, None), NOW) is None


def test_pace_cases():
    assert L.pace(row(70, 30 * 60), NOW) == 'under'  # Session 30% left, 30m of 5h left
    assert L.pace(row(40, 100 * 3600, 10080), NOW) == 'on'  # Weekly 60% left, 100h of 168h left
    assert L.pace(row(10, 120 * 3600, 10080), NOW) == 'under'  # 90% left, 120h of 168h left
    assert L.pace(row(40, 9 * 3600 * 0.5, 300), NOW) is not None
    assert L.pace(row(80, 270 * 60), NOW) == 'over'  # 20% left with 90% of the time left


def test_window_that_reset_since_the_check_shows_full():
    r = row(80, -10)
    assert (L.used_now(r, NOW), L.left_pct(r, NOW), L.time_left_frac(r, NOW), L.pace(r, NOW)) == (0.0, 100, None, None)


def test_time_left_fraction():
    assert L.time_left_frac(row(0, 150 * 60), NOW) == pytest.approx(0.5)
    assert L.time_left_frac(row(0, 999 * 60), NOW) == 1.0
    assert L.time_left_frac(row(0, None), NOW) is None


@pytest.mark.parametrize('left, lvl', [(100, 'ok'), (26, 'ok'), (25, 'amber'), (11, 'amber'), (10, 'red'), (0, 'red')])
def test_level(left, lvl):
    assert L.level(left) == lvl


def test_left_rounds():
    assert L.left_pct(row(30.4, 60), NOW) == 70 and L.left_pct(row(30.6, 60), NOW) == 69


def test_tray_rows():
    claude = [L.Row('seven_day_sonnet', 'Weekly · Sonnet', 12), L.Row('seven_day', 'Weekly', 30)]
    assert L.tray_row('claude', claude).key == 'seven_day'
    assert L.tray_row('claude', claude[:1]) is None
    codex = [L.Row('codex:secondary', 'Weekly', 60, None, 10080), L.Row('codex:primary', 'Session', 80, None, 300)]
    assert L.tray_row('codex', codex).key == 'codex:primary'
    assert L.tray_row('codex', codex[:1]).key == 'codex:secondary'
    assert L.tray_row('codex', []) is None


def test_tooltip_content_and_windows_limit():
    st = L.ProviderState([row(30, 3600, 10080), L.Row('x', 'Weekly · Sonnet', 12, NOW + 60, 10080)], NOW - 60)
    t = L.tooltip('Claude Code', st, NOW)
    assert t.startswith('Claude Code · checked ') and '70% left · 1h 0m' in t and 'Weekly · Sonnet  88% left · 1m' in t
    many = L.ProviderState([L.Row(str(i), f'Weekly · Some long model name {i}', i, NOW + 99999, 10080) for i in range(9)],
                           NOW, 'no answer in 25 s')
    long = L.tooltip('Claude Code', many, NOW)
    assert len(long) <= 127 and long.endswith('…') and 'check failed: no answer in 25 s' in long


def test_tooltip_before_first_check():
    assert L.tooltip('Codex', L.ProviderState(), NOW) == 'Codex · checking…'


def test_state_round_trip():
    st = L.ProviderState([row(5, 60)], NOW, 'oops')
    back = L.ProviderState.from_json(json.loads(json.dumps(st.to_json())))
    assert back.rows == st.rows and back.checked_at == NOW and back.error is None


# ---------- talking to a CLI (fake ones) ----------

def alive(pid):
    k = ctypes.WinDLL('kernel32')
    k.OpenProcess.restype = ctypes.c_void_p
    h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not h:
        return False
    try:
        return k.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102  # WAIT_TIMEOUT = still running
    finally:
        k.CloseHandle(ctypes.c_void_p(h))


def fake(tmp_path, body, hello=()):
    p = tmp_path / 'fake_cli.py'
    p.write_text(textwrap.dedent(body), encoding='utf-8')
    return L.Session([sys.executable, '-u', str(p)], list(hello))


ECHO = '''
    import json, os, sys
    for line in sys.stdin:
        m = json.loads(line)
        print('not json')
        print(json.dumps({'id': m.get('id'), 'pid': os.getpid()}), flush=True)
'''


def by_id(n):
    return {'id': n}, lambda m: ('ok', m['pid']) if m.get('id') == n else None


def test_session_answers_and_stays_running(tmp_path):
    s = fake(tmp_path, ECHO, hello=[{'id': 'hello'}])
    try:
        first = s.ask(by_id, timeout=10)
        second = s.ask(by_id, timeout=10)
        assert first == second and alive(first)  # same process answered both: no fresh start per check
    finally:
        s.close()
    time.sleep(0.3)
    assert not alive(first)


def test_session_restarts_when_old(tmp_path, caplog):
    caplog.set_level('INFO')
    s = fake(tmp_path, ECHO)
    s.MAX_AGE = 0
    try:
        a = s.ask(by_id, timeout=10)
        time.sleep(0.05)
        b = s.ask(by_id, timeout=10)
        assert a != b
        time.sleep(0.3)
        assert not alive(a)
    finally:
        s.close()
    assert 'started a copy (first check)' in caplog.text and 'started a copy (every 30 min)' in caplog.text


def test_session_restarts_when_the_login_changes(tmp_path, caplog):
    caplog.set_level('INFO')
    s = fake(tmp_path, ECHO)
    who = {'now': 'account-a'}

    def login():
        if who['now'] == 'mid-write':
            raise ValueError('half-written file')
        return who['now']
    s.login = login
    try:
        a = s.ask(by_id, timeout=10)
        assert s.ask(by_id, timeout=10) == a  # same login: same copy
        who['now'] = 'mid-write'
        assert s.ask(by_id, timeout=10) == a  # can't tell: keep the copy
        who['now'] = 'account-b'
        b = s.ask(by_id, timeout=10)
        assert b != a
        who['now'] = None  # signed out
        c = s.ask(by_id, timeout=10)
        assert c != b and s.ask(by_id, timeout=10) == c
    finally:
        s.close()
    assert caplog.text.count('started a copy (the login changed)') == 2


@pytest.mark.parametrize('content, account', [
    ('{"oauthAccount": {"accountUuid": "u1"}, "x": 1}', 'u1'),
    ('{"x": 1}', None),  # signed out
    ('[]', None),
])
def test_claude_account_reads_the_account_id(tmp_path, monkeypatch, content, account):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    (tmp_path / '.claude.json').write_text(content, encoding='utf-8')
    assert L.claude_account() == account


def test_claude_account_half_written_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    (tmp_path / '.claude.json').write_text('{"oauthAccount": {"accou', encoding='utf-8')
    with pytest.raises(ValueError):  # Session keeps its copy on this
        L.claude_account()


def test_codex_login_is_the_files_change_time(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    assert L.codex_login() is None  # signed out
    (tmp_path / 'auth.json').write_text('{}')
    assert L.codex_login() == (tmp_path / 'auth.json').stat().st_mtime_ns


def test_session_usage_counts_every_copy(tmp_path):
    s = fake(tmp_path, ECHO)
    assert s.usage() == {'cpu': 0.0, 'starts': 0, 'procs': 0, 'running': 0, 'mb': 0.0}
    try:
        s.ask(by_id, timeout=10)
        u = s.usage()
        assert u['starts'] == 1 and u['running'] >= 1 and u['procs'] >= 1 and u['mb'] > 1
    finally:
        s.close()
    s.ask(by_id, timeout=10)  # a second copy
    s.close()
    u = s.usage()
    assert u['starts'] == 2 and u['running'] == 0 and u['procs'] >= 2 and u['cpu'] > 0


def test_session_usage_does_not_wait_for_a_check(tmp_path):
    s = fake(tmp_path, 'import time; time.sleep(120)')  # never answers
    t = threading.Thread(target=lambda: pytest.raises(L.ProbeError, s.ask, by_id, timeout=3))
    t.start()
    time.sleep(1)  # the check now holds the session lock
    t0 = time.time()
    assert s.usage()['running'] >= 1
    assert time.time() - t0 < 0.5  # the window calls this; it must not freeze behind a check
    t.join()


def test_session_error_answer_then_recovers(tmp_path):
    s = fake(tmp_path, '''
        import json, os, sys
        for line in sys.stdin:
            m = json.loads(line)
            out = {'id': m['id'], 'error': {'message': 'not signed in'}} if m['id'] == 2 else {'id': m['id'], 'pid': os.getpid()}
            print(json.dumps(out), flush=True)
    ''')

    def build(n):
        return {'id': n}, lambda m: (('error', m['error']['message']) if 'error' in m else ('ok', n)) if m.get('id') == n else None
    try:
        with pytest.raises(L.ProbeError, match='not signed in'):
            s.ask(build, timeout=10)
        assert s.p is None  # dropped after a failure
        assert s.ask(build, timeout=10) == 3  # next check starts a fresh copy
    finally:
        s.close()


def test_session_cli_that_exits_without_answering(tmp_path):
    s = fake(tmp_path, 'print("hello")')
    with pytest.raises(L.ProbeError, match='closed without answering'):
        s.ask(by_id, timeout=10)


def test_session_cli_that_closed_its_output_fails_at_once(tmp_path):
    """Its end of output came before the check was waiting for it; that must not be lost (25 s wait)."""
    p = tmp_path / 'fake_cli.py'
    p.write_text('import os, time; os.close(1); time.sleep(120)')
    # the base python: a venv's python.exe is a launcher that keeps its own copy of the output pipe open
    s = L.Session([getattr(sys, '_base_executable', sys.executable), '-u', str(p)], [])
    s._start()
    for _ in range(100):
        if getattr(s.p, 'output_ended', False):
            break
        time.sleep(0.05)
    assert getattr(s.p, 'output_ended', False) and s.p.poll() is None  # output closed, process still running
    t0 = time.time()
    with pytest.raises(L.ProbeError, match='closed without answering'):
        s.ask(by_id, timeout=10)
    assert time.time() - t0 < 3


WEEK = {'utilization': 50.0, 'resets_at': '2030-01-08T12:00:00Z'}
ANSWER = {'five_hour': None, 'seven_day': WEEK, 'model_scoped': []}


def snapshot(fetched=1800000000000, week=WEEK):
    return json.dumps({'cachedUsageUtilization': {'fetchedAtMs': fetched, 'utilization': {'five_hour': None, 'seven_day': week}}})


@pytest.mark.parametrize('content, expected', [
    (snapshot(), 1_800_000_000.0),  # the snapshot holds these numbers: they are from its time
    (snapshot(week={**WEEK, 'utilization': 51.0}), None),  # other numbers: age unknown
    (snapshot(fetched=True), None), ('{"cachedUsageUtilization": 5}', None),
    ('{}', None), ('[]', None), ('{"cachedUsageUtilization": {"fetchedAtMs": 1', None)])  # half written
def test_claude_numbers_time_reads_claudes_snapshot(tmp_path, monkeypatch, content, expected):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    assert L.claude_numbers_time(ANSWER) is None  # no file
    (tmp_path / '.claude.json').write_text(content)
    assert L.claude_numbers_time(ANSWER) == expected


def test_numbers_time_keeps_a_known_time_when_the_age_is_unknown():
    rows = [L.Row('seven_day', 'Weekly', 50, NOW + 86400, 10080)]
    st = L.ProviderState(rows, NOW - 1500)
    assert L.numbers_time(st, rows, NOW - 100, NOW) == NOW - 100  # known: use it
    assert L.numbers_time(st, rows, NOW + 50, NOW) == NOW  # never in the future
    assert L.numbers_time(st, list(rows), None, NOW) == NOW - 1500  # unknown, same numbers: still that old
    assert L.numbers_time(st, [L.Row('seven_day', 'Weekly', 51, NOW + 86400, 10080)], None, NOW) == NOW  # changed
    assert L.numbers_time(L.ProviderState(), rows, None, NOW) == NOW


def test_old_numbers_show_their_time_instead_of_checked():
    rows = [L.Row('seven_day', 'Weekly', 50, NOW + 86400, 10080)]
    fresh = L.ProviderState(rows, NOW - 60)
    old = L.ProviderState(rows, NOW - 1500)
    assert 'checked' in L.tooltip('Claude Code', fresh, NOW) and not L.stale(fresh, NOW)
    assert f'numbers from {L.clock(NOW - 1500)}' in L.tooltip('Claude Code', old, NOW) and L.stale(old, NOW)
    assert not L.stale(L.ProviderState(rows, NOW - 1500, 'no answer in 25 s'), NOW)  # the error line says it


def test_session_missing_program():
    s = L.Session([r'C:\nope\missing.exe'], [])
    with pytest.raises(L.ProbeError, match='could not start'):
        s.ask(by_id, timeout=2)


def test_session_kills_a_hung_cli_and_its_children(tmp_path):
    pids = tmp_path / 'pids.txt'
    s = fake(tmp_path, f'''
        import os, subprocess, sys, time
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        open(r'{pids}', 'w').write(f'{{os.getpid()}} {{child.pid}}')
        time.sleep(120)  # ignores input, never answers
    ''')
    t0 = time.time()
    with pytest.raises(L.ProbeError, match='no answer in 2 s'):
        s.ask(by_id, timeout=2)
    assert time.time() - t0 < 10
    parent, child = map(int, pids.read_text().split())
    time.sleep(0.5)
    assert not alive(parent) and not alive(child)


def test_close_ends_children_a_cli_leaves_behind(tmp_path):
    pids = tmp_path / 'pids.txt'
    s = fake(tmp_path, f'''
        import json, os, subprocess, sys
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        open(r'{pids}', 'w').write(str(child.pid))
        for line in sys.stdin:
            print(json.dumps({{'id': json.loads(line)['id'], 'pid': os.getpid()}}), flush=True)
    ''')
    parent = s.ask(by_id, timeout=10)
    child = int(pids.read_text())
    assert alive(child)
    s.close()  # the CLI exits on end of input; its child would survive without the job
    time.sleep(0.5)
    assert not alive(parent) and not alive(child)


def test_fetch_reports_missing_cli(monkeypatch):
    monkeypatch.setattr(L.shutil, 'which', lambda name: None)
    L.close_all()
    with pytest.raises(L.ProbeError, match='Claude Code not found'):
        L.fetch_claude()
    with pytest.raises(L.ProbeError, match='Codex not found'):
        L.fetch_codex()


# ---------- edge cases ----------

@pytest.mark.parametrize('bad', [float('inf'), float('nan'), -float('inf')])
def test_nonfinite_numbers_are_ignored(bad):
    assert L._num(bad) is None
    rows = L.parse_codex({'rateLimits': {'primary': {'usedPercent': 5, 'windowDurationMins': 300, 'resetsAt': bad},
                                         'secondary': {'usedPercent': bad, 'windowDurationMins': 10080}}})
    assert [(r.label, r.resets_at) for r in rows] == [('Session', None)]
    assert L.countdown(rows[0], NOW) is None


@pytest.mark.parametrize('row', [
    {'key': 'k', 'label': 'Weekly', 'used': 'bad'},
    {'key': 'k', 'label': 'Weekly', 'used': 5, 'resets_at': 'soon'},
    {'key': 'k', 'label': 'Weekly', 'used': 5, 'window_mins': -1},
    {'key': 'k', 'label': 'Weekly', 'used': 5, 'window_mins': 1.5},
    {'key': 3, 'label': 'Weekly', 'used': 5},
    {'label': 'Weekly', 'used': 5},
])
def test_cached_rows_are_checked(row):
    with pytest.raises((TypeError, KeyError)):
        L.ProviderState.from_json({'rows': [row], 'checked_at': NOW})


def test_cached_row_with_zero_reset_survives():
    st = L.ProviderState.from_json({'rows': [{'key': 'k', 'label': 'W', 'used': 5, 'resets_at': 0}], 'checked_at': None})
    assert st.rows[0].resets_at == 0.0


def test_codex_tray_uses_the_main_limit_only():
    rows = L.parse_codex({
        'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 30, 'windowDurationMins': 10080}},
        'rateLimitsByLimitId': {'other': {'limitName': 'Mini', 'primary': {'usedPercent': 90, 'windowDurationMins': 300}}}})
    assert L.tray_row('codex', rows).label == 'Weekly'  # not Mini's 5-hour row


def test_tooltip_counts_like_windows():
    st = L.ProviderState([L.Row(str(i), 'Weekly · 🚀🚀🚀🚀🚀', 1, None, 10080) for i in range(8)], NOW)
    t = L.tooltip('Claude Code', st, NOW)
    assert L._utf16_len(t) <= 127 and t.endswith('…')
    t.encode('utf-16-le')  # no half emoji left at the cut


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows searches the current folder')
def test_cli_lookup_skips_current_folder(monkeypatch, tmp_path):
    (tmp_path / 'claude.cmd').write_text('@echo planted\n')
    monkeypatch.delenv('NoDefaultCurrentDirectoryInExePath', raising=False)
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    monkeypatch.setenv('PATHEXT', '.EXE;.BAT;.CMD')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(L.ProbeError, match='Claude Code not found'):
        L._claude_exe()
