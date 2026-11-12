import ctypes
import io
import json
import os
import sys
import textwrap
import threading
import time
import urllib.error

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


# ---------- free resets ----------

def test_codex_resets_none_or_null():
    assert L.parse_codex_resets(fixture('codex_ratelimits.json')) == []
    assert L.parse_codex_resets({'rateLimitResetCredits': None}) == []
    assert L.parse_codex_resets({'rateLimitResetCredits': {'availableCount': 0, 'credits': []}}) == []
    assert L.parse_codex_resets(None) == []


def test_codex_resets_null_credits_still_count():
    [r] = L.parse_codex_resets({'rateLimitResetCredits': {'availableCount': 2, 'credits': None}})
    assert (r.provider, r.kind, r.label, r.count, r.expires_at, r.credit_id) == ('codex', 'codex', 'Codex reset', 2, None, None)


def test_codex_resets_mix_takes_first_available_expiry():
    credits = [
        {'id': 'a', 'status': 'redeemed', 'expiresAt': NOW + 10, 'title': 'Old'},
        {'id': 'b', 'status': 'available', 'expiresAt': NOW + 900, 'title': 'Weekly refill', 'description': 'Refills it.'},
        {'id': 'c', 'status': 'available', 'expiresAt': NOW + 500},
        {'id': 'd', 'status': 'redeeming', 'expiresAt': NOW + 1},
        {'id': 'e', 'status': 'available', 'expiresAt': None},
    ]
    [r] = L.parse_codex_resets({'rateLimitResetCredits': {'availableCount': 3, 'credits': credits}})
    assert (r.label, r.description, r.count, r.expires_at) == ('Weekly refill', 'Refills it.', 3, NOW + 500)


def grant(id='g1', left=1, **kw):
    return {'id': id, 'resets_left': left, 'paused': False, 'usable_now': True, 'ends_at': None, **kw}


def block(*grants, next_id='g1', eligible=True):
    return {'eligible': eligible, 'grants': list(grants), 'next_grant_id': next_id}


@pytest.mark.parametrize('b', [None, {}, block(grant(), eligible=False), block(grant(), next_id=None),
                               block(grant(paused=True)), block(grant(usable_now=False)),
                               block(grant(ends_at='2020-01-01T00:00:00Z')), block(grant(ends_at='2030-01-01')),
                               block(grant(left=0)), block(grant(id='Bad ID!')), block(grant(left=True))])
def test_claude_resets_none(b):
    assert L.parse_claude_resets(b, NOW) == []


def test_claude_resets_several_kinds():
    b = block(grant('g1', 1, type='weekly', ends_at='2030-01-01T00:00:00Z'),
              grant('g2', 2, clears=['five_hour', 'seven_day'], ends_at='2027-02-01T00:00:00.5+00:00'),
              grant('g9', 1, type='full', ends_at='2027-01-31T00:00:00.5+00:00'),
              grant('g3', 1, clears=['seven_day']),
              grant('g4', 1, gives_back_weekly=True, type='weekly'),
              grant('g5', 1, clears=['five_hour']),
              grant('g6', 5, paused=True, type='full'), grant('g7', 5, usable_now=False, type='full'),
              grant('g8', 5, type='full', ends_at='2026-01-01T00:00:00Z'))
    rs = L.parse_claude_resets(b, NOW)
    assert [(r.kind, r.label, r.count, r.credit_id) for r in rs] == [
        ('session', '5-hour reset', 1, 'g5'), ('weekly', 'Weekly reset', 2, 'g1'),
        ('fable', 'Fable reset', 1, 'g4'), ('full', 'Full reset', 3, 'g2')]
    assert rs[1].expires_at == pytest.approx(1893456000.0) and rs[0].expires_at is None
    assert rs[3].expires_at == pytest.approx(1801353600.5)  # the soonest of its kind, not the first listed
    assert rs[3].description == 'Refills your 5-hour and weekly limits.'


def test_resets_line_and_tooltip():
    rs = [L.Reset('claude', 'weekly', 'Weekly reset', '', 1, 1_900_000_000.0),
          L.Reset('claude', 'full', 'Full reset', '', 1, NOW - 5)]
    line = L.resets_line(rs, NOW)
    assert line.startswith('Free resets: 2 · Weekly reset ×1, Full reset ×1 · first expires ')
    assert L.resets_line([], NOW) is None
    st = L.ProviderState([row(5, 60)], NOW, resets=rs)
    assert L.tooltip('Claude Code', st, NOW).split('\n')[1] == '2 free resets'
    assert 'free reset' not in L.tooltip('Claude Code', L.ProviderState([row(5, 60)], NOW), NOW)


def test_state_with_resets_round_trips_and_old_files_load():
    rs = [L.Reset('claude', 'full', 'Full reset', 'Refills.', 2, NOW + 5, 'g2'), L.Reset('codex', 'codex', 'Codex reset', 'x', 1)]
    st = L.ProviderState([row(5, 60)], NOW, resets=rs)
    assert L.ProviderState.from_json(json.loads(json.dumps(st.to_json()))).resets == rs
    old = {'rows': [{'key': 'k', 'label': 'Weekly', 'used': 5, 'resets_at': None, 'window_mins': None}], 'checked_at': NOW}
    assert L.ProviderState.from_json(old).resets == []
    bad = dict(st.to_json(), resets=[{'provider': 'claude', 'kind': 'full', 'label': 'x', 'description': 'y', 'count': 0}])
    with pytest.raises(TypeError):
        L.ProviderState.from_json(bad)


# Claude's reset calls, with a fake Anthropic. The token below is fake; tests check it never leaks.
TOKEN = 'sk-ant-oat01-FAKE-TOKEN-must-not-leak'


class FakeResp(io.BytesIO):
    def __init__(self, status, raw):
        super().__init__(raw)
        self.status = status


class FakeAnthropic:
    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    def __call__(self, req, timeout):
        self.seen.append((req.get_method(), req.full_url, dict(req.header_items()), req.data, timeout))
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        status, body = a
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, 'nope', {}, io.BytesIO(json.dumps(body or {}).encode()))
        return FakeResp(status, json.dumps(body).encode())


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    (tmp_path / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': TOKEN}}), encoding='utf-8')
    (tmp_path / '.claude.json').write_text(json.dumps({'oauthAccount': {'organizationUuid': 'org-1'}}), encoding='utf-8')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(L, '_version', '2.1.0')
    monkeypatch.setattr(L, '_claude_resets', {'at': None, 'value': [], 'account': None, 'lock': threading.Lock()})
    return tmp_path


def use_http(monkeypatch, *answers):
    f = FakeAnthropic(*answers)
    monkeypatch.setattr(L, '_urlopen', f)
    return f


def usage_body(*grants):
    return {'five_hour': None, 'cedar_ember': block(*grants)}


def test_claude_read_200_sends_t3s_request(claude_home, monkeypatch):
    http = use_http(monkeypatch, (200, usage_body(grant('g1', 2, type='weekly'))))
    [r] = L.claude_resets(NOW)
    assert (r.kind, r.count) == ('weekly', 2)
    method, url, headers, data, timeout = http.seen[0]
    assert (method, url, data, timeout) == ('GET', 'https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1', None, 10)
    assert headers['Authorization'] == f'Bearer {TOKEN}' and headers['Anthropic-beta'] == 'oauth-2025-04-20'
    assert headers['User-agent'] == 'claude-cli/2.1.0 (external, cli)'


def test_claude_read_asks_at_most_every_5_minutes(claude_home, monkeypatch):
    http = use_http(monkeypatch, (200, usage_body(grant())), (200, usage_body()), (200, usage_body()))
    assert len(L.claude_resets(NOW)) == 1
    assert len(L.claude_resets(NOW + 299)) == 1 and len(http.seen) == 1
    assert L.claude_resets(NOW + 300) == [] and len(http.seen) == 2
    L.claude_resets(NOW + 301, force=True)
    assert len(http.seen) == 3


@pytest.mark.parametrize('status', [401, 403])
def test_claude_read_signed_out_shows_none(claude_home, monkeypatch, status):
    use_http(monkeypatch, (200, usage_body(grant())), (status, None))
    assert L.claude_resets(NOW)
    assert L.claude_resets(NOW + 400) == []


@pytest.mark.parametrize('failure', [(429, None), (500, None), TimeoutError('timed out'),
                                     urllib.error.URLError(TimeoutError()), OSError('reset')])
def test_claude_read_429_or_failure_keeps_last(claude_home, monkeypatch, failure):
    use_http(monkeypatch, (200, usage_body(grant())), failure)
    first = L.claude_resets(NOW)
    assert first and L.claude_resets(NOW + 400) == first


def test_claude_read_missing_token_or_version_asks_nothing(claude_home, monkeypatch):
    http = use_http(monkeypatch)
    (claude_home / '.credentials.json').unlink()
    assert L.claude_resets(NOW) == []
    (claude_home / '.credentials.json').write_text('{"claudeAiOauth": {"accessToken": "  "}}', encoding='utf-8')
    assert L.claude_resets(NOW, force=True) == []
    (claude_home / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': TOKEN}}), encoding='utf-8')
    monkeypatch.setattr(L, '_version', '')
    assert L.claude_resets(NOW, force=True) == [] and http.seen == []


def test_claude_call_refuses_other_addresses(claude_home, monkeypatch):
    http = use_http(monkeypatch)
    with pytest.raises(L.ProbeError):
        L._claude_call('GET', 'https://api.anthropic.com.evil.example/x', None, 10)
    assert http.seen == []


@pytest.mark.parametrize('result, outcome', [('reset', 'reset'), ('not_limited', 'nothing'), ('already_used', 'already'),
                                             ('ineligible', 'no_credit'), ('cooldown', 'cooldown'),
                                             ('unavailable', 'unconfirmed'), ('surprise', 'failed')])
def test_claude_spend_outcomes(claude_home, monkeypatch, result, outcome):
    http = use_http(monkeypatch, (200, {'result': result}))
    assert L.use_claude_reset('g1', 'req_1') == outcome
    method, url, headers, data, timeout = http.seen[0]
    assert (method, url, timeout) == ('POST', 'https://api.anthropic.com/api/organizations/org-1/reset_rate_limits', 25)
    assert json.loads(data) == {'program': 'cedar_ember', 'grant_id': 'g1', 'request_id': 'req_1'}
    assert headers['Authorization'] == f'Bearer {TOKEN}'


@pytest.mark.parametrize('answer, outcome', [((429, None), 'rate_limited'), ((401, None), 'signed_out'),
                                             ((403, None), 'signed_out'), ((500, None), 'failed'),
                                             ((404, None), 'refused'), ((400, {'error': 'x'}), 'refused'),
                                             ((409, None), 'failed'),
                                             (TimeoutError('timed out'), 'failed')])
def test_claude_spend_http_failures(claude_home, monkeypatch, answer, outcome):
    use_http(monkeypatch, answer)
    assert L.use_claude_reset('g1', 'req_1') == outcome


def test_claude_spend_checks_ids_and_login_before_sending(claude_home, monkeypatch):
    http = use_http(monkeypatch)
    assert L.use_claude_reset('Bad Grant', 'req_1') == 'failed'
    assert L.use_claude_reset('g1', 'bad id!') == 'failed'
    assert L.use_claude_reset(None, 'req_1') == 'failed'
    (claude_home / '.claude.json').write_text('{"oauthAccount": {}}', encoding='utf-8')
    assert L.use_claude_reset('g1', 'req_1') == 'signed_out'
    (claude_home / '.claude.json').write_text(json.dumps({'oauthAccount': {'organizationUuid': 'org-1'}}), encoding='utf-8')
    (claude_home / '.credentials.json').unlink()
    assert L.use_claude_reset('g1', 'req_1') == 'signed_out' and http.seen == []


def test_use_reset_claude_asks_again_after(claude_home, monkeypatch):
    http = use_http(monkeypatch, (200, {'result': 'reset'}), (200, usage_body()))
    L._claude_resets['at'] = time.time()  # a fresh answer would normally hold off the next read
    assert L.use_reset(L.Reset('claude', 'full', 'Full reset', '', 1, None, 'g1')) == 'reset'
    assert [s[0] for s in http.seen] == ['POST', 'GET'] and L._claude_resets['value'] == []


def test_token_never_in_logs_or_errors(claude_home, monkeypatch, caplog):
    caplog.set_level('DEBUG')

    class Leaky(Exception):
        pass
    for boom in (Leaky(f'failed with header Bearer {TOKEN}'), TimeoutError(TOKEN), OSError(TOKEN)):
        use_http(monkeypatch, boom)
        with pytest.raises(L.ProbeError) as e:
            L._claude_call('GET', L.CLAUDE_USAGE_URL, None, 10)
        assert TOKEN not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__
        use_http(monkeypatch, boom, boom)
        L.claude_resets(NOW, force=True)
        assert L.use_claude_reset('g1', 'req_1') == 'failed'
    for answer in ((401, None), (429, None), (500, None), (200, {'result': 'reset'})):
        use_http(monkeypatch, answer, answer)
        L.claude_resets(NOW, force=True)
        L.use_claude_reset('g1', 'req_1')
    assert caplog.records and TOKEN not in caplog.text


class FakeCodex:
    def __init__(self, answer):
        self.answer, self.sent = answer, []

    def ask(self, build):
        msg, match = build(7)
        self.sent.append(msg)
        if isinstance(self.answer, Exception):
            raise self.answer
        kind, value = match(self.answer)
        if kind == 'error':
            raise L.ProbeError(value)
        return value


@pytest.mark.parametrize('outcome, ours', [('reset', 'reset'), ('nothingToReset', 'nothing'), ('noCredit', 'no_credit'),
                                           ('alreadyRedeemed', 'already'), ('weird', 'failed')])
def test_codex_spend_outcomes(monkeypatch, outcome, ours):
    fake_codex = FakeCodex({'id': 7, 'result': {'outcome': outcome}})
    monkeypatch.setattr(L, 'CODEX', fake_codex)
    assert L.use_codex_reset('key-1') == ours
    assert fake_codex.sent == [{'jsonrpc': '2.0', 'id': 7, 'method': 'account/rateLimitResetCredit/consume',
                                'params': {'idempotencyKey': 'key-1'}}]


@pytest.mark.parametrize('answer', [{'id': 7, 'error': {'message': 'no'}}, L.ProbeError('no answer in 25 s')])
def test_codex_spend_failures(monkeypatch, answer):
    fake_codex = FakeCodex(answer)
    monkeypatch.setattr(L, 'CODEX', fake_codex)
    assert L.use_codex_reset('key-1', 'cr_1') == 'failed'
    assert fake_codex.sent[0]['params'] == {'idempotencyKey': 'key-1', 'creditId': 'cr_1'}


def test_use_reset_codex_makes_a_key(monkeypatch):
    fake_codex = FakeCodex({'id': 7, 'result': {'outcome': 'reset'}})
    monkeypatch.setattr(L, 'CODEX', fake_codex)
    assert L.use_reset(L.Reset('codex', 'codex', 'Codex reset', '', 1)) == 'reset'
    assert len(fake_codex.sent[0]['params']['idempotencyKey']) == 32


def test_real_spend_paths_are_guarded(monkeypatch):
    """tests/conftest.py puts a guard on both real spend paths for every test, and a test's undo() keeps it."""
    monkeypatch.setattr(L, 'CLAUDE_READ_SECS', 1)
    monkeypatch.undo()
    assert L._urlopen.__name__ == 'guarded_open' and L.Session._send.__name__ == 'guarded_send'


def test_claude_resets_forget_the_last_account(claude_home, monkeypatch):
    def sign_in(uuid):
        (claude_home / '.claude.json').write_text(json.dumps({'oauthAccount': {'organizationUuid': 'org-1',
                                                                               'accountUuid': uuid}}), encoding='utf-8')
    sign_in('a')
    http = use_http(monkeypatch, (200, usage_body(grant())), (429, None))
    assert L.claude_resets(NOW)
    sign_in('b')  # another account, a minute later: asked at once, and the old resets aren't shown meanwhile
    assert L.claude_resets(NOW + 60) == [] and len(http.seen) == 2


def test_used_claude_reset_leaves_the_cache_even_when_asking_again_fails(claude_home, monkeypatch):
    use_http(monkeypatch, (200, usage_body(grant('g1', 2, type='weekly'))), (200, {'result': 'reset'}), (429, None))
    assert [r.count for r in L.claude_resets(NOW)] == [2]
    assert L.use_reset(L.Reset('claude', 'weekly', 'Weekly reset', '', 2, None, 'g1'), 'req_1') == 'reset'
    assert [r.count for r in L._claude_resets['value']] == [1]
    assert L.without_one(L._claude_resets['value'], 'weekly') == []
