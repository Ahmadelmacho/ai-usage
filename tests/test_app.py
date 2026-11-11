"""Runs the real app (window + both tray icons) with fake checks, then closes it."""
import json
import os
import threading
import time

import pytest

from usage_tray import app as A, limits as L

NOW = time.time()


@pytest.fixture
def fake_checks(monkeypatch, tmp_path):
    calls = {'claude': 0, 'codex': 0}
    answers = {
        'claude': lambda: [L.Row('seven_day', 'Weekly', 30, NOW + 86400 * 5, 10080),
                           L.Row('seven_day_sonnet', 'Weekly · Sonnet', 12, NOW + 86400 * 5, 10080)],
        'codex': lambda: [L.Row('codex:primary', 'Session', 80, NOW + 3600, 300),
                          L.Row('codex:secondary', 'Weekly', 60, NOW + 86400 * 5, 10080)],
    }

    def make(p):
        def fetch():
            calls[p] += 1
            time.sleep(0.2)
            return answers[p]()
        return fetch

    monkeypatch.setattr(L, 'fetch_claude', make('claude'))
    monkeypatch.setattr(L, 'fetch_codex', make('codex'))
    monkeypatch.setattr(L, 'close_all', lambda: None)
    monkeypatch.setattr(L, 'fetch_claude_dated', lambda: (L.fetch_claude(), time.time()))  # not ~/.claude.json
    monkeypatch.setattr(A, 'CACHE', str(tmp_path / 'last.json'))
    monkeypatch.setattr(A, 'REFRESH_SECS', 60)  # keep the automatic checks out of the scripted steps below
    monkeypatch.setattr(A.update, 'check', lambda **kw: None)  # never ask the real GitHub
    return calls, answers, tmp_path


def drive(app, steps):
    """Run (delay_ms, fn) steps on the Tk loop, collecting failures, then quit."""
    errors = []

    def wrap(fn):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - reported below
                errors.append(e)
        return run

    t = 0
    for delay, fn in steps:
        t += delay
        app.root.after(t, wrap(fn))
    app.root.after(t + 100, lambda: app.q.put('quit'))
    app.run()
    if errors:
        raise errors[0]


def test_full_cycle(fake_checks, monkeypatch):
    calls, answers, tmp = fake_checks
    app = A.App(start_hidden=True)
    seen = {}

    def after_first_check():
        assert calls == {'claude': 1, 'codex': 1}
        assert app.shown['claude'][:2] == ('70', 'ok') and app.shown['codex'][:2] == ('20', 'amber')
        assert app.icons['claude'].title.startswith('Claude Code · checked ')
        assert 'Weekly  70% left · 4d 23h' in app.icons['claude'].title
        assert 'Session  20% left · 59m' in app.icons['codex'].title
        assert all(ic.visible for ic in app.icons.values())
        saved = json.loads((tmp / 'last.json').read_text())
        assert [r['used'] for r in saved['codex']['rows']] == [80, 60]
        assert app.root.state() == 'withdrawn'  # started hidden
        line = app.log_health()
        assert 'claude: last 0 min 1 ok, 0 failed, avg 0.2' in line and 'codex: last 0 min 1 ok' in line
        assert ' MB, ' in line.split('app: ')[1] and 'GDI' in line
        assert app.counts['claude'] == A.App.new_counts()  # counted afresh for the next line

    def open_it():
        app.q.put('open')

    def check_open():
        assert app.root.state() == 'normal'
        assert app.label.cget('image')
        seen['h'] = app.root.winfo_height()
        assert seen['h'] > 300 and app.root.winfo_width() >= A.draw.W_MIN

    def maximize():
        app.root.state('zoomed')

    def check_maximized_fills_screen():
        assert app.root.state() == 'zoomed'
        app.root.update_idletasks()
        img_w, img_h = app._photo.width(), app._photo.height()
        assert img_h >= app.root.winfo_height() - 2 and img_w >= app.root.winfo_width() - 2  # no half-empty window
        assert img_h > seen['h']
        x0, _, x1, _ = app.hits['refresh']
        assert x1 < img_w and x0 > img_w // 2  # refresh button follows the centered column
        app.root.state('normal')

    def click_refresh():
        x0, y0, x1, y1 = app.hits['refresh']
        app.label.event_generate('<Button-1>', x=(x0 + x1) // 2, y=(y0 + y1) // 2)

    def check_second_check():
        assert calls == {'claude': 2, 'codex': 2}

    def close_window():
        app.root.protocol('WM_DELETE_WINDOW')  # registered handler exists
        app.root.withdraw()

    def failing_codex():
        def boom():
            raise L.ProbeError('no answer in 25 s')
        monkeypatch.setattr(L, 'fetch_codex', boom)
        app.q.put('refresh')

    def check_failure_keeps_old_numbers():
        st = app.states['codex']
        assert st.error == 'no answer in 25 s' and len(st.rows) == 2
        assert app.shown['codex'][0] == '20'
        assert 'check failed: no answer in 25 s' in app.icons['codex'].title
        assert app.sched['codex']['fails'] == 1 and app.sched['claude']['fails'] == 0
        assert app.interval('codex') == 120 and app.interval('claude') == 60  # only Codex backs off
        assert app.counts['codex']['failed'] == 1

    def broken_render():
        def boom(*a, **k):
            raise RuntimeError('drawing broke')
        monkeypatch.setattr(A.draw, 'render_window', boom)
        app.drawn = None
        app.q.put('open')  # handler raises inside poll()

    def menu_still_works_after_an_error():
        assert app.drawn is None  # the failed drawing is not remembered as done...
        monkeypatch.undo()  # restores render_window (and the fakes; no more checks run from here)
        assert app.root.tk.call('after', 'info')  # poll() and tick() are still scheduled
        app.render()
        assert app.drawn is not None  # ...so the next try draws it

    drive(app, [(1500, after_first_check), (10, open_it), (500, check_open), (10, maximize),
                (600, check_maximized_fills_screen), (300, click_refresh),
                (1200, check_second_check), (10, close_window), (10, failing_codex), (1200, check_failure_keeps_old_numbers),
                (10, broken_render), (400, menu_still_works_after_an_error)])
    assert all(not t.is_alive() for t in app.icon_threads)  # Close removed both tray icons


class Bare:
    """Just the state App.load_cache works on (Tk can only start once per test run reliably)."""
    def __init__(self):
        self.states = {'codex': L.ProviderState(), 'claude': L.ProviderState()}


def test_starts_from_saved_numbers(fake_checks):
    calls, answers, tmp = fake_checks
    st = L.ProviderState(answers['claude'](), NOW - 600)
    (tmp / 'last.json').write_text(json.dumps({'claude': st.to_json(), 'codex': {'rows': 'junk'}}))
    b = Bare()
    A.App.load_cache(b)
    assert b.states['claude'].rows == st.rows and b.states['claude'].checked_at == NOW - 600
    assert b.states['codex'].rows == []


@pytest.mark.parametrize('content', ['{not json', '[]', '{"claude": 5}', ''])
def test_broken_cache_is_ignored(fake_checks, content):
    _, _, tmp = fake_checks
    (tmp / 'last.json').write_text(content)
    b = Bare()
    A.App.load_cache(b)
    assert b.states['claude'].rows == [] and b.states['codex'].rows == []


def test_second_copy_opens_the_first_ones_window():
    import queue
    NAME = f'AIUsageTest{os.getpid()}'  # the installed app may be running with the real name
    first = A.single_instance(show_existing=True, name=NAME)
    assert first  # first copy
    assert A.single_instance(show_existing=True, name=NAME) is None  # a second copy before the listener starts...
    q = queue.Queue()
    A.listen_for_show(q, first[1])
    assert q.get(timeout=2) == 'open'  # ...is still not lost
    assert A.single_instance(show_existing=True, name=NAME) is None
    assert q.get(timeout=2) == 'open'
    assert A.single_instance(show_existing=False, name=NAME) is None  # a hidden (login) start just exits quietly
    with pytest.raises(queue.Empty):
        q.get(timeout=0.5)
    for h in first:
        A.kernel32.CloseHandle(h)


def test_backoff_doubles_to_five_minutes():
    class S:
        sched = {'claude': {'fails': 0}}
    for fails, secs in ((0, 5), (1, 10), (2, 20), (5, 160), (6, 300), (50, 300)):
        S.sched['claude']['fails'] = fails
        assert A.App.interval(S, 'claude') == secs


def test_update_shows_only_when_newer(fake_checks, monkeypatch):
    started = []
    monkeypatch.setattr(A.update, 'start', lambda: started.append(1))
    app = A.App(start_hidden=True)

    def menu_texts():
        return [str(i) for i in app.icons['claude'].menu.items if i.visible and i is not A.Menu.SEPARATOR]

    def nothing_yet():
        app.q.put('open')
        assert menu_texts() == ['Check for updates', 'Refresh', 'Open', 'Close']

    def no_link():
        assert 'update' not in app.hits and 'refresh' in app.hits
        app.q.put(('newer', 'v9.0.0'))

    def shows_update():
        assert menu_texts()[0] == 'Update to v9.0.0'
        assert [str(i) for i in app.icons['codex'].menu.items if i.visible][0] == 'Update to v9.0.0'
        x0, y0, x1, y1 = app.hits['update']
        assert x1 < app.hits['refresh'][0]  # by the title, left of the refresh button
        app.label.event_generate('<Button-1>', x=(x0 + x1) // 2, y=(y0 + y1) // 2)

    def link_click_starts_updater():
        assert started == [1]
        app.icons['claude'].menu.items[0](app.icons['claude'])  # the tray menu item, as pystray calls it

    def menu_click_starts_updater():
        assert started == [1, 1]

    drive(app, [(300, nothing_yet), (600, no_link), (400, shows_update), (400, link_click_starts_updater),
                (400, menu_click_starts_updater)])


def test_watch_updates_waits_then_repeats(monkeypatch):
    import queue
    found = iter([None, 'v2.0.0'])
    monkeypatch.setattr(A.update, 'check', lambda: next(found))

    class Stop:  # records each wait instead of sleeping: a timed wait on Windows can end a clock tick early
        waits = []

        def wait(self, secs):
            self.waits.append(secs)
            return len(self.waits) > 2  # stop after the second check
    fake = type('Fake', (), {'q': queue.Queue()})()
    A.App.watch_updates(fake, first=60, every=6 * 3600, stop=Stop())
    assert Stop.waits == [60, 6 * 3600, 6 * 3600]  # waits before the first check, then between checks
    assert fake.q.get_nowait() == ('newer', 'v2.0.0') and fake.q.empty()


def test_second_update_click_waits_for_the_first(fake_checks, monkeypatch):
    running = type('Proc', (), {'poll': lambda self: None})()
    started = []
    monkeypatch.setattr(A.update, 'start', lambda: started.append(1) or running)
    app = A.App(start_hidden=True)

    def clicks():
        app.update_tag = 'v9.0.0'
        app.handle('update')
        app.handle('update')
        assert started == [1]
        running.poll = lambda: 0  # the update window closed (e.g. the install failed): a click tries again
        app.handle('update')
        assert started == [1, 1]

    drive(app, [(100, clicks)])


def test_menu_rebuild_waits_while_the_menu_is_open(monkeypatch):
    built = []
    icon = A.TrayIcon(9, 'ai-usage-test', A.draw.render_tray('1', 'ok', 'claude', 16), 'test',
                      A.Menu(A.MenuItem('Close', lambda: None)))
    monkeypatch.setattr(icon, '_update_menu', lambda: built.append(1))

    def open_menu(self, wparam, lparam):  # stands in for TrackPopupMenuEx, which delivers posted messages
        self._on_rebuild_menu(0, 0)
        assert built == []  # the open menu is not destroyed under the user
    monkeypatch.setattr(A.Win32Icon, '_on_notify', open_menu)
    icon._on_notify(0, 0)
    assert built == [1]  # rebuilt once it closed
    icon._on_rebuild_menu(0, 0)
    assert built == [1, 1]


def test_check_for_updates_by_hand(fake_checks, monkeypatch):
    """Menu item and window button: one check at a time, off the window thread, then a short note or the update."""
    gate, calls = threading.Event(), []
    results = iter([None, OSError('offline'), 'v9.0.0'])

    def check(strict=False):
        calls.append(threading.current_thread() is threading.main_thread())
        gate.wait(5)
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(A.update, 'check', check)
    monkeypatch.setattr(A, 'NOTE_MS', 600)
    app = A.App(start_hidden=True)
    item = next(i for i in app.icons['claude'].menu.items if str(i) == 'Check for updates')

    def button_text():
        return app.check_text() if 'check' in app.hits else None

    def click_button():
        x0, y0, x1, y1 = app.hits['check']
        app.label.event_generate('<Button-1>', x=(x0 + x1) // 2, y=(y0 + y1) // 2)

    def menu_click():
        app.q.put('open')
        item(app.icons['claude'])  # as pystray calls it

    def checking():
        assert calls == [False]  # ran once, not on the window thread
        assert str(item) == 'Checking…' and button_text() == 'Checking…'
        click_button()
        item(app.icons['codex'])  # second clicks while it runs

    def release():
        gate.set()

    def up_to_date():
        assert calls == [False]  # the second clicks did nothing
        assert str(item) == f'Up to date (v{A.__version__})' == button_text()

    def back_to_idle():
        assert str(item) == 'Check for updates' == button_text()
        click_button()

    def offline():
        assert len(calls) == 2
        assert str(item) == "Couldn't check" == button_text()

    def offline_again_idle():
        assert button_text() == 'Check for updates'
        click_button()

    def newer():
        assert len(calls) == 3
        assert app.update_tag == 'v9.0.0' and 'update' in app.hits and 'check' not in app.hits  # link takes its place
        assert [str(i) for i in app.icons['claude'].menu.items if i.visible][:2] == ['Update to v9.0.0', 'Check for updates']

    drive(app, [(300, menu_click), (400, checking), (200, release), (400, up_to_date), (700, back_to_idle),
                (400, offline), (700, offline_again_idle), (400, newer)])
