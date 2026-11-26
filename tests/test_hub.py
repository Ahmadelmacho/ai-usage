import io
import json
import threading
import time
import urllib.error

import pytest
from PIL import ImageChops

from usage_tray import app as A, draw, limits as L, settings as S

NOW = 1_800_000_000.0
KEY = 'mgmt-secret-key'
WEEK = {'utilization': 97, 'resets_at': '2099-01-01T00:00:00Z'}
CLAUDE_A = {'five_hour': {'utilization': 10, 'resets_at': '2099-01-01T00:00:00Z'}, 'seven_day': WEEK,
            'limits': [{'kind': 'weekly_scoped', 'percent': 68, 'resets_at': None,
                        'scope': {'model': {'display_name': 'Fable'}}}, {'kind': 'other'}, 'junk']}
CLAUDE_B = {'seven_day': {'utilization': 47, 'resets_at': None}}
CODEX = {'plan_type': 'plus', 'rate_limit': {
    'primary_window': {'used_percent': 10, 'reset_at': NOW + 3600, 'limit_window_seconds': 18000},
    'secondary_window': {'used_percent': 66, 'reset_at': NOW + 86400, 'limit_window_seconds': 604800}}}
FILES = [{'id': 'a.json', 'provider': 'claude', 'email': 'a@example.com', 'auth_index': 'i1'},
         {'id': 'b.json', 'provider': 'claude', 'email': 'b@example.com', 'auth_index': 'i2'},
         {'id': 'off.json', 'provider': 'claude', 'auth_index': 'i3', 'disabled': True},
         {'id': 'c.json', 'provider': 'codex', 'email': 'c@example.com', 'auth_index': 'i4',
          'id_token': {'chatgpt_account_id': ' acct-9 '}},
         {'id': 'g.json', 'provider': 'gemini', 'auth_index': 'i5'}]


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeHub:
    def __init__(self, usage, files=FILES, list_status=200):
        self.usage, self.files, self.list_status, self.sent = usage, files, list_status, []

    def __call__(self, req, timeout):
        body = json.loads(req.data) if req.data else None
        self.sent.append((req.get_method(), req.full_url, dict(req.header_items()), body))
        if req.full_url.endswith('/auth-files'):
            if self.list_status != 200:
                raise urllib.error.HTTPError(req.full_url, self.list_status, 'no', {}, io.BytesIO(b''))
            return Reply(json.dumps({'files': self.files}).encode())
        status, answer = self.usage[body['auth_index']]
        return Reply(json.dumps({'status_code': status, 'body': json.dumps(answer)}).encode())

    def asked(self):
        return [body['auth_index'] for _, url, _, body in self.sent if url.endswith('/api-call')]


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setattr(L, '_version', '2.1.0')
    monkeypatch.setattr(L, '_hub', {'source': None, 'at': None, 'value': {}, 'errors': {}, 'failed': None,
                                    'lock': threading.Lock()})

    def use(usage, **kw):
        fake = FakeHub(usage, **kw)
        monkeypatch.setattr(L, '_urlopen', fake)
        return fake
    return use


@pytest.mark.parametrize('url, base', [
    ('http://localhost:8317/management.html#/', 'http://localhost:8317'),
    (' https://hub.example/x/ ', 'https://hub.example/x'), ('localhost:8317', None), ('ftp://hub', None),
    ('http://user:pw@hub', None), ('http://', None), ('', None), (None, None)])
def test_hub_base(url, base):
    assert L.hub_base(url) == base


def test_pool_averages_each_limit_and_orders_them():
    a = L.Account([L.Row('seven_day', 'Weekly', 97, NOW + 500, 10080), L.Row('five_hour', 'Session', 10, NOW + 90, 300)])
    b = L.Account([L.Row('seven_day', 'Weekly', 47, NOW + 900, 10080)])
    done = L.Account([L.Row('seven_day', 'Weekly', 80, NOW - 5, 10080)])
    rows = L.pool([a, b], NOW)
    assert [(r.key, r.used, r.resets_at) for r in rows] == [('five_hour', 10, NOW + 90), ('seven_day', 72, NOW + 500)]
    assert L.pool([b, done], NOW)[0].used == 23.5 and L.pool([b, done], NOW)[0].resets_at == NOW + 900
    assert L.tray_row('claude', rows, [a, b]).key == 'seven_day'
    assert L.tray_row('claude', rows, [a]).key == 'five_hour'
    assert L.tray_row('claude', rows).key == 'five_hour'


def test_hub_rows_use_the_same_names_as_the_local_ones():
    assert [(r.key, r.label, r.used) for r in L.hub_claude_rows(CLAUDE_A)] == [
        ('five_hour', 'Session', 10), ('seven_day', 'Weekly', 97), ('seven_day_fable', 'Weekly · Fable', 68)]
    assert [(r.key, r.label, r.used, r.resets_at, r.window_mins) for r in L.hub_codex_rows(CODEX)] == [
        ('codex:300', 'Session', 10, NOW + 3600, 300), ('codex:10080', 'Weekly', 66, NOW + 86400, 10080)]
    with pytest.raises(L.ProbeError):
        L.hub_codex_rows({'rate_limit': None})
    with pytest.raises(L.ProbeError):
        L.hub_claude_rows({})


def test_hub_read_asks_each_enabled_account_once_for_both_providers(hub):
    fake = hub({'i1': (200, CLAUDE_A), 'i2': (200, CLAUDE_B), 'i4': (200, CODEX)})
    claude, at, error = L.hub_accounts('http://localhost:8317/management.html', KEY, 'claude')
    codex, at2, _ = L.hub_accounts('http://localhost:8317', KEY, 'codex')
    assert [a.who['email'] for a in claude] == ['a@example.com', 'b@example.com'] and at == at2 and error is None
    assert [len(a.rows) for a in claude] == [3, 1] and codex[0].who['plan'] == 'ChatGPT Plus Subscription'
    assert sorted(fake.asked()) == ['i1', 'i2', 'i4']
    for method, url, headers, body in fake.sent:
        assert url.startswith('http://localhost:8317/v0/management/') and headers['Authorization'] == f'Bearer {KEY}'
        if body:
            assert body['method'] == 'GET' and body['url'] in L.HUB_USAGE.values()
            assert body['header']['Authorization'] == 'Bearer $TOKEN$' and KEY not in json.dumps(body)
    sent = {body['auth_index']: body['header'] for _, _, _, body in fake.sent if body}
    assert sent['i1']['User-Agent'] == 'claude-cli/2.1.0 (external, cli)'
    assert sent['i4']['Chatgpt-Account-Id'] == 'acct-9' and 'Chatgpt-Account-Id' not in sent['i2']
    assert 'example.com' not in repr(claude) and KEY not in repr(L._hub['value'])


def test_hub_read_waits_five_minutes_unless_forced(hub, monkeypatch):
    fake = hub({'i1': (200, CLAUDE_A), 'i2': (200, CLAUDE_B), 'i4': (200, CODEX)})
    clock = {'t': NOW}
    monkeypatch.setattr(L.time, 'time', lambda: clock['t'])
    L.hub_accounts('http://h', KEY, 'claude')
    clock['t'] += 5
    L.hub_accounts('http://h', KEY, 'codex', force=True)
    clock['t'] += L.HUB_READ_SECS - 10
    L.hub_accounts('http://h', KEY, 'claude')
    assert len(fake.asked()) == 3
    clock['t'] += 20
    assert L.hub_accounts('http://h', KEY, 'claude')[1] == clock['t'] and len(fake.asked()) == 6
    clock['t'] += 30
    L.hub_accounts('http://h', KEY, 'claude', force=True)
    assert len(fake.asked()) == 9
    L.hub_accounts('http://other', KEY, 'claude')
    assert len(fake.asked()) == 12


def test_an_account_that_cannot_be_read_is_never_silent(hub):
    usage = {'i1': (200, CLAUDE_A), 'i2': (429, {}), 'i4': (429, {})}
    hub(usage)
    first, _, error = L.hub_accounts('http://h', KEY, 'claude')
    assert len(first) == 1 and error == 'Anthropic answered 429 through the hub'
    with pytest.raises(L.ProbeError, match='OpenAI answered 429 through the hub'):
        L.hub_accounts('http://h', KEY, 'codex')
    usage['i2'] = (200, CLAUDE_B)
    L._hub['at'] = 0
    both, _, error = L.hub_accounts('http://h', KEY, 'claude')
    assert len(both) == 2 and error is None
    usage['i1'] = usage['i2'] = (401, {})
    L._hub['at'] = 0
    kept, _, error = L.hub_accounts('http://h', KEY, 'claude')
    assert [a.rows for a in kept] == [a.rows for a in both] and error == 'Anthropic answered 401 through the hub'
    kept[1].rows[0].resets_at = time.time() - 1
    L._hub['at'] = 0
    kept, _, error = L.hub_accounts('http://h', KEY, 'claude')
    assert [a.rows for a in kept] == [both[0].rows] and error


def test_only_a_far_http_hub_is_unprotected():
    assert L.hub_unprotected('http://hub.example:8317/management.html')
    assert not any(L.hub_unprotected(u) for u in ('https://hub.example', 'http://localhost:8317', 'http://127.0.0.1:8317'))


def test_a_failed_forced_read_is_not_hidden_by_the_last_good_one(hub, monkeypatch):
    fake = hub({'i1': (200, CLAUDE_A), 'i2': (200, CLAUDE_B), 'i4': (200, CODEX)})
    clock = {'t': NOW}
    monkeypatch.setattr(L.time, 'time', lambda: clock['t'])
    L.hub_accounts('http://h', KEY, 'claude')
    fake.list_status = 500
    for step, lists in ((20, 2), (25, 2), (31, 3)):
        clock['t'] = NOW + step
        with pytest.raises(L.ProbeError, match='the hub answered 500'):
            L.hub_accounts('http://h', KEY, 'claude', force=step == 20)
        assert sum(url.endswith('/auth-files') for _, url, _, _ in fake.sent) == lists


def test_many_accounts_still_get_bars():
    bars = draw.account_bars(237, 483, [L.Account([L.Row('k', 'Weekly', 5)])] * 50, 'k')
    assert len(bars) == 50 and all(x1 > x0 for x0, x1, _, _ in bars) and bars[-1][1] == pytest.approx(483)


def test_a_wrong_key_is_not_tried_again_for_five_minutes(hub, monkeypatch):
    fake = hub({}, list_status=401)
    clock = {'t': NOW}
    monkeypatch.setattr(L.time, 'time', lambda: clock['t'])
    for step in (0, 60, 200):
        clock['t'] = NOW + step
        with pytest.raises(L.HubRefused, match='refused the management key') as e:
            L.hub_accounts('http://h', KEY, 'claude')
        assert KEY not in str(e.value)
    assert len(fake.sent) == 1
    with pytest.raises(L.HubRefused):
        L.hub_accounts('http://h', KEY, 'codex', force=True)
    assert len(fake.sent) == 2


def test_hub_failures_say_so_without_the_key(hub, monkeypatch):
    hub({}, list_status=500)
    with pytest.raises(L.ProbeError, match='the hub answered 500'):
        L.hub_accounts('http://h', KEY, 'claude')

    def down(req, timeout):
        raise OSError(f'refused {req.headers}')
    monkeypatch.setattr(L, '_urlopen', down)
    with pytest.raises(L.ProbeError) as e:
        L.hub_accounts('http://h2', KEY, 'claude')
    assert str(e.value) == 'the hub did not answer' and e.value.__cause__ is None
    with pytest.raises(L.ProbeError, match='address or key is missing'):
        L.hub_accounts('nope', KEY, 'claude')


def test_key_is_locked_to_this_windows_login(tmp_path):
    locked = S.lock(KEY)
    assert KEY not in locked and S.unlock(locked) == KEY
    assert S.unlock('') == '' and S.unlock('not base64 !') == '' and S.unlock('AAAA') == ''
    path = str(tmp_path / 's.json')
    S.save(path, {**S.DEFAULTS, 'hub_url': 'http://h', 'hub_key': locked})
    assert KEY not in (tmp_path / 's.json').read_text() and S.unlock(S.load(path)['hub_key']) == KEY
    S.save(path, {**S.DEFAULTS, 'hub_url': 'http://h'})
    assert S.load(path) == S.DEFAULTS
    S.save(path, S.DEFAULTS)
    assert 'hub' not in (tmp_path / 's.json').read_text()


def test_window_draws_one_bar_for_each_account():
    a = L.Account([L.Row('five_hour', 'Session', 10, NOW + 900, 300), L.Row('seven_day', 'Weekly', 97, NOW + 9000, 10080)])
    b = L.Account([L.Row('seven_day', 'Weekly', 47, NOW + 90000, 10080)])
    st = {'claude': L.ProviderState(L.pool([a, b], NOW), NOW - 200, accounts=[a, b]),
          'codex': L.ProviderState([L.Row('codex:primary', 'Session', 30, NOW + 900, 300)], NOW - 20)}
    plain = {'claude': L.ProviderState(L.pool([a, b], NOW), NOW - 20), 'codex': st['codex']}
    img, hits = draw.render_window(st, NOW, 700)
    bars = sorted(n for n in hits if n.startswith('bar:'))
    assert bars == ['bar:claude:five_hour@0', 'bar:claude:seven_day@0', 'bar:claude:seven_day@1', 'bar:codex:codex:primary']
    left, right = hits['bar:claude:seven_day@0'], hits['bar:claude:seven_day@1']
    assert left[2] < right[0] and hits['bar:claude:five_hour@0'][::2] == left[::2]
    assert right[2] == hits['bar:codex:codex:primary'][2]
    assert img.size == draw.render_window(plain, NOW, 700)[0].size and not L.stale(st['claude'], NOW)
    assert L.stale(L.ProviderState(st['claude'].rows, NOW - 200), NOW)
    gap = ((left[2] + right[0]) // 2, (left[1] + left[3]) // 2)
    assert img.getpixel(gap) == (0, 0, 0) and draw.render_window(plain, NOW, 700)[0].getpixel(gap) != (0, 0, 0)


def test_app_shows_hub_accounts_and_goes_back_without_a_hub(monkeypatch, tmp_path):
    a = L.Account([L.Row('five_hour', 'Session', 10, time.time() + 900, 300),
                   L.Row('seven_day', 'Weekly', 97, time.time() + 9000, 10080)], {'email': 'a@example.com', 'plan': None})
    b = L.Account([L.Row('seven_day', 'Weekly', 47, time.time() + 90000, 10080)], {'email': 'b@example.com', 'plan': None})
    asked = []
    monkeypatch.setattr(L, 'hub_accounts', lambda url, key, p, force=False: (
        asked.append((url, key, p)) or ([a, b] if p == 'claude' else []), time.time() - 200, None))
    monkeypatch.setattr(A, 'fetch_codex', lambda: ([L.Row('codex:primary', 'Session', 80, time.time() + 3600, 300)], [], None))
    monkeypatch.setattr(L, 'fetch_claude_dated', lambda: ([L.Row('seven_day', 'Weekly', 30, None, 10080)], time.time()))
    monkeypatch.setattr(L, 'claude_resets', lambda force=False: [])
    monkeypatch.setattr(L, 'claude_who', lambda g: None)
    monkeypatch.setattr(L, 'close_all', lambda: None)
    monkeypatch.setattr(L.CLAUDE, 'close', lambda: asked.append('closed'))
    monkeypatch.setattr(A, 'CACHE', str(tmp_path / 'last.json'))
    monkeypatch.setattr(A, 'SETTINGS', str(tmp_path / 'settings.json'))
    monkeypatch.setattr(A, 'REFRESH_SECS', 60)
    monkeypatch.setattr(A.update, 'check', lambda **kw: None)
    S.save(A.SETTINGS, {**S.DEFAULTS, 'hub_url': 'http://localhost:8317/management.html', 'hub_key': S.lock(KEY)})
    app = A.App(start_hidden=True)
    errors = []

    def step(fn):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        return run

    def hub_numbers():
        assert ('http://localhost:8317/management.html', KEY, 'claude') in asked and 'closed' in asked
        st = app.states['claude']
        assert st.accounts == [a, b] and [r.key for r in st.rows] == ['five_hour', 'seven_day']
        assert app.shown['claude'][0] == '28' and app.shown['codex'][0] == '20'
        assert 'checked' in app.icons['claude'].title and app.states['codex'].accounts == []
        assert KEY not in (tmp_path / 'settings.json').read_text()
        assert 'example.com' not in (tmp_path / 'last.json').read_text()
        app.q.put('open')

    def hub_card():
        assert 'bar:claude:seven_day@1' in app.hits and 'bar:claude:seven_day' not in app.hits
        app.open_card('bar:claude:seven_day@1')
        m = draw.card_model('claude', L.ProviderState(rows=b.rows, account=b.who), b.rows[0], time.time(),
                            'localhost:8317')
        assert app.card_cache[0][1] == repr(m) and m['email'] == 'b@example.com' and m['host'] == 'localhost:8317'
        app.close_card(at_once=True)
        n, st = len(asked), app.states['claude']
        app.handle(('result', 'claude', [L.Row('seven_day', 'Weekly', 1)], None, time.time(), 0.1, [], None, None, [],
                    ('http://another-hub', KEY)))
        assert st.accounts == [a, b] and app.sched['claude']['busy']
        app.handle(('result', 'claude', L.pool([a], time.time()), 'Anthropic answered 429 through the hub',
                    time.time(), 0.1, [], None, None, [a], app.hub))
        assert st.accounts == [a] and st.error.startswith('Anthropic answered 429') and app.sched['claude']['fails'] == 0
        assert 'check failed: Anthropic answered 429' in app.icons['claude'].title
        app.open_settings()
        app.save_hub(remove=True)

    def local_again():
        assert app.hub is None and app.states['claude'].accounts == []
        assert app.shown['claude'][0] == '70' and 'bar:claude:seven_day' in app.hits
        assert 'hub' not in (tmp_path / 'settings.json').read_text()
        n = len(asked)
        app._hub_url.set('localhost:8317')
        app.save_hub()
        assert app.hub is None and app.hub_note.startswith('The address must start with')
        app._hub_url.set('http://localhost:8317')
        app.save_hub()
        assert app.hub is None and app.hub_note == "Paste the hub's management key"
        assert len(asked) == n and not app.busy()
        app._hub_key.set(KEY)
        app.save_hub()
        assert app.hub == ('http://localhost:8317', KEY) and app.hub_note == 'Saved.'

    def hub_back():
        assert app.states['claude'].accounts == [a, b] and app.shown['claude'][0] == '28'

    for i, fn in enumerate((hub_numbers, hub_card, local_again, hub_back)):
        app.root.after(1200 * (i + 1), step(fn))
    app.root.after(1200 * 5, lambda: app.q.put('quit'))
    app.run()
    A.draw.use_palette(False)
    if errors:
        raise errors[0]


@pytest.mark.parametrize('scale', [1.0, 1.25])
def test_a_hovered_button_is_drawn_in_place_with_hub_bars(scale):
    a = L.Account([L.Row('five_hour', 'Session', 10, NOW + 900, 300), L.Row('seven_day', 'Weekly', 97, NOW + 9000, 10080)])
    b = L.Account([L.Row('seven_day', 'Weekly', 47, NOW + 90000, 10080)])
    st = {'claude': L.ProviderState(L.pool([a, b], NOW), NOW, accounts=[a, b]),
          'codex': L.ProviderState([L.Row('codex:primary', 'Session', 30, NOW + 900, 300)], NOW)}
    hover = {'settings': 1.0, 'refresh': 1.0}
    parts = []
    got, _ = draw.render_window(st, NOW, 680, scale, fx={}, parts=parts)
    want, _ = draw.render_window(st, NOW, 680, scale, fx=hover)
    for part in parts:
        if set(part[0]) & set(hover):
            patch, at = draw.render_part(part, hover)
            got.paste(patch, at)
    assert max(hi for _, hi in ImageChops.difference(got, want).getextrema()) <= 2
