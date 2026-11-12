"""Ask Claude Code and Codex for their plan limits, the same way T3 Code does, and format the answers.

Claude: `claude -p` in stream-json mode with no prompt, control request `get_usage` (no model call, no tokens).
Codex:  `codex app-server`, JSON-RPC `account/rateLimits/read`.
Both CLIs refresh their own logins. The one exception is Claude's free resets (Anthropic's own usage call, like
T3): the Claude login token is read only for those two calls, never saved, logged or refreshed.
"""
import ctypes
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
    resets: list['Reset'] = field(default_factory=list)

    def to_json(self):
        return {'rows': [asdict(r) for r in self.rows], 'checked_at': self.checked_at,
                'resets': [asdict(r) for r in self.resets]}

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
        resets = []
        for r in d.get('resets') or []:  # files from before free resets have none
            count, exp = r['count'], r.get('expires_at')
            if (not all(isinstance(r.get(k), str) for k in ('provider', 'kind', 'label', 'description'))
                    or not isinstance(count, int) or isinstance(count, bool) or count < 1
                    or (exp is not None and _num(exp) is None)
                    or not (r.get('credit_id') is None or isinstance(r['credit_id'], str))):
                raise TypeError('bad cached reset')
            resets.append(Reset(r['provider'], r['kind'], r['label'], r['description'], count,
                                None if exp is None else float(exp), r.get('credit_id')))
        return cls(rows=rows, checked_at=checked, resets=resets)


@dataclass
class Reset:
    """One kind of free reset the person can spend, with how many of it are left."""
    provider: str  # 'claude' or 'codex'
    kind: str  # Claude: session/weekly/fable/full; Codex: codex
    label: str
    description: str
    count: int
    expires_at: float | None = None  # epoch seconds of the first one to expire
    credit_id: str | None = None  # Claude: the grant to spend; Codex: None (Codex picks one, like T3)


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
    n = sum(r.count for r in state.resets)
    if n:
        lines.append(f"{n} free reset{'s' if n > 1 else ''}")
    for r in state.rows:
        cd = countdown(r, now)
        lines.append(f'{r.label}  {left_pct(r, now)}% left' + (f' · {cd}' if cd else ''))
    text = '\n'.join(lines)
    if _utf16_len(text) <= TIP_MAX:
        return text
    while _utf16_len(text) > TIP_MAX - 1:  # drop whole characters, never half of an emoji's pair
        text = text[:-1]
    return text + '…'


# ---------- free resets ----------
# Codex: the rateLimits/read answer carries them; spending one is the JSON-RPC consume call.
# Claude: Anthropic's own usage call (cedar_ember), the same one T3 makes. It needs Claude's login token, so the
# token rules are strict: read only for these two calls, held in a local, sent only to api.anthropic.com with
# Windows' certificate check and no redirects, never logged, saved, put in an error, or refreshed.

ANTHROPIC = 'https://api.anthropic.com'
CLAUDE_USAGE_URL = ANTHROPIC + '/api/oauth/usage?cedar_ember=1&skip_spend=1'
CLAUDE_READ_SECS = 300  # ask Anthropic at most every 5 minutes (plus Refresh and right after a spend)
_GRANT_ID = re.compile(r'[a-z0-9_-]{1,40}')
_REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
_FULL_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})')
CLAUDE_KINDS = {  # T3's labels and one-line descriptions, in T3's order
    'session': ('5-hour reset', 'Refills your 5-hour limit.'),
    'weekly': ('Weekly reset', 'Refills your weekly limit.'),
    'fable': ('Fable reset', "Refills your Fable allowance and gives back what Fable used on your weekly limit. "
                             "It doesn't reset your whole week."),
    'full': ('Full reset', 'Refills your 5-hour and weekly limits.'),
}
OUTCOMES = {  # what a spend returns -> plain words for the window
    'reset': 'Reset used',
    'nothing': 'Nothing to reset right now',
    'no_credit': 'No resets left',
    'already': 'That reset was already used',
    'cooldown': 'Claude is cooling down, try later',
    'rate_limited': 'Claude is cooling down, try later',
    'signed_out': 'Signed out',
    'unconfirmed': "Couldn't confirm the reset. Refresh to check",
    'refused': "That reset can't be used now. Refresh to check",
    'failed': "Couldn't use the reset. Try again later",
}


def parse_codex_resets(result):
    """Free resets in an account/rateLimits/read result (T3's codexResetCreditsToContract)."""
    summary = result.get('rateLimitResetCredits') if isinstance(result, dict) else None
    if not isinstance(summary, dict):
        return []
    count = summary.get('availableCount')
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return []
    available = [c for c in summary.get('credits') or [] if isinstance(c, dict) and c.get('status') == 'available']
    expiries = [e for e in (_num(c.get('expiresAt')) for c in available) if e is not None]
    first = available[0] if available else {}
    title, desc = first.get('title'), first.get('description')
    return [Reset('codex', 'codex', title.strip() if isinstance(title, str) and title.strip() else 'Codex reset',
                  desc.strip() if isinstance(desc, str) and desc.strip() else 'Refills your Codex limits.',
                  count, min(expiries) if expiries else None)]


def _claude_grant_kind(g):
    if g.get('gives_back_weekly') is True:
        return 'fable'
    if g.get('type') in ('session', 'weekly', 'full'):
        return g['type']
    clears = g.get('clears') if isinstance(g.get('clears'), list) else []
    session, other = 'five_hour' in clears, any(c != 'five_hour' for c in clears)
    return 'session' if session and not other else 'weekly' if other and not session else 'full'


def _future_epoch(s, now):
    if not isinstance(s, str) or not _FULL_TIME.fullmatch(s):
        return None
    try:
        t = datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None
    return t if t > now else None


def parse_claude_resets(block, now):
    """Free resets in the usage answer's `cedar_ember` block (T3's claudeResetCreditsToContract)."""
    if not isinstance(block, dict) or block.get('eligible') is not True:
        return []
    live = [g for g in block.get('grants') or []
            if isinstance(g, dict) and isinstance(g.get('id'), str) and _GRANT_ID.fullmatch(g['id'])
            and isinstance(g.get('resets_left'), int) and not isinstance(g['resets_left'], bool)
            and g['resets_left'] >= 0 and g.get('paused') is not True and g.get('usable_now') is True
            and (g.get('ends_at') is None or _future_epoch(g['ends_at'], now))]
    if not any(g['id'] == block.get('next_grant_id') for g in live):
        return []  # T3 shows none unless Anthropic names a usable next grant
    out = []
    for kind, (label, desc) in CLAUDE_KINDS.items():
        grants = [g for g in live if g['resets_left'] > 0 and _claude_grant_kind(g) == kind]
        if grants:
            ends = [e for e in (_future_epoch(g.get('ends_at'), now) for g in grants) if e]
            out.append(Reset('claude', kind, label, desc, sum(g['resets_left'] for g in grants),
                             min(ends) if ends else None, grants[0]['id']))
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):  # a redirect would carry the token to another address
        return None


_opener = None


def _urlopen(req, timeout):
    """The one place a Claude reset call reaches the network. Tests replace it."""
    global _opener
    if _opener is None:
        from .update import TLS  # Windows' own certificate check
        _opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=TLS), _NoRedirect)
    return _opener.open(req, timeout=timeout)


_version = None


def _claude_version():
    """`claude --version`, asked once per app run, for the user-agent (T3 sends the same)."""
    global _version
    if _version is None:
        try:
            out = subprocess.run(_claude_exe() + ['--version'], capture_output=True, text=True, timeout=15,
                                 creationflags=CREATE_NO_WINDOW, cwd=_work_dir()).stdout.split()
        except (ProbeError, OSError, subprocess.SubprocessError):
            return None  # try again next time
        _version = out[0] if out and re.fullmatch(r'[0-9][0-9A-Za-z.+-]*', out[0]) else ''
    return _version or None


def _claude_dir():
    return os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _claude_call(method, url, body, timeout):
    """(HTTP status, JSON body or None) from Anthropic, or None when there is no login to ask with.
    Raises ProbeError on a network failure, with text that never holds the token."""
    if not url.startswith(ANTHROPIC + '/'):
        raise ProbeError('not an Anthropic address')
    version = _claude_version()
    if not version:
        return None
    creds = _read_json(os.path.join(_claude_dir(), '.credentials.json'))
    oauth = creds.get('claudeAiOauth') if isinstance(creds, dict) else None
    token = oauth.get('accessToken') if isinstance(oauth, dict) else None
    token = token.strip() if isinstance(token, str) else ''
    creds = oauth = None
    if not token:
        return None
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={'authorization': f'Bearer {token}', 'anthropic-beta': 'oauth-2025-04-20',
                                          'user-agent': f'claude-cli/{version} (external, cli)',
                                          'content-type': 'application/json'})
    token = None
    try:
        try:
            with _urlopen(req, timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, b''
            e.close()
    except Exception as e:  # noqa: BLE001 - any failure becomes plain words, with the request (and token) dropped
        raise ProbeError('no answer' if isinstance(e, TimeoutError) or 'timed out' in str(e) else 'network error') from None
    finally:
        req = None
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


_claude_resets = {'at': None, 'value': [], 'account': None, 'lock': threading.Lock()}


def claude_resets(now=None, force=False):
    """Claude's free resets, asking Anthropic at most every 5 minutes (force: Refresh or right after a spend).
    No login or a signed-out answer -> none, quietly. Too many requests or a network failure -> the last answer."""
    now = time.time() if now is None else now
    c = _claude_resets
    try:
        account = claude_account()
    except (OSError, ValueError):
        account = c['account']  # couldn't tell (a file mid-write): keep what we had
    with c['lock']:
        if account != c['account']:  # another account (or signed out): the last one's resets aren't its own
            c['account'], c['value'], force = account, [], True
        if not force and c['at'] is not None and now - c['at'] < CLAUDE_READ_SECS:
            return c['value']
        c['at'] = now
        try:
            got = _claude_call('GET', CLAUDE_USAGE_URL, None, 10)
        except ProbeError as e:
            log.info('claude resets: %s', e)
            return c['value']
        if got is None or got[0] in (401, 403):
            c['value'] = []
        elif got[0] == 200 and isinstance(got[1], dict):
            c['value'] = parse_claude_resets(got[1].get('cedar_ember'), now)
        else:
            log.info('claude resets: answer %s', got[0])
        return c['value']


def use_claude_reset(grant_id, request_id):
    """Spend one Claude reset. request_id is the idempotency key: reuse it to retry the same spend.
    Returns a key of OUTCOMES."""
    if not isinstance(grant_id, str) or not _GRANT_ID.fullmatch(grant_id) or \
            not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        return 'failed'
    acc = _read_json(_claude_config())
    acc = acc.get('oauthAccount') if isinstance(acc, dict) else None
    org = acc.get('organizationUuid') if isinstance(acc, dict) else None
    org = org.strip() if isinstance(org, str) else ''
    if not org:
        return 'signed_out'
    try:
        got = _claude_call('POST', f"{ANTHROPIC}/api/organizations/{urllib.parse.quote(org, safe='')}/reset_rate_limits",
                           {'program': 'cedar_ember', 'grant_id': grant_id, 'request_id': request_id}, 25)
    except ProbeError as e:
        log.info('claude reset spend: %s', e)
        return 'failed'
    if got is None or got[0] in (401, 403):
        return 'signed_out'
    if got[0] == 429:
        return 'rate_limited'
    if got[0] in (400, 404, 410, 422):  # this request refused (say, an expired grant); a retry's first try is unknown
        return 'refused'
    result = got[1].get('result') if got[0] == 200 and isinstance(got[1], dict) else None
    return {'reset': 'reset', 'not_limited': 'nothing', 'already_used': 'already', 'ineligible': 'no_credit',
            'cooldown': 'cooldown', 'unavailable': 'unconfirmed'}.get(result, 'failed')


def use_codex_reset(idempotency_key, credit_id=None):
    """Spend one Codex reset through the running Codex. Reuse the key to retry the same spend."""
    params = {'idempotencyKey': idempotency_key}
    if credit_id:
        params['creditId'] = credit_id

    def build(n):
        def match(m):
            if m.get('id') != n:
                return None
            return ('error', 'refused') if 'error' in m else ('ok', m.get('result'))
        return {'jsonrpc': '2.0', 'id': n, 'method': 'account/rateLimitResetCredit/consume', 'params': params}, match
    try:
        result = CODEX.ask(build)
    except ProbeError as e:
        log.info('codex reset spend: %s', e)
        return 'failed'
    outcome = result.get('outcome') if isinstance(result, dict) else None
    return {'reset': 'reset', 'nothingToReset': 'nothing', 'noCredit': 'no_credit',
            'alreadyRedeemed': 'already'}.get(outcome, 'failed')


def use_reset(reset, key=None):
    """Spend one reset of this kind; pass the same `key` to retry the same spend. Returns a key of OUTCOMES.
    Afterwards Claude's resets are asked again; Codex's come with its next check."""
    key = key or uuid.uuid4().hex
    if reset.provider == 'claude':
        out = use_claude_reset(reset.credit_id, key)
        if out in SPENT:  # gone, even if asking again below fails and keeps the last answer
            with _claude_resets['lock']:
                _claude_resets['value'] = without_one(_claude_resets['value'], reset.kind)
        claude_resets(force=True)
        return out
    return use_codex_reset(key, reset.credit_id)


SPENT = ('reset', 'already')  # outcomes after which that reset is used up
SETTLED = SPENT + ('nothing', 'no_credit')  # outcomes that say for sure what happened to the spend


def without_one(resets, kind):
    """The list with one reset of this kind taken away."""
    return [r if r.kind != kind else Reset(**{**vars(r), 'count': r.count - 1})
            for r in resets if r.kind != kind or r.count > 1]


def resets_line(resets, now):
    """'Free resets: 2 · Weekly reset ×1, Full reset ×1 · first expires Nov 20', or None when there are none."""
    if not resets:
        return None
    line = f'Free resets: {sum(r.count for r in resets)} · ' + ', '.join(f'{r.label} ×{r.count}' for r in resets)
    exp = [r.expires_at for r in resets if r.expires_at and r.expires_at > now]
    if exp:
        d = datetime.fromtimestamp(min(exp))
        line += f" · first expires {d.strftime('%b')} {d.day}"
    return line
