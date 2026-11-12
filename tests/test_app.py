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
    fetch_codex = make('codex')
    resets = answers['resets'] = {'claude': [], 'codex': []}  # free resets the fakes report; tests fill them
    forced = answers['forced'] = []  # each Claude resets read: was it forced?
    monkeypatch.setattr(A, 'fetch_codex', lambda: (fetch_codex(), list(resets['codex'])))
    monkeypatch.setattr(L, 'claude_resets', lambda force=False: forced.append(force) or list(resets['claude']))
    monkeypatch.setattr(L, 'close_all', lambda: None)
    monkeypatch.setattr(L, 'fetch_claude_dated', lambda: (L.fetch_claude(), time.time()))  # not ~/.claude.json
    monkeypatch.setattr(A, 'CACHE', str(tmp_path / 'last.json'))
    monkeypatch.setattr(A, 'REFRESH_SECS', 60)  # keep the automatic checks out of the scripted steps below
    monkeypatch.setattr(A.update, 'check', lambda **kw: None)  # never ask the real GitHub
    monkeypatch.setattr(A, 'SETTINGS', str(tmp_path / 'settings.json'))
    yield calls, answers, tmp_path
    A.draw.use_palette(False)  # a test that switched to Light must not leak it into the next


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
        monkeypatch.setattr(A, 'fetch_codex', boom)
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
        assert menu_texts() == ['Check for updates', 'Settings…', 'Refresh', 'Open', 'Close']

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


def test_settings_window_theme_and_tray_colors(fake_checks, monkeypatch):
    """Gear button and tray menu open one Settings window; theme and colors apply at once and are saved."""
    _, _, tmp = fake_checks
    windows = {'light': True}
    monkeypatch.setattr(A.S, 'windows_light', lambda: windows['light'])
    app = A.App(start_hidden=True)
    tops = lambda: [w for w in app.root.winfo_children() if isinstance(w, A.tk.Toplevel)]

    def click_gear():
        app.q.put('open')
        app.root.after(300, lambda: app.label.event_generate('<Button-1>', x=sum(app.hits['settings'][::2]) // 2,
                                                             y=sum(app.hits['settings'][1::2]) // 2))

    def gear_opened_settings():
        assert app.settings_open() and len(tops()) == 1
        assert A.draw.BG == A.draw.DARK['BG'] and app.settings_win.cget('bg') == A.draw.DARK['BG']
        next(i for i in app.icons['claude'].menu.items if str(i) == 'Settings…')(app.icons['claude'])

    def menu_raised_the_same_window():
        assert len(tops()) == 1
        app.set_pref('theme', 'system')  # Windows says light (faked)

    def follows_windows():
        app.root.update_idletasks()  # the Settings rebuild waits for idle time
        assert A.draw.BG == A.draw.LIGHT['BG'] and app.label.cget('bg') == A.draw.LIGHT['BG']
        assert app.settings_win.cget('bg') == A.draw.LIGHT['BG']  # rebuilt in the new palette
        windows['light'] = False  # Windows switches to dark; the next tick picks it up

    def back_to_dark():
        assert A.draw.BG == A.draw.DARK['BG']
        app.set_pref('codex', '#00ff00')

    def codex_recolored():
        assert app.shown['codex'][3] == '#00ff00'
        assert json.loads((tmp / 'settings.json').read_text()) == {'theme': 'system', 'claude': '#d97757',
                                                                   'codex': '#00ff00'}
        assert A.S.load(A.SETTINGS)['codex'] == '#00ff00'  # what the next start reads

    drive(app, [(1500, click_gear), (600, gear_opened_settings), (300, menu_raised_the_same_window),
                (300, follows_windows), (1500, back_to_dark), (300, codex_recolored)])


CLAUDE_RESETS = [L.Reset('claude', 'weekly', 'Weekly reset', 'Refills your weekly limit.', 1, NOW + 86400 * 8, 'g1'),
                 L.Reset('claude', 'full', 'Full reset', 'Refills your 5-hour and weekly limits.', 2, None, 'g2')]
CODEX_RESETS = [L.Reset('codex', 'codex', 'Codex reset', 'Refills your Codex limits.', 1)]


def click(app, name):
    x0, y0, x1, y1 = app.hits[name]
    app.label.event_generate('<Button-1>', x=(x0 + x1) // 2, y=(y0 + y1) // 2)


def test_free_resets_show_and_spend(fake_checks, monkeypatch):
    """Resets line, a Use link per kind, the confirm (no, then yes), one spend at a time, the outcome note."""
    calls, answers, _ = fake_checks
    answers['resets']['claude'][:] = CLAUDE_RESETS
    answers['resets']['codex'][:] = CODEX_RESETS
    asked, spent = [], []
    reply = {'yes': False}

    def askyesno(title, message, **kw):
        asked.append((title, message, kw['parent']))
        return reply['yes']
    monkeypatch.setattr(A.messagebox, 'askyesno', askyesno)
    gate = threading.Event()

    def use_reset(r, key=None):  # the fake spend; L.use_reset itself is never reached
        spent.append((r.provider, r.kind, r.credit_id))
        gate.wait(5)
        return 'reset'
    monkeypatch.setattr(L, 'use_reset', use_reset)
    monkeypatch.setattr(A, 'RESET_NOTE_MS', 1500)
    app = A.App(start_hidden=True)

    def shown():
        app.q.put('open')

    def links_and_tooltip():
        assert {'use:claude:weekly', 'use:claude:full', 'use:codex:codex'} <= set(app.hits)
        assert '3 free resets' in app.icons['claude'].title and '1 free reset\n' in app.icons['codex'].title
        assert answers['forced'][0] is False  # the automatic check doesn't force Anthropic
        app.label.event_generate('<Motion>', x=sum(app.hits['use:claude:full'][::2]) // 2,
                                 y=sum(app.hits['use:claude:full'][1::2]) // 2)
        assert str(app.label.cget('cursor')) == 'hand2'
        click(app, 'use:claude:full')

    def said_no():
        title, message, parent = asked[0]
        assert title == 'Use a Full reset?' and parent is app.root
        assert '2 resets of this kind left' in message and 'Refills your 5-hour and weekly limits.' in message
        assert message.endswith("This can't be undone.") and spent == [] and app.spending is None
        reply['yes'] = True
        click(app, 'use:claude:full')

    def spending():
        assert spent == [('claude', 'full', 'g2')] and app.spending == 'claude'
        assert app.notes() == {'claude': 'Using a reset…'} and 'use:claude:full' not in app.hits
        click(app, 'use:codex:codex')  # a second click while the first runs does nothing
        assert len(asked) == 2 and spent == [('claude', 'full', 'g2')]
        answers['resets']['claude'][:] = CLAUDE_RESETS[:1]  # Anthropic now reports one kind fewer
        gate.set()

    def outcome():
        assert app.spending is None and app.reset_notes == {'claude': 'Reset used'}
        assert calls['claude'] == 2 and answers['forced'][-1] is False  # limits asked again, no extra forced read
        assert [r.kind for r in app.states['claude'].resets] == ['weekly']
        assert 'use:claude:weekly' not in app.hits  # the note stands in for the links

    def note_gone():
        assert app.reset_notes == {} and 'use:claude:weekly' in app.hits

    drive(app, [(1500, shown), (500, links_and_tooltip), (300, said_no), (300, spending), (700, outcome),
                (1500, note_gone)])


@pytest.mark.parametrize('outcome, text', [('reset', 'Reset used'), ('nothing', 'Nothing to reset right now'),
                                           ('no_credit', 'No resets left'),
                                           ('cooldown', 'Claude is cooling down, try later'),
                                           ('rate_limited', 'Claude is cooling down, try later'),
                                           ('signed_out', 'Signed out'), ('already', 'That reset was already used'),
                                           ('unconfirmed', "Couldn't confirm the reset. Refresh to check"),
                                           ('refused', "That reset can't be used now. Refresh to check"),
                                           ('failed', "Couldn't use the reset. Try again later"),
                                           ('crash', "Couldn't use the reset. Try again later")])
def test_each_reset_outcome_shows_in_plain_words(fake_checks, monkeypatch, outcome, text):
    _, answers, _ = fake_checks
    answers['resets']['codex'][:] = CODEX_RESETS
    monkeypatch.setattr(A.messagebox, 'askyesno', lambda *a, **k: True)

    def use_reset(r, key=None):
        if outcome == 'crash':
            raise RuntimeError('boom')
        return outcome
    monkeypatch.setattr(L, 'use_reset', use_reset)
    app = A.App(start_hidden=True)

    def use():
        app.q.put('open')
        app.root.after(300, lambda: click(app, 'use:codex:codex'))

    def shows():
        assert app.notes() == {'codex': text} and app.spending is None

    drive(app, [(1500, use), (800, shows)])


def test_old_cache_without_resets_loads(fake_checks):
    """A last.json from before free resets still gives its numbers."""
    _, answers, tmp = fake_checks
    st = L.ProviderState(answers['claude'](), NOW - 600)
    old = st.to_json()
    del old['resets']
    (tmp / 'last.json').write_text(json.dumps({'claude': old}))
    b = Bare()
    A.App.load_cache(b)
    assert b.states['claude'].rows == st.rows and b.states['claude'].resets == []


def test_unsure_spend_retries_with_the_same_key_and_a_used_one_is_gone(fake_checks, monkeypatch):
    """A spend that may have gone through ("failed") is retried as the same spend; a used reset leaves the list
    at once, even when the check after it fails."""
    _, answers, _ = fake_checks
    answers['resets']['codex'][:] = [L.Reset('codex', 'codex', 'Codex reset', '', 2)]
    monkeypatch.setattr(A.messagebox, 'askyesno', lambda *a, **k: True)
    keys, outcomes = [], ['failed', 'reset', 'reset']

    def use_reset(r, key=None):
        keys.append(key)
        return outcomes[len(keys) - 1]
    monkeypatch.setattr(L, 'use_reset', use_reset)
    app = A.App(start_hidden=True)

    def first():
        app.q.put('open')
        app.root.after(300, lambda: click(app, 'use:codex:codex'))

    def second():
        assert app.notes() == {'codex': "Couldn't use the reset. Try again later"}
        app._reset_note('codex', None)

        def boom():
            raise L.ProbeError('no answer in 25 s')
        monkeypatch.setattr(A, 'fetch_codex', boom)  # the check after the spend fails
        app.root.after(100, lambda: click(app, 'use:codex:codex'))

    def third():
        assert keys[1] == keys[0]  # the retry is the same spend
        assert [r.count for r in app.states['codex'].resets] == [1]  # the used one is gone
        app._reset_note('codex', None)
        app.root.after(100, lambda: click(app, 'use:codex:codex'))

    def fresh_key():
        assert len(keys) == 3 and keys[2] != keys[1] and app.states['codex'].resets == []

    drive(app, [(1500, first), (800, second), (800, third), (800, fresh_key)])


def test_resets_alone_changing_are_saved(fake_checks):
    _, answers, tmp = fake_checks
    b = Bare()
    b.saved, b.saved_at = None, 0.0
    b.states['codex'] = L.ProviderState(answers['codex'](), NOW, resets=list(CODEX_RESETS))
    A.App.save_cache(b)
    b.states['codex'].resets = []
    A.App.save_cache(b)  # only the resets changed: written now, not 5 minutes later
    assert json.loads((tmp / 'last.json').read_text())['codex']['resets'] == []


def test_retry_keeps_key_and_grant_until_settled_and_old_checks_dont_restore(fake_checks, monkeypatch):
    _, answers, _ = fake_checks
    answers['resets']['claude'][:] = [L.Reset('claude', 'weekly', 'Weekly reset', '', 2, None, 'g1')]
    monkeypatch.setattr(A.messagebox, 'askyesno', lambda *a, **k: True)
    tries, outcomes = [], ['failed', 'rate_limited', 'reset']

    def use_reset(r, key=None):
        tries.append((key, r.credit_id))
        return outcomes[len(tries) - 1]
    monkeypatch.setattr(L, 'use_reset', use_reset)
    app = A.App(start_hidden=True)

    def again():
        app._reset_note('claude', None)
        app.root.after(100, lambda: click(app, 'use:claude:weekly'))

    def first():
        app.q.put('open')
        app.root.after(300, lambda: click(app, 'use:claude:weekly'))
        # Anthropic now lists another grant first; the unsure spend must still be retried as itself
        answers['resets']['claude'][:] = [L.Reset('claude', 'weekly', 'Weekly reset', '', 2, None, 'g9')]

    def done():
        assert len(tries) == 3 and len({t for t in tries}) == 1 and tries[0][1] == 'g1'
        assert app.spend_keys == {}  # settled: the next spend is a new one
        app.states['claude'].resets = L.without_one(answers['resets']['claude'], 'weekly')  # what is left now
        # a check that began before the spend comes back with the old list: it doesn't bring the used one back
        app.recheck.add('claude')
        app.handle(('result', 'claude', answers['claude'](), None, time.time(), 0.1, list(answers['resets']['claude'])))
        assert [r.count for r in app.states['claude'].resets] == [1]

    drive(app, [(1500, first), (800, again), (800, again), (800, done)])


def test_refused_first_try_keeps_no_key_but_a_refused_retry_does(fake_checks, monkeypatch):
    """A first try Anthropic refuses (say, an expired grant) spent nothing: no key is kept, so the next click is
    a fresh spend. A refused retry proves nothing about the unsure try before it: the key stays."""
    _, answers, _ = fake_checks
    answers['resets']['claude'][:] = [L.Reset('claude', 'weekly', 'Weekly reset', '', 2, None, 'g1')]
    monkeypatch.setattr(A.messagebox, 'askyesno', lambda *a, **k: True)
    tries, outcomes = [], ['refused', 'failed', 'refused']

    def use_reset(r, key=None):
        tries.append(key)
        return outcomes[len(tries) - 1]
    monkeypatch.setattr(L, 'use_reset', use_reset)
    app = A.App(start_hidden=True)

    def first():
        app.q.put('open')
        app.root.after(300, lambda: click(app, 'use:claude:weekly'))

    def again():
        app._reset_note('claude', None)
        app.root.after(100, lambda: click(app, 'use:claude:weekly'))

    def done():
        assert len(tries) == 3 and tries[1] != tries[0] and tries[2] == tries[1]
        assert app.spend_keys == {('claude', 'weekly'): (tries[1], 'g1')}

    drive(app, [(1500, first), (800, again), (800, again), (800, done)])


def center(app, name):
    x0, y0, x1, y1 = app.hits[name]
    return {'x': (x0 + x1) // 2, 'y': (y0 + y1) // 2}


def test_hover_press_spin_fade_and_glide(fake_checks, monkeypatch):
    """Hover eases in and out, a click dips, the refresh icon spins during a check by hand and settles at 0,
    the window fades in, and a card's % glides to the exact new value. Nothing keeps running afterwards."""
    _, answers, _ = fake_checks
    monkeypatch.setattr(A.anim, 'motion_on', lambda: True)  # this PC may have animations off
    app = A.App(start_hidden=True)
    key = 'card:codex:codex:primary'
    seen = []  # the card's % at each frame
    real_frame = app.on_frame
    app.anim.on_frame = lambda: (seen.append(app.anim.value(key)), real_frame())

    def shown():
        app.open_window()
        assert app.alpha == 0.0 and 'window' in app.anim.tweens  # starts clear, then fades in

    def faded_in_then_hover():
        assert app.alpha == 1.0 and float(app.root.attributes('-alpha')) == 1.0
        app.label.event_generate('<Motion>', **center(app, 'settings'))
        assert app.hover == 'settings' and 'settings' in app.anim.tweens
        assert str(app.label.cget('cursor')) == 'hand2'

    def hovered_then_leave():
        assert app.anim.value('settings') == 1.0 and not app.anim.running
        app.label.event_generate('<Motion>', x=2, y=2)  # off every clickable
        assert app.hover is None and str(app.label.cget('cursor')) == ''

    def left_then_click():
        assert app.anim.value('settings') == 0.0 and not app.anim.running
        answers['codex'] = lambda: [L.Row('codex:primary', 'Session', 40, NOW + 3600, 300),
                                    L.Row('codex:secondary', 'Weekly', 60, NOW + 86400 * 5, 10080)]
        app.label.event_generate('<Button-1>', **center(app, 'refresh'))
        assert app.anim.value('refresh!') == 1.0 and app.spinning and 'spin' in app.anim.tweens

    def all_still():
        assert app.anim.value('refresh!') == 0.0 and app.anim.value('spin') == 0.0 and not app.spinning
        assert app.anim.value(key) == 60.0 and app.fx()[key] is None  # exact end; drawn from the row itself
        assert any(20 < v < 60 for v in seen)  # it glided through values in between
        assert not app.anim.running and app.anim.job is None  # idle: no frame timer

    drive(app, [(1500, shown), (500, faded_in_then_hover), (400, hovered_then_leave),
                (400, left_then_click), (2500, all_still)])


def test_reduced_motion_shows_end_states_at_once(fake_checks, monkeypatch):
    monkeypatch.setattr(A.anim, 'motion_on', lambda: False)
    app = A.App(start_hidden=True)

    def shown():
        app.q.put('open')

    def check():
        assert app.alpha == 1.0
        app.label.event_generate('<Motion>', **center(app, 'refresh'))
        assert app.anim.value('refresh') == 1.0
        app.label.event_generate('<Button-1>', **center(app, 'refresh'))
        assert app.anim.value('refresh!') == 0.0 and app.anim.value('spin') == 0.0
        assert not app.anim.running and app.anim.job is None

    drive(app, [(1500, shown), (200, check)])


def test_settings_buttons_ease_on_hover(fake_checks, monkeypatch):
    monkeypatch.setattr(A.anim, 'motion_on', lambda: True)
    app = A.App(start_hidden=True)

    def opened():
        app.open_settings()
        app.settings_win.update()

    def hover():
        w, color, hot = app.tk_hover['tk:5:2']  # Claude's Change… button
        assert w.cget('cursor') == 'hand2' and w.cget('bg') == color
        w.event_generate('<Enter>')

    def hovered():
        w, color, hot = app.tk_hover['tk:5:2']
        assert w.cget('bg') == hot and not app.anim.running
        w.event_generate('<Leave>')

    def back():
        w, color, hot = app.tk_hover['tk:5:2']
        assert w.cget('bg') == color and not app.anim.running
        assert all(r.cget('cursor') == 'hand2' for k, (r, *_) in app.tk_hover.items() if k.startswith('tk:radio'))

    drive(app, [(300, opened), (300, hover), (400, hovered), (400, back)])


def test_minimized_during_a_check_stops_the_spin(fake_checks, monkeypatch):
    monkeypatch.setattr(A.anim, 'motion_on', lambda: True)
    app = A.App(start_hidden=True)

    def shown():
        app.open_window()

    def click_then_minimize():
        app.label.event_generate('<Button-1>', **center(app, 'refresh'))
        assert app.spinning
        app.root.iconify()

    def stopped():
        assert not app.spinning and not app.anim.running and app.anim.job is None

    drive(app, [(1500, shown), (500, click_then_minimize), (2000, stopped)])
