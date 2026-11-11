"""Ask Claude Code and Codex for their plan limits, the same way T3 Code does, and format the answers.

Claude: `claude -p` in stream-json mode with no prompt, control request `get_usage` (no model call, no tokens).
Codex:  `codex app-server`, JSON-RPC `account/rateLimits/read`.
Both CLIs refresh their own logins, so this app never touches a token.
"""
import ctypes
import json
import logging
import math
import os
import queue
import shutil
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from datetime import datetime

CREATE_NO_WINDOW = 0x08000000
SESSION_MINS, WEEK_MINS = 300, 10080
PROBE_TIMEOUT = 25  # seconds, same as T3
log = logging.getLogger('ai-usage.limits')


class ProbeError(Exception):
    """A check failed; the message is shown to the user, so keep it short and plain."""


@dataclass
class Row:
    key: str
    label: str
    used: float  # percent used, 0-100
    resets_at: float | None = None  # epoch seconds
    window_mins: int | None = None


@dataclass
class ProviderState:
    rows: list[Row] = field(default_factory=list)
    checked_at: float | None = None  # last successful check
    error: str | None = None  # last attempt's failure, None when it worked

    def to_json(self):
        return {'rows': [asdict(r) for r in self.rows], 'checked_at': self.checked_at}

    @classmethod
    def from_json(cls, d):
        """Rebuild from the cache file; raises TypeError on anything malformed so the caller can drop it."""
        rows = []
        for r in d['rows']:
            used, resets, mins = _num(r['used']), r.get('resets_at'), r.get('window_mins')
            if (not isinstance(r['key'], str) or not isinstance(r['label'], str) or used is None
                    or (resets is not None and _num(resets) is None)
                    or (mins is not None and (not isinstance(mins, int) or isinstance(mins, bool) or mins <= 0))):
                raise TypeError('bad cached row')
            rows.append(Row(r['key'], r['label'], _clamp(used), None if resets is None else float(resets), mins))
        checked = d.get('checked_at')
        if checked is not None and _num(checked) is None:
            raise TypeError('bad cached time')
        return cls(rows=rows, checked_at=checked)


# ---------- parsing ----------

def _num(v):
    """A finite real number as float, else None (rejects bools, NaN and infinity)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return float(v)
    return None


def _clamp(p):
    return max(0.0, min(100.0, p))


def _iso_to_epoch(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _spend_percent(rl):
    """Monthly spend row, same rules as T3's readSpendPercent."""
    spend = rl.get('spend')
    if isinstance(spend, dict) and spend.get('enabled') is True:
        used, limit = spend.get('used') or {}, spend.get('limit') or {}
        u, lim = _num(used.get('amount_minor')), _num(limit.get('amount_minor'))
        if (u is not None and lim and lim > 0 and u >= 0 and used.get('currency') == limit.get('currency')
                and used.get('exponent') == limit.get('exponent')):
            return _clamp(u / lim * 100)
    extra = rl.get('extra_usage')
    if isinstance(extra, dict) and extra.get('is_enabled') is True:
        u, lim = _num(extra.get('used_credits')), _num(extra.get('monthly_limit'))
        if u is not None and lim and lim > 0 and u >= 0:
            return _clamp(u / lim * 100)
        p = _num(extra.get('utilization'))
        if p is not None and p >= 0:
            return _clamp(p)
    return None


def parse_claude(resp):
    """`resp` is the get_usage control response payload. Percentages are already 0-100."""
    if not isinstance(resp, dict) or not resp.get('rate_limits_available') or not isinstance(resp.get('rate_limits'), dict):
        raise ProbeError('no limits for this login')
    rl = resp['rate_limits']
    rows = []
    for key, label, mins in (('five_hour', 'Session', SESSION_MINS), ('seven_day', 'Weekly', WEEK_MINS)):
        w = rl.get(key)
        if isinstance(w, dict) and _num(w.get('utilization')) is not None:
            rows.append(Row(key, label, _clamp(w['utilization']), _iso_to_epoch(w.get('resets_at')), mins))
    for e in rl.get('model_scoped') or []:
        if isinstance(e, dict) and isinstance(e.get('display_name'), str) and _num(e.get('utilization')) is not None:
            name = e['display_name'].strip()
            rows.append(Row('seven_day_' + ''.join(c if c.isalnum() else '_' for c in name.lower()),
                            f'Weekly · {name}', _clamp(e['utilization']), _iso_to_epoch(e.get('resets_at')), WEEK_MINS))
    spend = _spend_percent(rl)
    if spend is not None:
        rows.append(Row('monthly_spend', 'Monthly spend', spend))
    if not rows:
        raise ProbeError('no limits for this login')
    return rows


def _window_label(mins):
    if mins == SESSION_MINS:
        return 'Session'
    if mins == WEEK_MINS:
        return 'Weekly'
    if isinstance(mins, int) and mins > 0:
        return f'{mins // 1440}d' if mins % 1440 == 0 else f'{mins // 60}h' if mins % 60 == 0 else f'{mins}m'
    return 'Limit'


def parse_codex(result):
    """`result` is the account/rateLimits/read result. usedPercent is 0-100, resetsAt is epoch seconds."""
    if not isinstance(result, dict):
        raise ProbeError('no limits for this login')
    main = result.get('rateLimits') if isinstance(result.get('rateLimits'), dict) else None
    main_id = (main or {}).get('limitId') or 'codex'
    snaps = result.get('rateLimitsByLimitId')
    snaps = dict(snaps) if isinstance(snaps, dict) and snaps else {}
    if main and main_id not in snaps:
        snaps[main_id] = main
    rows = []
    for lid in sorted(snaps, key=lambda k: k != main_id):  # the main limit first
        snap = snaps[lid]
        if not isinstance(snap, dict):
            continue
        suffix = '' if lid == main_id else f" · {snap.get('limitName') or lid}"
        for slot in ('primary', 'secondary'):
            w = snap.get(slot)
            if isinstance(w, dict) and _num(w.get('usedPercent')) is not None:
                mins = w.get('windowDurationMins') if isinstance(w.get('windowDurationMins'), int) else None
                rows.append(Row(f'{lid}:{slot}', _window_label(mins) + suffix, _clamp(w['usedPercent']),
                                _num(w.get('resetsAt')), mins))
    if not rows:
        raise ProbeError('no limits for this login')
    return rows


# ---------- talking to the CLIs ----------

_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_ntdll = ctypes.WinDLL('ntdll')
_ntdll.NtResumeProcess.restype, _ntdll.NtResumeProcess.argtypes = ctypes.c_long, [wintypes.HANDLE]
CREATE_SUSPENDED = 0x4


class _JobLimits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD),
                ('IoCounters', ctypes.c_uint64 * 6), ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t)]


def _kill_on_close_job():
    """A Windows job that kills every process in it when it is terminated or its last handle closes
    (including when this app exits or crashes). Children of a process in the job join it automatically."""
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        raise ProbeError('could not set up a check')
    info = _JobLimits(LimitFlags=0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # ExtendedLimitInformation
        _k32.CloseHandle(job)
        raise ProbeError('could not set up a check')
    return job


class _JobAccounting(ctypes.Structure):  # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
    _fields_ = [('TotalUserTime', ctypes.c_int64), ('TotalKernelTime', ctypes.c_int64),
                ('ThisPeriodTotalUserTime', ctypes.c_int64), ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD),
                ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]


class _JobPids(ctypes.Structure):  # JOBOBJECT_BASIC_PROCESS_ID_LIST
    _fields_ = [('Assigned', wintypes.DWORD), ('InList', wintypes.DWORD), ('Ids', ctypes.c_size_t * 512)]


class _MemCounters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
        (n, ctypes.c_size_t) for n in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                                       'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
                                       'PagefileUsage', 'PeakPagefileUsage')]


_k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID]
_k32.OpenProcess.restype, _k32.OpenProcess.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD]


def working_set_mb(handle):
    m = _MemCounters(cb=ctypes.sizeof(_MemCounters))
    return m.WorkingSetSize / 2 ** 20 if _k32.K32GetProcessMemoryInfo(handle, ctypes.byref(m), m.cb) else 0.0


def _job_usage(job):
    """(CPU seconds, processes ever started, processes running, MB in use now) for everything in a job."""
    a = _JobAccounting()
    if not _k32.QueryInformationJobObject(job, 1, ctypes.byref(a), ctypes.sizeof(a), None):
        return 0.0, 0, 0, 0.0
    mb, ids = 0.0, _JobPids()
    if _k32.QueryInformationJobObject(job, 3, ctypes.byref(ids), ctypes.sizeof(ids), None):
        for pid in ids.Ids[:ids.InList]:
            h = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                mb += working_set_mb(h)
                _k32.CloseHandle(h)
    return (a.TotalUserTime + a.TotalKernelTime) / 1e7, a.TotalProcesses, a.ActiveProcesses, mb


class Session:
    """One CLI kept running between checks, so a check is a single message (about 0.06 s of CPU) instead of a
    fresh start (about 1.4 s of CPU and a rewrite of ~/.claude.json). Restarted after any failure and every
    30 minutes. Everything it starts lives in a kill-on-close job, so nothing outlives this app."""

    MAX_AGE = 1800

    def __init__(self, argv, hello, extra_env=None, name='cli', login=None):
        self.argv = argv  # list, or a function returning one (raises ProbeError when the CLI is missing)
        self.hello = hello  # messages sent once after starting
        self.extra_env = extra_env
        self.name = name
        self.login = login  # function naming who is signed in (None = signed out); raises OSError/ValueError if it can't tell
        self.login_seen = None
        self.lock = threading.Lock()  # one check at a time; held for the whole check
        self.job_lock = threading.Lock()  # guards the job handle and totals; held only for moments
        self.p = self.job = self.waiter = None
        self.started = 0.0
        self.n = 1
        self.starts, self.done_cpu, self.done_procs = 0, 0.0, 0  # totals over copies already closed

    def usage(self):
        """Totals over every copy so far (CPU seconds, copies, processes started) plus what runs now. Never waits
        for a check, so the window can call it."""
        with self.job_lock:
            cpu, procs, running, mb = _job_usage(self.job) if self.job else (0.0, 0, 0, 0.0)
            return {'cpu': self.done_cpu + cpu, 'starts': self.starts, 'procs': self.done_procs + procs,
                    'running': running, 'mb': mb}

    def _start(self, why=''):
        argv = self.argv() if callable(self.argv) else self.argv
        env = dict(os.environ, **self.extra_env) if self.extra_env else None
        job = _kill_on_close_job()
        try:  # started suspended so it can't start anything before it is in the job
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env=env, cwd=_work_dir(), creationflags=CREATE_NO_WINDOW | CREATE_SUSPENDED,
                                 text=True, encoding='utf-8', errors='replace')
        except OSError as e:
            _k32.CloseHandle(job)
            raise ProbeError(f'could not start ({e.strerror or e})') from e
        if not _k32.AssignProcessToJobObject(job, int(p._handle)) or _ntdll.NtResumeProcess(int(p._handle)) != 0:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout):
                f.close()
            _k32.CloseHandle(job)
            raise ProbeError('could not set up a check')
        with self.job_lock:
            self.p, self.job, self.started = p, job, time.time()
            self.starts += 1
        log.info('%s: started a copy (%s), pid %s', self.name, why or 'first check', p.pid)
        threading.Thread(target=self._read, args=(p,), daemon=True).start()
        self._send(self.hello)

    def _read(self, p):
        for line in p.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            w = self.waiter
            if w and w[0] is p and isinstance(msg, dict):
                hit = w[1](msg)
                if hit is not None:
                    w[2].put(hit)
        p.output_ended = True  # per process; set before looking for a waiter, ask() checks it after setting one
        w = self.waiter
        if w and w[0] is p:
            w[2].put(('error', 'closed without answering'))

    def _send(self, msgs):
        try:
            for m in msgs:
                self.p.stdin.write(json.dumps(m) + '\n')
            self.p.stdin.flush()
        except OSError:
            pass  # it exited; the reader reports that

    def ask(self, build, timeout=PROBE_TIMEOUT):
        """build(n) -> (message, match). match(msg) returns ('ok', value), ('error', text) or None."""
        with self.lock:
            login = self._login_now()  # a running copy keeps the account it started with, so a new login restarts it
            if self.p is None or self.p.poll() is not None or login != self.login_seen or \
                    time.time() - self.started > self.MAX_AGE:
                why = ('after a failed check' if self.starts else '') if self.p is None else \
                    f'the last one exited, code {self.p.returncode}' if self.p.poll() is not None else \
                    'the login changed' if login != self.login_seen else 'every 30 min'
                self._close()
                self._start(why)
                self.login_seen = login
            self.n += 1
            msg, match = build(self.n)
            answers = queue.Queue()
            self.waiter = (self.p, match, answers)
            if getattr(self.p, 'output_ended', False):
                answers.put(('error', 'closed without answering'))
            self._send([msg])
            try:
                kind, value = answers.get(timeout=timeout)
            except queue.Empty:
                self._close()
                raise ProbeError(f'no answer in {timeout} s') from None
            finally:
                self.waiter = None
            if kind == 'error':
                self._close()
                raise ProbeError(value)
            return value

    def _login_now(self):
        try:
            return self.login() if self.login else None
        except (OSError, ValueError):
            return self.login_seen  # couldn't tell (say, a file mid-write): keep the running copy

    def close(self):
        with self.lock:
            self._close()

    def _close(self):
        p = self.p
        if p is None:
            return
        try:
            p.stdin.close()
        except OSError:
            pass
        with self.job_lock:
            job, self.p, self.job = self.job, None, None
            _k32.TerminateJobObject(job, 1)  # ends it and everything it started
            cpu, procs, _, _ = _job_usage(job)
            self.done_cpu += cpu
            self.done_procs += procs
            _k32.CloseHandle(job)
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            pass


def _work_dir():
    d = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'AIUsage')
    os.makedirs(d, exist_ok=True)
    return d


def _which(name, label):
    def argv():
        # On Windows shutil.which looks in the current folder before PATH (even with path=) unless this is set.
        os.environ['NoDefaultCurrentDirectoryInExePath'] = '1'
        exe = shutil.which(name)
        if not exe:
            raise ProbeError(f'{label} not found')
        return [exe]
    return argv


_claude_exe, _codex_exe = _which('claude', 'Claude Code'), _which('codex', 'Codex')
# Both CLIs update plugins and marketplaces in the background (dozens of `git clone`s, and a restart cuts a
# big clone off so the next start begins it again). Claude: DISABLE_AUTOUPDATER turns its plugin auto-update
# off (it starts after a random delay), and skipping the user settings file skips loading the plugins (the
# login lives elsewhere). Codex: plugins and apps off.
CLAUDE = Session(lambda: _claude_exe() + [
    '-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
    '--setting-sources', 'project', '--settings', '{"disableAllHooks":true}',
    '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--no-session-persistence'],
    [{'type': 'control_request', 'request_id': 'init', 'request': {'subtype': 'initialize'}}],
    {'ENABLE_CLAUDEAI_MCP_SERVERS': 'false', 'CLAUDE_CODE_AUTO_CONNECT_IDE': '0', 'CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL': '1',
     'DISABLE_AUTOUPDATER': '1'}, name='claude', login=lambda: claude_account())
CODEX = Session(lambda: _codex_exe() + ['app-server'] + [
    a for f in ('plugins', 'plugin_discovery_prefetch', 'remote_plugin', 'apps', 'in_app_updates',
                'skill_mcp_dependency_install') for a in ('--disable', f)], [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'ai-usage', 'version': '1.0'}}},
    {'jsonrpc': '2.0', 'method': 'initialized'}], name='codex', login=lambda: codex_login())


def ask_claude():
    """Raw get_usage payload from Claude Code."""
    def build(n):
        rid = f'usage-{n}'

        def match(m):
            r = m.get('response') if m.get('type') == 'control_response' else None
            if not isinstance(r, dict) or r.get('request_id') != rid:
                return None
            if r.get('subtype') == 'error':
                return 'error', str(r.get('error') or 'refused')[:60]
            return 'ok', r.get('response')
        return {'type': 'control_request', 'request_id': rid, 'request': {'subtype': 'get_usage', 'skip_behaviors': True}}, match
    return CLAUDE.ask(build)


def ask_codex():
    """Raw account/rateLimits/read result from Codex."""
    def build(n):
        def match(m):
            if m.get('id') != n:
                return None
            if 'error' in m:
                err = m['error']
                return 'error', str(err.get('message') if isinstance(err, dict) else err)[:60]
            return 'ok', m.get('result')
        return {'jsonrpc': '2.0', 'id': n, 'method': 'account/rateLimits/read', 'params': None}, match
    return CODEX.ask(build)


def fetch_claude():
    return parse_claude(ask_claude())


def _claude_config():
    d = os.environ.get('CLAUDE_CONFIG_DIR')
    return os.path.join(d or os.path.expanduser('~'), '.claude.json')


def claude_account():
    """Id of the signed-in Claude account, from ~/.claude.json (no tokens there). The token file
    (.credentials.json) gets rewritten whenever any Claude session refreshes a sign-in, so it can't tell a switch."""
    try:
        with open(_claude_config(), encoding='utf-8') as f:
            d = json.load(f)
        acc = d.get('oauthAccount') if isinstance(d, dict) else None
    except FileNotFoundError:
        return None
    return acc.get('accountUuid') if isinstance(acc, dict) else None


def codex_login():
    """Change time of Codex's login file; its contents (tokens) are never read.
    shortcut: a token refresh (every few days) also restarts the copy; cheap (4 processes), so not worth reading the token file."""
    try:
        return os.stat(os.path.join(os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex'), 'auth.json')).st_mtime_ns
    except FileNotFoundError:
        return None  # signed out


def claude_numbers_time(rate_limits):
    """When Anthropic sent the numbers in a get_usage answer, or None if unknown. Claude's usage endpoint often
    answers 429 (too many requests); Claude then answers from the snapshot it saved in ~/.claude.json (up to an
    hour old, shared by every Claude session). The snapshot's time counts only if it holds these same numbers."""
    try:
        with open(_claude_config(), encoding='utf-8') as f:
            snap = json.load(f).get('cachedUsageUtilization', {})
        t, saved = _num(snap.get('fetchedAtMs')), snap.get('utilization')
        same = [_pick(saved.get(k)) == _pick(rate_limits.get(k)) for k in ('five_hour', 'seven_day')]
    except (OSError, ValueError, AttributeError):
        return None
    return t / 1000 if t and all(same) and _pick(rate_limits.get('seven_day')) else None


def _pick(window):
    return (window.get('utilization'), window.get('resets_at')) if isinstance(window, dict) else None


def fetch_claude_dated():
    """(rows, when Anthropic sent them or None if unknown)."""
    raw = ask_claude()
    rows = parse_claude(raw)
    limits = raw.get('rate_limits') if isinstance(raw, dict) else None
    return rows, claude_numbers_time(limits) if isinstance(limits, dict) else None


def numbers_time(state, rows, sent_at, now):
    """The time to show for new rows: when they were sent, else (unknown) unchanged rows keep their known time."""
    if sent_at is not None:
        return min(now, sent_at)
    return state.checked_at if state.checked_at and rows == state.rows else now


def fetch_codex():
    return parse_codex(ask_codex())


def close_all():
    CLAUDE.close()
    CODEX.close()


# ---------- what to show ----------

def used_now(row, now):
    """A window that already reset since the check is back to 0% used."""
    return 0.0 if row.resets_at is not None and row.resets_at <= now else row.used


def left_pct(row, now):
    return round(100 - used_now(row, now))


def time_left_frac(row, now):
    """Share of the window's time still left (T3's white marker), or None."""
    if row.resets_at is None or not row.window_mins or row.resets_at <= now:
        return None
    return max(0.0, min(1.0, (row.resets_at - now) / (row.window_mins * 60)))


def pace(row, now):
    """'under' = using slower than time passes, 'over' = faster (may run out before reset), 'on' = about even."""
    t = time_left_frac(row, now)
    if t is None:
        return None
    diff = (100 - used_now(row, now)) - t * 100
    return 'under' if diff > 5 else 'over' if diff < -5 else 'on'


def countdown(row, now):
    if row.resets_at is None:
        return None
    s = int(row.resets_at - now)
    if s <= 0:
        return 'now'
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f'{d}d {h}h' if d else f'{h}h {m}m' if h else f'{max(m, 1)}m'


def clock(ts):
    return datetime.fromtimestamp(ts).strftime('%I:%M %p').lstrip('0')


STALE_SECS = 120  # numbers older than this show their own time ("numbers from 3:00 PM") instead of "checked"


def stale(state, now):
    return bool(state.rows and state.checked_at and not state.error and now - state.checked_at > STALE_SECS)


def tray_row(provider, rows):
    """Claude icon = weekly limit; Codex icon = 5-hour limit of the main Codex limit."""
    if provider == 'claude':
        return next((r for r in rows if r.key == 'seven_day'), None)
    if not rows:
        return None
    main = [r for r in rows if r.key.split(':')[0] == rows[0].key.split(':')[0]]  # parse_codex puts the main limit first
    return next((r for r in main if r.window_mins == SESSION_MINS), main[0])


def level(left):
    return 'red' if left <= 10 else 'amber' if left <= 25 else 'ok'


TIP_MAX = 127  # Windows' tray hover text buffer holds 128 UTF-16 units including the end marker


def _utf16_len(s):
    return len(s.encode('utf-16-le')) // 2


def tooltip(name, state, now):
    """Hover text, cut to what Windows' tray hover text can hold."""
    if state.error:
        head = f'{name} · check failed: {state.error}'
    elif state.checked_at:
        head = f"{name} · {'numbers from' if stale(state, now) else 'checked'} {clock(state.checked_at)}"
    else:
        head = f'{name} · checking…'
    lines = [head]
    for r in state.rows:
        cd = countdown(r, now)
        lines.append(f'{r.label}  {left_pct(r, now)}% left' + (f' · {cd}' if cd else ''))
    text = '\n'.join(lines)
    if _utf16_len(text) <= TIP_MAX:
        return text
    while _utf16_len(text) > TIP_MAX - 1:  # drop whole characters, never half of an emoji's pair
        text = text[:-1]
    return text + '…'
