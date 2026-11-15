"""AI Usage: two tray numbers (Claude 5-hour % left, or weekly on a plan with no 5-hour limit; Codex 5-hour % left)
and a Limits window like T3 Code's. A provider that is not installed or not signed in gets no tray number."""
import ctypes
import json
import logging
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
from types import SimpleNamespace
from tkinter import colorchooser, messagebox
from ctypes import wintypes
from logging.handlers import RotatingFileHandler

from PIL import Image, ImageTk
from pystray import Menu, MenuItem
from pystray._util import win32
from pystray._win32 import Icon as Win32Icon

from . import __version__, anim, draw, limits as L, settings as S, update

APP = 'AI Usage'
REFRESH_SECS = 5  # checks every 5 s. Cheap because both CLIs stay running between checks (limits.Session)
MAX_BACKOFF_SECS = 300  # when checks keep failing, wait up to 5 min between tries
UPDATE_FIRST_SECS, UPDATE_SECS = 60, 6 * 3600  # update check 1 min after start, then every 6 hours
NOTE_MS = 4000  # how long "Up to date" / "Couldn't check" stays after a check by hand
RESET_NOTE_MS = 6000  # how long "Reset used" (or why not) stays after a "Use" click
CARD_OPEN_MS = 300  # T3 (Base UI): the mouse rests this long on a bar before its hover card opens
CARD_CLOSE_MS = 150  # time to cross from a bar to its card before the card closes
HOST = os.environ.get('COMPUTERNAME') or socket.gethostname()  # the hover card's "Signed in", as T3 names this PC
HEALTH_SECS = 600  # a health line in app.log 1 min after start, then every 10 min, and on Close
NAMES = {'claude': 'Claude Code', 'codex': 'Codex'}
DATA = L._work_dir()  # %LOCALAPPDATA%\AIUsage, made here at import
CACHE = os.path.join(DATA, 'last.json')
SETTINGS = os.path.join(DATA, 'settings.json')

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
for fn, res, args in (('CreateMutexW', wintypes.HANDLE, [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]),
                      ('CreateEventW', wintypes.HANDLE, [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]),
                      ('SetEvent', wintypes.BOOL, [wintypes.HANDLE]),
                      ('CloseHandle', wintypes.BOOL, [wintypes.HANDLE]),
                      ('WaitForSingleObject', wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD])):
    getattr(kernel32, fn).restype, getattr(kernel32, fn).argtypes = res, args
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
user32.GetGuiResources.restype, user32.GetGuiResources.argtypes = wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]
user32.GetParent.restype, user32.GetParent.argtypes = wintypes.HWND, [wintypes.HWND]
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.MonitorFromWindow.restype, user32.MonitorFromWindow.argtypes = wintypes.HANDLE, [wintypes.HWND, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, wintypes.LPVOID]


class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT),
                ('dwFlags', wintypes.DWORD)]
log = logging.getLogger('ai-usage')
WM_REBUILD_MENU = win32.WM_USER + 20  # pystray uses WM_USER + 10 and + 11


class TrayIcon(Win32Icon):
    """pystray icon with a fixed id, so Windows remembers "always show" across restarts
    (pystray passes `hID`, which NOTIFYICONDATAW ignores, so every pystray icon gets id 0),
    an icon loaded at the exact tray size instead of scaled down from 32 px, and a lock so the
    window thread, pystray's thread and Explorer restarts never touch the icon handle at once."""

    def __init__(self, uid, *args, **kwargs):
        self._uid = uid
        self._lock = threading.RLock()
        self._menu_open = self._menu_stale = False
        self._want = True  # shown or hidden, as show() last asked
        super().__init__(*args, **kwargs)
        self._message_handlers[WM_REBUILD_MENU] = self._on_rebuild_menu

    def run(self):
        super().run(lambda icon: icon.show())

    def show(self, want=None):
        """Show or hide the icon (None: apply the last wish). Before its message loop is ready this only remembers
        it, and run() applies it."""
        with self._lock:
            if want is not None:
                self._want = want
            if self._running:
                self.visible = self._want

    def _on_taskbarcreated(self, wparam, lparam):  # Explorer restarted: re-add, unless a hide runs at that moment
        with self._lock:
            super()._on_taskbarcreated(wparam, lparam)

    def _on_display_change(self, wparam, lparam):
        with self._lock:
            super()._on_display_change(wparam, lparam)

    def rebuild_menu(self):
        """Re-read the menu items' text and visibility. pystray destroys and remakes the native menu, so do it on
        the icon's own thread, never while that menu is open."""
        if self._hwnd:
            win32.PostMessage(self._hwnd, WM_REBUILD_MENU, 0, 0)
        else:  # message loop not started yet: nobody can be showing the menu
            self._update_menu()

    def _on_rebuild_menu(self, wparam, lparam):
        if self._menu_open:  # the open menu's own message loop delivered this; rebuild once it closes
            self._menu_stale = True
        else:
            self._update_menu()

    def _on_notify(self, wparam, lparam):
        self._menu_open = True
        try:
            super()._on_notify(wparam, lparam)
        finally:
            self._menu_open = False
            if self._menu_stale:
                self._menu_stale = False
                self._update_menu()

    def _message(self, code, flags, **kwargs):
        win32.Shell_NotifyIcon(code, win32.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW), hWnd=self._hwnd, uID=self._uid, uFlags=flags, **kwargs))

    def _assert_icon_handle(self):
        if self._icon_handle:
            return
        n = self.icon.width
        fd, path = tempfile.mkstemp(suffix='.ico')
        os.close(fd)
        try:
            self.icon.save(path, format='ICO', sizes=[(n, n)], bitmap_format='bmp')
            self._icon_handle = win32.LoadImage(None, path, win32.IMAGE_ICON, n, n, win32.LR_LOADFROMFILE)
        finally:
            os.remove(path)

    def _update_icon(self):
        with self._lock:
            super()._update_icon()

    def _show(self):
        with self._lock:
            super()._show()

    def _hide(self):
        with self._lock:
            super()._hide()

    def _update_title(self):
        with self._lock:
            super()._update_title()


def tray_size():
    return user32.GetSystemMetrics(49) or 16  # SM_CXSMICON


def work_area(hwnd=None):
    """Screen minus taskbar: of the monitor the window is on, else of the main monitor."""
    if hwnd:
        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        if user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):  # 2 = nearest monitor
            return mi.rcWork
    r = wintypes.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA: screen minus taskbar
    return r


class App:
    def __init__(self, start_hidden):
        self.q = queue.Queue()
        self.states = {'claude': L.ProviderState(), 'codex': L.ProviderState()}  # window order: Claude on top
        self.load_cache()
        self.sched = {p: {'busy': False, 'last': 0.0, 'fails': 0} for p in self.states}  # per provider
        self.counts = {p: self.new_counts() for p in self.states}  # checks since the last health line
        self.started_at = self.counted_since = time.time()
        self.health_at = self.started_at - HEALTH_SECS + 60  # first line after 1 min, as a baseline
        self.manual = False
        self.last_ui = self.saved_at = 0.0
        self.saved = None
        self.start_hidden = start_hidden
        self.shown = {}  # what each tray icon currently shows, to skip no-op updates
        self.hits = {}
        self.drawn = None  # what the window last showed, to skip identical redraws
        self.placed = False
        self.update_tag = None  # a newer release tag once update.check() finds one
        self.updater = None  # the update window's process, so a second click doesn't start a second installer
        self.checking = False  # a "Check for updates" click is running, so a second click does nothing
        self.check_note = None  # "Up to date (vX)" / "Couldn't check" for NOTE_MS after that check
        self.note_job = None
        self.prefs = S.load(SETTINGS)
        self.light = None  # the palette in use; apply_theme() sets it below
        self.settings_win = None
        self.spending = None  # the provider whose reset is being spent; a second "Use" click waits for it
        self.reset_notes = {}  # provider -> outcome text, for RESET_NOTE_MS
        self.reset_jobs = {}
        self.spend_keys = {}  # (provider, kind) -> (key, credit) of a spend not known to be settled; a retry reuses both
        self.recheck = set()  # providers to check again once their running check ends (it started before a spend)
        self.hover = None  # the clickable under the mouse (its hit name)
        self.parts, self.part_fx = [], []  # the window's moving parts and the fx each was last drawn with
        self.screen = None  # the window picture shown now, which part redraws are pasted into
        self.spinning = False
        self.slots = {}  # fade key -> what that place last showed, so new text fades in
        self.tk_hover = {}  # Settings window widgets with a hover look: key -> (widget, color, hover color)
        self.alpha = 1.0
        self.screen_ver = 0  # counts full redraws, so the hover card knows its glass is out of date
        self.card_bar = None  # 'bar:<provider>:<row key>' whose hover card is open or fading out
        self.card_open = False
        self.card_wait = None  # the bar the mouse rests on before its card opens
        self.card_jobs = {}  # 'open' / 'close' -> after() id
        self.card_cache = None  # (what the card showed, (patch, top-left, hits))
        self.card_hits = {}  # the open card's clickables, screen px
        self.card_shown = None  # (fade, what) last put on screen
        self.reveal = False  # the card's email shown (a click on it, like T3); hidden again when the card closes
        self.mouse = None  # where the mouse last was over the window (None: outside it)

        self.root = root = tk.Tk()
        root.withdraw()
        root.title(APP)
        root.configure(bg=draw.BG)
        root.report_callback_exception = lambda *exc: log.error('window error', exc_info=exc)
        self._app_icon = ImageTk.PhotoImage(draw.app_icon(64), master=root)
        root.iconphoto(True, self._app_icon)
        root.protocol('WM_DELETE_WINDOW', self.hide_window)  # X hides; Close in the tray menu quits
        self.anim = anim.Animator(root, self.on_frame)
        self.label = tk.Label(root, bd=0, bg=draw.BG, highlightthickness=0, anchor='nw')
        self.label.pack(fill='both', expand=True)
        self.label.bind('<Button-1>', self.on_click)
        self.label.bind('<Motion>', self.on_motion)
        self.label.bind('<Leave>', self.on_leave)
        root.bind('<Configure>', self.on_configure)
        self._last_size = None
        self.apply_theme()

        menu = Menu(MenuItem(lambda item: f'Update to {self.update_tag}', lambda: self.q.put('update'),
                             visible=lambda item: bool(self.update_tag)),
                    MenuItem(lambda item: self.check_text(), lambda: self.q.put('check')),
                    MenuItem('Settings…', lambda: self.q.put('settings')),
                    MenuItem('Refresh', lambda: self.q.put('refresh')),
                    MenuItem('Open', lambda: self.q.put('open'), default=True),
                    Menu.SEPARATOR,
                    MenuItem('Close', lambda: self.q.put('quit')))
        n = tray_size()
        self.icons = {p: TrayIcon(uid, f'ai-usage-{p}', draw.render_tray('–', 'none', p, n, self.prefs[p]), NAMES[p],
                                 menu)
                      for uid, p in ((1, 'claude'), (2, 'codex'))}
        self.icon_threads = [threading.Thread(target=ic.run, daemon=True) for ic in self.icons.values()]

    # ---------- cache ----------

    def load_cache(self):
        try:
            with open(CACHE, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for p in self.states:  # one bad entry must not drop the other provider's numbers
            try:
                self.states[p] = L.ProviderState.from_json(data[p])
            except (TypeError, KeyError, AttributeError, ValueError):
                pass

    def save_cache(self):
        data = json.dumps({p: s.to_json() for p, s in self.states.items()})
        rows = json.dumps({p: {k: v for k, v in s.to_json().items() if k != 'checked_at'} for p, s in self.states.items()})
        if rows == self.saved and time.time() - self.saved_at < 300:
            return  # numbers unchanged: write at most every 5 minutes
        tmp = CACHE + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(data)
            os.replace(tmp, CACHE)
            self.saved, self.saved_at = rows, time.time()
        except OSError:
            log.exception('could not save %s', CACHE)

    # ---------- checking ----------

    def refresh(self, manual=False, only=None, force=None):
        """force: ask Anthropic for Claude's resets now (default: on a check by hand)."""
        for p in only or self.states:
            s = self.sched[p]
            if s['busy']:
                continue
            s['busy'], s['last'] = True, time.time()
            threading.Thread(target=self._check, args=(p, manual if force is None else force), daemon=True).start()
        if manual:
            self.manual = True
            self.update_ui()

    def _check(self, p, force=False):
        t0 = time.time()
        try:
            if p == 'claude':
                # Claude may answer with numbers Anthropic sent a while ago: keep their real time (None = unknown)
                rows, sent_at = L.fetch_claude_dated()
                resets = self._claude_resets(force)
                who = L.claude_who(L.CLAUDE.greeting())
            else:
                rows, resets, who = fetch_codex()
                sent_at = time.time()
            self.q.put(('result', p, rows, None, sent_at, time.time() - t0, resets, None, who))
        except L.Unavailable as e:
            log.info('%s: %s', p, e)
            self.q.put(('result', p, None, None, time.time(), time.time() - t0, None, str(e), None))
        except L.ProbeError as e:
            log.warning('%s check failed: %s', p, e)
            self.q.put(('result', p, None, str(e), time.time(), time.time() - t0, None, None, None))
        except Exception:
            log.exception('%s check crashed', p)
            self.q.put(('result', p, None, 'unexpected error (see app.log)', time.time(), time.time() - t0, None, None,
                        None))

    @staticmethod
    def _claude_resets(force):
        """Claude's free resets (asks Anthropic at most every 5 min, or now on Refresh); None keeps the last ones."""
        try:
            return L.claude_resets(force=force)
        except Exception:
            log.exception('claude resets crashed')
            return None

    def watch_updates(self, first=UPDATE_FIRST_SECS, every=UPDATE_SECS, stop=None):
        """Off the window thread: ask GitHub for a newer release, 1 min after start and then every 6 hours."""
        stop = stop or threading.Event()
        wait = first
        while not stop.wait(wait):
            tag = update.check()
            if tag:
                self.q.put(('newer', tag))
            wait = every

    def _check_updates(self):
        try:
            self.q.put(('checked', update.check(strict=True), None))
        except Exception as e:
            until = update.busy_until(e)
            self.q.put(('checked', None, f'GitHub busy, try at {L.clock(until)}' if until else "Couldn't check"))

    def check_text(self):
        return 'Checking…' if self.checking else self.check_note or 'Check for updates'

    def _set_note(self, note):
        self.check_note = note
        if self.note_job:
            self.root.after_cancel(self.note_job)
        self.note_job = self.root.after(NOTE_MS, self._note_out) if note else None
        for ic in self.icons.values():
            ic.rebuild_menu()
        self.update_ui()

    def _note_out(self):
        """Fade "Up to date" out, then put the button's own text back (it fades in)."""
        self.fade_to('fade:check', 0.0)
        self.note_job = self.root.after(self.fade_ms(), self._set_note, None)

    def interval(self, p):
        """5 s normally; doubles with each failure in a row, up to 5 min, so a broken login can't spin the CPU.
        Not installed or signed out: every 5 min, so it comes back by itself (Refresh asks at once)."""
        if self.states[p].missing:
            return MAX_BACKOFF_SECS
        return min(MAX_BACKOFF_SECS, REFRESH_SECS * 2 ** min(self.sched[p]['fails'], 10))

    def busy(self):
        return any(s['busy'] for s in self.sched.values())

    def handle(self, msg):
        if msg == 'open':
            self.open_window()
        elif msg == 'refresh':
            self.refresh(manual=True)
        elif msg == 'settings':
            self.open_settings()
        elif msg == 'update':
            if self.updater and self.updater.poll() is None:
                return  # already running
            log.info('update to %s: starting the installer', self.update_tag)
            self.updater = update.start()
        elif msg == 'check':
            if self.checking:
                return
            self.checking = True
            threading.Thread(target=self._check_updates, daemon=True).start()
            self._set_note(None)
        elif msg[0] == 'checked':
            _, tag, err = msg
            self.checking = False
            if tag:
                self.handle(('newer', tag))
            self._set_note(err or (None if tag else f'Up to date (v{__version__})'))
        elif msg[0] == 'newer':
            if msg[1] != self.update_tag:
                self.update_tag = msg[1]
                log.info('update available: %s', msg[1])
                for ic in self.icons.values():
                    ic.rebuild_menu()
                self.update_ui()
        elif msg[0] == 'spent':
            _, p, kind, key, credit, outcome = msg
            self.spending = None
            log.info('%s reset: %s', p, outcome)
            retry = self.spend_keys.get((p, kind), (None,))[0] == key
            # a first try refused spent nothing; a refused retry proves nothing about the unsure try before it
            if outcome in L.SETTLED or (outcome == 'refused' and not retry):
                self.spend_keys.pop((p, kind), None)
            else:  # it may have gone through: until we know, a retry must be the same spend
                self.spend_keys[(p, kind)] = (key, credit)
            if outcome in L.SPENT:  # that one is gone, whatever the next check says
                self.states[p].resets = L.without_one(self.states[p].resets, kind)
            self._reset_note(p, L.OUTCOMES.get(outcome, L.OUTCOMES['failed']))
            if self.sched[p]['busy']:
                self.recheck.add(p)
            self.refresh(manual=True, only=[p], force=False)  # the Claude spend already asked its resets again
        elif msg[0] == 'result':
            _, p, rows, err, ts, secs, resets, missing, who = msg
            st, s, c = self.states[p], self.sched[p], self.counts[p]
            if missing:  # not installed or signed out: its old numbers and resets go, the card says why
                st.rows, st.resets, st.checked_at, st.error, st.missing = [], [], None, None, missing
                st.account = None
            elif rows is not None:
                st.rows, st.checked_at, st.error = rows, L.numbers_time(st, rows, ts, time.time()), None
                st.missing, st.account = None, who
            else:
                st.error = err
            if resets is not None and p not in self.recheck:  # a check begun before a spend has old resets
                st.resets = resets
            s['busy'] = False
            if rows is not None and s['fails']:
                log.info('%s checks working again after %d failed', p, s['fails'])
            s['fails'] = 0 if rows is not None else s['fails'] + 1
            c['ok' if rows is not None else 'failed'] += 1
            c['secs'] += secs
            c['slowest'] = max(c['slowest'], secs)
            if not self.busy():
                self.manual = False
            if p in self.recheck:  # this check began before a spend: ask again for what is left now
                self.recheck.discard(p)
                self.refresh(manual=True, only=[p], force=False)
            self.save_cache()
            self.update_ui()

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg == 'quit':
                    self.quit()
                    return
                try:
                    self.handle(msg)
                except Exception:
                    log.exception('could not handle %s', msg if isinstance(msg, str) else msg[0])
        except queue.Empty:
            pass
        self.root.after(150, self.poll)  # always rescheduled, so the tray menu keeps working after an error

    def tick(self):
        try:
            now = time.time()
            self.apply_theme()  # Match Windows follows a change in Windows within a second
            due = [p for p, s in self.sched.items() if not s['busy'] and now - s['last'] >= self.interval(p)]
            if due:
                self.refresh(only=due)
            elif now - self.last_ui >= 30:
                self.update_ui()  # countdowns and hover text
            if now - self.health_at >= HEALTH_SECS:
                self.log_health()
        except Exception:
            log.exception('tick failed')
        finally:
            self.root.after(1000, self.tick)

    @staticmethod
    def new_counts():
        return {'ok': 0, 'failed': 0, 'secs': 0.0, 'slowest': 0.0}

    def log_health(self):
        """One line on how the app has been doing, for reading app.log after hours of running."""
        now = time.time()
        up = int(now - self.started_at)
        parts = [f'up {up // 3600}h{up % 3600 // 60:02d}m']
        for p, session in (('claude', L.CLAUDE), ('codex', L.CODEX)):
            c = self.counts[p]
            n = c['ok'] + c['failed']
            at = self.states[p].checked_at
            line = (f"{p}: last {int(now - self.counted_since) // 60} min {c['ok']} ok, {c['failed']} failed"
                    + (f", avg {c['secs'] / n:.2f} s, slowest {c['slowest']:.2f} s" if n else '')
                    + (f", numbers {int(now - at) // 60} min old" if at else ''))
            u = session.usage()
            line += (f"; copies started {u['starts']}, processes started {u['procs']}, running {u['running']},"
                     f" CPU {u['cpu']:.1f} s, {u['mb']:.0f} MB")
            parts.append(line)
            self.counts[p] = self.new_counts()
        me, handles = kernel32.GetCurrentProcess(), wintypes.DWORD()
        kernel32.GetProcessHandleCount(me, ctypes.byref(handles))
        parts.append(f'app: CPU {time.process_time():.1f} s, {L.working_set_mb(me):.0f} MB, {handles.value} handles,'
                     f' {user32.GetGuiResources(me, 0)} GDI, {user32.GetGuiResources(me, 1)} USER,'
                     f' {threading.active_count()} threads')
        self.health_at = self.counted_since = now
        text = 'health: ' + ' | '.join(parts)
        log.info(text)
        return text

    # ---------- showing ----------

    def update_ui(self):
        now = self.last_ui = time.time()
        n = tray_size()
        gone = {p for p, st in self.states.items() if st.missing}
        for p, ic in self.icons.items():
            ic.show(p not in gone or (p == 'claude' and gone == set(self.icons)))  # one stays, so the app can be opened
            st = self.states[p]
            row = L.tray_row(p, st.rows)
            left = L.left_pct(row, now) if row else None
            look = (str(left), L.level(left), n, self.prefs[p]) if row else ('–', 'none', n, self.prefs[p])
            if self.shown.get(p) != look:
                ic.icon = draw.render_tray(*look[:2], p, n, look[3])
                self.shown[p] = look  # only once shown, so a failed update is retried
            ic.title = L.tooltip(NAMES[p], st, now)
        if self.root.state() in ('normal', 'zoomed'):
            self.render()

    def scale(self):
        try:
            return (user32.GetDpiForWindow(self.hwnd()) or 96) / 96
        except (AttributeError, OSError):
            return 1.0

    def hwnd(self):
        return user32.GetParent(self.root.winfo_id())

    def render(self):
        s = self.scale()
        now = time.time()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        busy = self.busy() and self.manual  # 5-second background checks don't flash "Checking…"
        notes = self.notes()
        self.start_motion(busy, notes, now)
        shown = [(L.left_pct(r, now), L.countdown(r, now), L.pace(r, now))  # what each row shows right now
                 for st in self.states.values() for r in st.rows]
        content_h = self.content_height(now, notes, s)
        sig = (repr([(st.rows, st.error, st.missing, L.stale(st, now)) for st in self.states.values()]), shown,
               [st.checked_at and L.clock(st.checked_at) for st in self.states.values()], busy, w, h, s, int(now // 60),
               self.update_tag, self.check_text(), repr([st.resets for st in self.states.values()]), notes, content_h)
        fx = self.fx()
        if sig == self.drawn:
            self.show(force=self.redraw_parts(fx))
            return
        parts = []
        img, self.hits = draw.render_window(self.states, now, w / s, s, busy, h / s, self.update_tag,
                                              self.check_text(), notes, fx, parts)
        self._photo = ImageTk.PhotoImage(img, master=self.root)
        self.label.configure(image=self._photo)
        self.screen, self.parts = img, parts
        self.part_fx = [tuple(fx.get(k) for k in part[0]) for part in parts]
        self.drawn = sig  # only once drawn, so a failed render is retried
        self.screen_ver += 1
        self.card_shown = (0.0, None)  # the new picture has no card on it yet
        self.show()
        if self.card_open and self.mouse:  # things moved: the mouse may not be on the card or its bar any more
            self.track_card(self.mouse)
        self.root.minsize(round(draw.W_MIN * s), content_h)
        if self.root.state() == 'normal' and h < content_h:
            # grow to fit the content, upward when it would run under the taskbar of the window's monitor
            work = work_area(self.hwnd())
            over = self.root.winfo_rooty() + content_h + round(12 * s) - work.bottom
            y = max(work.top, self.root.winfo_y() - max(0, over))
            self.root.geometry(f'{w}x{content_h}+{self.root.winfo_x()}+{y}')

    def content_height(self, now, notes, s):
        """Window height in screen px: the content, or the tallest hover card if that needs more."""
        return round(max(draw.window_height(self.states, now, notes), draw.card_room(self.states, now, HOST)) * s)

    # ---------- motion ----------

    def fx(self):
        """What moves now, for draw.render_window: every animated value; a card's % only while it glides."""
        fx = dict(self.anim.values)
        for k in fx:
            if k.startswith('card:') and k not in self.anim.tweens:
                fx[k] = None
        return fx

    def fade_ms(self):
        return anim.FADE_MS if self.anim.motion() else 0

    def fade_to(self, key, value):
        self.anim.to(key, value, anim.FADE_MS, start=self.anim.value(key, 1.0))

    def start_motion(self, busy, notes, now):
        """Start what the coming picture should move: the refresh spin, fade-ins of new notes, gliding numbers."""
        if busy != self.spinning:
            self.spinning = busy
            self.anim.spin('spin') if busy else self.anim.settle('spin')
        slots = {'fade:check': self.check_text()}
        for p in self.states:
            slots[f'fade:{p}'] = notes.get(p)
        for key, shows in slots.items():
            if key in self.slots and self.slots[key] != shows and shows:
                self.anim.to(key, 1.0, anim.FADE_MS, start=0.0)
            self.slots[key] = shows
        for p, st in self.states.items():
            for r in st.rows:
                key, left = f'card:{p}:{r.key}', 100 - L.used_now(r, now)
                if key in self.anim.values:
                    self.anim.to(key, left, anim.GLIDE_MS)
                else:
                    self.anim.set(key, left)  # first numbers show at once

    def redraw_parts(self, fx):
        """Redraw only the parts whose motion changed, into the window picture. True if any did."""
        changed = False
        for i, part in enumerate(self.parts):
            now = tuple(fx.get(k) for k in part[0])
            if now != self.part_fx[i]:
                im, (x, y) = draw.render_part(part, fx)
                self.screen.paste(im, (x, y))
                self.part_fx[i] = now
                changed = True
                if self.card_cache:  # a part under the card moved: its glass is drawn again
                    patch, (cx, cy), _ = self.card_cache[1]
                    if x < cx + patch.width and cx < x + im.width and y < cy + patch.height and cy < y + im.height:
                        self.card_cache = None
        return changed

    def on_frame(self):
        a = self.anim.value('window', 1.0)
        if a != self.alpha:
            self.alpha = a
            self.root.attributes('-alpha', a)
        self.paint_tk_hover()
        if self.root.state() in ('normal', 'zoomed'):
            self.render()
        elif self.spinning:  # minimized mid-check: nothing draws, so stop the one tween that never ends by itself
            self.anim.set('spin', 0.0)
            self.spinning = False

    def set_hover(self, name):
        if name == self.hover:
            return
        for n, v in ((self.hover, 0.0), (name, 1.0)):
            if n:
                self.anim.to(n, v, anim.HOVER_MS, start=self.anim.value(n, 0.0))
        self.hover = name
        self.render()

    def press(self, name):
        """A click's quick dip: full at once, then back over a moment."""
        self.anim.to(name + '!', 0.0, anim.HOVER_MS, start=1.0)
        self.render()

    def hide_window(self):
        """The X: hide, and stop whatever moves (the next open starts still)."""
        self.close_card(at_once=True)
        self.root.withdraw()
        self.anim.cancel()
        self.anim.values = {k: v for k, v in self.anim.values.items() if k.startswith('card:')}
        self.hover, self.spinning = None, False

    def open_window(self):
        root = self.root
        if not self.placed:
            self.placed = True
            s = self.scale()
            w = round(680 * s)
            h = self.content_height(time.time(), self.notes(), s)
            work = work_area()
            margin = round(12 * s)
            root.geometry(f'{w}x{h}+{max(work.left, work.right - w - margin)}+{max(work.top, work.bottom - h - margin - round(32 * s))}')
        if root.state() in ('iconic', 'withdrawn'):
            if root.state() == 'withdrawn':  # fade in from hidden
                self.anim.to('window', 1.0, anim.FADE_MS, start=0.0)
                self.alpha = self.anim.value('window', 1.0)
                root.attributes('-alpha', self.alpha)
            root.deiconify()
        root.update_idletasks()
        self.title_bar(self.hwnd())
        root.attributes('-topmost', True)
        root.after(200, lambda: root.attributes('-topmost', False))
        root.lift()
        root.focus_force()
        self.drawn = None
        self.render()

    def title_bar(self, hwnd):
        try:
            dwm = ctypes.windll.dwmapi
            on = ctypes.c_int(0 if self.light else 1)
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 20, ctypes.byref(on), 4)  # dark mode caption
            bg = int(draw.BG[1:], 16)
            colorref = ctypes.c_int(((bg & 0xFF) << 16) | (bg & 0xFF00) | (bg >> 16))
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 35, ctypes.byref(colorref), 4)  # caption color, Win11
        except OSError:
            pass

    def on_configure(self, e):
        size = (e.width, e.height)
        if e.widget is self.root and size != self._last_size:
            self._last_size = size
            if self.root.state() in ('normal', 'zoomed'):
                self.root.after_idle(self.render)

    def _hit(self, e, name='refresh'):
        x0, y0, x1, y1 = self.hits.get(name, (0, 0, -1, -1))
        return x0 <= e.x <= x1 and y0 <= e.y <= y1

    def hit_name(self, e):
        """The clickable under the mouse that eases on hover: a button, a link, or one on the hover card."""
        on_card = self.card_hit(e)
        if on_card:
            return None if on_card == 'card' else on_card
        return next((n for n in ('refresh', 'settings', 'update', 'check') if self._hit(e, n)), None)

    def on_click(self, e):
        self.mouse = SimpleNamespace(x=e.x, y=e.y)
        on_card = self.card_hit(e)
        if on_card:
            if on_card.startswith('use:'):
                _, p, kind = on_card.split(':')
                self.close_card(at_once=True)  # like T3: the card closes, then the question
                self.use_reset(p, kind)
            elif on_card == 'email':
                self.reveal = not self.reveal
                self.show()
            return
        bar = self.bar_at(e)
        if bar:  # a click opens its card at once, a second one closes it
            self.close_card() if self.card_open and bar == self.card_bar else self.open_card(bar)
            return
        if self.card_open:
            self.close_card()
        name = self.hit_name(e)
        if name:
            self.press(name)
        if self._hit(e):
            self.refresh(manual=True)
        elif self._hit(e, 'update'):
            self.q.put('update')
        elif self._hit(e, 'check'):
            self.q.put('check')
        elif self._hit(e, 'settings'):
            self.open_settings()

    def on_motion(self, e):
        self.mouse = SimpleNamespace(x=e.x, y=e.y)
        name = self.hit_name(e)
        self.label.configure(cursor='hand2' if name or (not self.card_hit(e) and self.bar_at(e)) else '')
        self.set_hover(name)
        self.track_card(e)

    def on_leave(self, e):
        self.mouse = None
        self.set_hover(None)
        self.card_wait = None
        self._card_job('open')
        if self.card_open:
            self._card_job('close', CARD_CLOSE_MS, self.close_card)

    # ---------- hover card ----------

    def bar_at(self, e):
        return next((n for n in self.hits if n.startswith('bar:') and self._hit(e, n)), None)

    def card_hit(self, e):
        """On the open card: its clickable under the mouse ('use:...', 'email'), else 'card'. Off it: None."""
        inside = lambda b: b[0] <= e.x <= b[2] and b[1] <= e.y <= b[3]
        if not self.card_open or not inside(self.card_hits.get('card', (0, 0, -1, -1))):
            return None
        return next((n for n, b in self.card_hits.items() if n != 'card' and inside(b)), 'card')

    def near_card(self, e):
        """On the open card, its bar, or the gap between them: where the card stays open."""
        if 'card' not in self.card_hits or self.card_bar not in self.hits:
            return False
        cx0, cy0, cx1, cy1 = card = self.card_hits['card']
        bx0, by0, bx1, by1 = bar = self.hits[self.card_bar]
        gap = (max(cx0, bx0), min(cy1, by1), min(cx1, bx1), max(cy0, by0))
        return any(b[0] <= e.x <= b[2] and b[1] <= e.y <= b[3] for b in (card, bar, gap))

    def _card_job(self, kind, ms=None, fn=None):
        """Cancel the card's pending 'open' or 'close'; with fn, schedule it in its place."""
        job = self.card_jobs.pop(kind, None)
        if job:
            self.root.after_cancel(job)
        if fn:
            self.card_jobs[kind] = self.root.after(ms, lambda: (self.card_jobs.pop(kind, None), fn()))

    def track_card(self, e):
        """T3's hover card: rest on a bar and its card opens; it stays while the mouse is on the bar, the card or
        the gap between them, and closes a moment after the mouse leaves them."""
        bar = None if self.card_hit(e) else self.bar_at(e)
        if self.card_open and bar in (None, self.card_bar) and self.near_card(e):
            self._card_job('close')
            return
        if bar:
            self._card_job('close')
            if self.card_open:
                self.open_card(bar)  # from one bar to the next: its card at once
            elif bar != self.card_wait:
                self.card_wait = bar
                self._card_job('open', CARD_OPEN_MS, lambda: self.mouse and self.bar_at(self.mouse) == bar
                               and self.open_card(bar))  # only if the bar is still under the mouse
            return
        self.card_wait = None
        self._card_job('open')
        if self.card_open and 'close' not in self.card_jobs:
            self._card_job('close', CARD_CLOSE_MS, self.close_card)

    def open_card(self, bar):
        for kind in ('open', 'close'):
            self._card_job(kind)
        self.card_wait = None
        if bar not in self.hits:
            return
        if not self.card_open:
            self.reveal = False
        self.card_bar, self.card_open = bar, True
        self.anim.to('card', 1.0, anim.FADE_MS, start=self.anim.value('card', 0.0))
        self.show()

    def close_card(self, at_once=False):
        for kind in ('open', 'close'):
            self._card_job(kind)
        self.card_wait, self.card_open = None, False
        if at_once:
            self.anim.set('card', 0.0)
        else:
            self.anim.to('card', 0.0, anim.FADE_MS, start=self.anim.value('card', 0.0))
        self.show()

    def card_layer(self):
        """(patch, top-left, hits) of the card over the window picture now; drawn again only when it changed.
        None when its row is gone."""
        _, p, key = self.card_bar.split(':', 2)
        st = self.states[p]
        row = next((r for r in st.rows if r.key == key), None)
        if self.card_bar not in self.hits or row is None or st.missing:
            return None
        m = draw.card_model(p, st, row, time.time(), HOST, busy=self.spending == p)
        fx = {n: self.anim.value(n, 0.0) for r in st.resets for n in (f'use:{p}:{r.kind}', f'use:{p}:{r.kind}!')}
        sig = (self.card_bar, repr(m), self.reveal, tuple(fx.items()), self.hits[self.card_bar], self.screen_ver)
        if not self.card_cache or self.card_cache[0] != sig:
            self.card_cache = (sig, draw.render_card(self.screen, m, self.hits[self.card_bar], self.scale(), fx,
                                                     self.reveal))
        return self.card_cache[1]

    def show(self, force=False):
        """Put the window picture on screen, with the hover card over it while it is open or fading out."""
        if self.screen is None:
            return
        a, layer = self.anim.value('card', 0.0), None
        if self.card_bar:
            layer = self.card_layer()
            if layer is None or not (self.card_open or a):  # its row went, or it faded out
                self.card_bar, self.card_open, self.card_cache, layer = None, False, None, None
                self.anim.set('card', 0.0)
        a = a if layer else 0.0
        self.card_hits = layer[2] if layer and self.card_open else {}
        look = (a, self.card_cache[0] if layer else None)
        if look == self.card_shown and not force:
            return
        img = self.screen
        if layer:
            patch, (x, y), _ = layer
            img = img.copy()
            if a < 1:
                patch = Image.blend(img.crop((x, y, x + patch.width, y + patch.height)), patch, a)
            img.paste(patch, (x, y))
        self._photo.paste(img)
        self.card_shown = look

    # ---------- free resets ----------

    def notes(self):
        """A line under each provider's cards: while one of its resets is spent, then the outcome."""
        notes = dict(self.reset_notes)
        if self.spending:
            notes[self.spending] = 'Using a reset…'
        return notes

    def use_reset(self, p, kind):
        """A "Use" button on the hover card: ask first, then spend one off the window thread. One at a time."""
        if self.spending:
            return
        r = next((r for r in self.states[p].resets if r.kind == kind), None)
        if not r:
            return
        left = f"{r.count} {'reset' if r.count == 1 else 'resets'} of this kind left"
        if not messagebox.askyesno(f'Use a {r.label}?', f'Use one {r.label} for {NAMES[p]}?\n\n{left}.\n'
                                   f"{r.description}\n\nThis can't be undone.", icon='warning', default='no',
                                   parent=self.root):
            return
        if self.spending:  # a click that arrived while the question was open
            return
        self.spending = p
        log.info('%s reset: using a %s', p, r.label)
        key, credit = self.spend_keys.get((p, kind)) or (uuid.uuid4().hex, r.credit_id)
        r = L.Reset(**{**vars(r), 'credit_id': credit})  # a retry asks for the same grant as the first try
        threading.Thread(target=self._spend, args=(p, r, key), daemon=True).start()
        self.drawn = None
        self.update_ui()

    def _spend(self, p, r, key):
        try:
            outcome = L.use_reset(r, key)
        except Exception:
            log.exception('%s reset spend crashed', p)
            outcome = 'failed'
        self.q.put(('spent', p, r.kind, key, r.credit_id, outcome))

    def _reset_note(self, p, text):
        if self.reset_jobs.get(p):
            self.root.after_cancel(self.reset_jobs.pop(p))
        if text:
            self.reset_notes[p] = text
            self.reset_jobs[p] = self.root.after(RESET_NOTE_MS, self._reset_out, p)
        else:
            self.reset_notes.pop(p, None)
            self.reset_jobs.pop(p, None)
        self.drawn = None
        self.update_ui()

    def _reset_out(self, p):
        """Fade the outcome out, then take its line away."""
        self.fade_to(f'fade:{p}', 0.0)
        self.reset_jobs[p] = self.root.after(self.fade_ms(), self._reset_note, p, None)

    # ---------- settings ----------

    def apply_theme(self):
        light = S.is_light(self.prefs['theme'])
        if light == self.light:
            return
        self.light = light
        draw.use_palette(light)
        self.root.configure(bg=draw.BG)
        self.label.configure(bg=draw.BG)
        self.title_bar(self.hwnd())
        self.drawn = None
        if self.root.state() in ('normal', 'zoomed'):
            self.render()
        if self.settings_open():
            self.root.after_idle(self.fill_settings)

    def set_pref(self, key, value):
        self.prefs[key] = value
        try:
            S.save(SETTINGS, self.prefs)
        except OSError:
            log.exception('could not save %s', SETTINGS)
        self.apply_theme()
        self.update_ui()
        if self.settings_open():
            self.root.after_idle(self.fill_settings)  # not now: the clicked button is still in its own handler

    def pick_color(self, p):
        _, color = colorchooser.askcolor(self.prefs[p], parent=self.settings_win, title=f'{NAMES[p]} number color')
        if color:
            self.set_pref(p, color.lower())

    def settings_open(self):
        return bool(self.settings_win and self.settings_win.winfo_exists())

    def open_settings(self):
        if not self.settings_open():
            self.settings_win = tk.Toplevel(self.root)
            self.settings_win.title(f'{APP} settings')
            self.settings_win.resizable(False, False)
            self.fill_settings()
        top = self.settings_win
        top.deiconify()
        top.lift()
        top.focus_force()

    def fill_settings(self):
        """(Re)build the Settings window's widgets in the current palette."""
        top = self.settings_win
        if not self.settings_open():
            return
        for w in top.winfo_children():
            w.destroy()
        self.tk_hover = {}
        bg, fg = draw.BG, draw.TEXT
        control, hot, edge = (draw.solid(c) for c in (draw.CONTROL, draw.CONTROL_HOVER, draw.CONTROL_EDGE))
        body, head = ('Segoe UI', 10), ('Segoe UI Semibold', 11)
        top.configure(bg=bg, padx=22, pady=16)

        def label(text, row, font, **grid):
            tk.Label(top, text=text, bg=bg, fg=fg, font=font).grid(row=row, column=0, columnspan=4, sticky='w', **grid)

        def button(text, cmd, row, col):  # hover tints it; pressed, it shows activebackground
            b = tk.Button(top, text=text, command=cmd, bg=control, fg=fg, activebackground=edge,
                          activeforeground=fg, relief='flat', bd=0, highlightthickness=1,
                          highlightbackground=edge, padx=10, pady=2, font=body, cursor='hand2')
            b.grid(row=row, column=col, padx=(6, 0), pady=3)
            self.hoverable(b, f'tk:{row}:{col}', control, hot)

        label('Window theme', 0, head, pady=(0, 4))
        self._theme_var = tk.StringVar(top, self.prefs['theme'])
        for i, (key, name) in enumerate(S.THEMES.items()):
            r = tk.Radiobutton(top, text=name, value=key, variable=self._theme_var, bg=bg, fg=fg,
                               activebackground=edge, activeforeground=fg, selectcolor=control,
                               highlightthickness=0, bd=0, padx=4, font=body, cursor='hand2',
                               command=lambda: self.set_pref('theme', self._theme_var.get()))
            r.grid(row=1 + i, column=0, columnspan=4, sticky='w', pady=1)
            self.hoverable(r, f'tk:radio:{key}', bg, hot)
        label('Tray number colors', 4, head, pady=(14, 4))
        for i, p in enumerate(('claude', 'codex')):
            row = 5 + i
            tk.Label(top, text=NAMES[p], bg=bg, fg=fg, font=body).grid(row=row, column=0, sticky='w', padx=(0, 14))
            tk.Frame(top, bg=self.prefs[p], width=22, height=22, highlightthickness=1,
                     highlightbackground=edge).grid(row=row, column=1, padx=(0, 4))
            button('Change…', lambda p=p: self.pick_color(p), row, 2)
            button('Reset', lambda p=p: self.set_pref(p, S.DEFAULTS[p]), row, 3)
        top.update_idletasks()
        self.title_bar(user32.GetParent(top.winfo_id()))

    def hoverable(self, w, key, color, hot):
        """A Settings window widget whose background eases to hot under the mouse."""
        self.tk_hover[key] = (w, color, hot)
        self.anim.set(key, 0.0)
        for event, v in (('<Enter>', 1.0), ('<Leave>', 0.0)):
            w.bind(event, lambda e, v=v: (self.anim.to(key, v, anim.HOVER_MS, start=self.anim.value(key, 0.0)),
                                          self.paint_tk_hover()))

    def paint_tk_hover(self):
        for key, (w, color, hot) in self.tk_hover.items():
            if w.winfo_exists():
                w.configure(bg=draw.mix(color, hot, self.anim.value(key, 0.0)))

    # ---------- lifecycle ----------

    def quit(self):
        try:
            self.log_health()
        except Exception:
            log.exception('health line failed')
        for ic in self.icons.values():
            try:
                ic.stop()
            except Exception:
                log.exception('tray stop failed')
        for t in self.icon_threads:
            t.join(2)
        threading.Thread(target=L.close_all, daemon=True).start()  # don't wait on a check in progress
        self.anim.cancel()
        self.root.destroy()

    def run(self):
        self.update_ui()  # first icons and hover text are set before the tray threads show them
        for t in self.icon_threads:
            t.start()
        threading.Thread(target=self.watch_updates, daemon=True).start()
        self.root.after(150, self.poll)
        self.root.after(0, self.tick)  # first check right away
        if not self.start_hidden:
            self.root.after(0, self.open_window)
        self.root.mainloop()


def fetch_codex():
    """Codex's rows, free resets and who is signed in (for the hover card), from one answer."""
    raw = L.ask_codex()
    return L.parse_codex(raw), L.parse_codex_resets(raw), L.codex_who(L.CODEX.greeting(), raw)


def single_instance(show_existing, name='AIUsage'):
    """First copy: returns (mutex, show_event). A second copy wakes the first one's window (unless it is the
    hidden login start) and returns None. The event exists before the mutex, so a wake-up is never lost."""
    ev = kernel32.CreateEventW(None, False, False, f'Local\\{name}.Show')
    ctypes.set_last_error(0)
    h = kernel32.CreateMutexW(None, False, f'Local\\{name}.Instance')
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        if show_existing and ev:
            kernel32.SetEvent(ev)
        for x in (ev, h):
            if x:
                kernel32.CloseHandle(x)
        return None
    return h, ev


def listen_for_show(q, ev):
    def wait():
        while kernel32.WaitForSingleObject(ev, 0xFFFFFFFF) == 0:
            q.put('open')

    threading.Thread(target=wait, daemon=True).start()


def dark_menus():
    """Let the tray menu follow Windows dark mode (uxtheme ordinals 135/136, stable since Windows 10 1903)."""
    try:
        ux = ctypes.WinDLL('uxtheme')
        ux[135].argtypes = [ctypes.c_int]
        ux[135](1)  # SetPreferredAppMode(AllowDark)
        ux[136]()  # FlushMenuThemes
    except (OSError, AttributeError):
        pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    hidden = '--hidden' in argv
    handler = RotatingFileHandler(os.path.join(DATA, 'app.log'), maxBytes=1_000_000, backupCount=1, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    threading.excepthook = lambda a: log.error('thread error', exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: sharp text
    except (AttributeError, OSError):
        pass
    first = single_instance(show_existing=not hidden)
    if not first:
        return 0
    dark_menus()
    app = App(hidden)
    listen_for_show(app.q, first[1])
    log.info('started (hidden=%s)', hidden)
    app.run()
    log.info('closed')
    return 0
