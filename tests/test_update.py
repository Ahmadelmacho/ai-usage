"""update.py: version compare, check() against a fake GitHub API on 127.0.0.1, and how the updater is started."""
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from usage_tray import __version__, update


@pytest.mark.parametrize('tag, current, want', [
    ('v1.0.1', '1.0.0', True), ('v1.1', '1.0.9', True), ('2.0.0', '1.9.9', True), ('v1.0.10', '1.0.9', True),
    ('v1.0.0', '1.0.0', False), ('1.0', '1.0.0', False),  # (1, 0) < (1, 0, 0): never offers the same version
    ('v0.9.9', '1.0.0', False),
    ('v1.2-beta', '1.0.0', False), ('latest', '1.0.0', False), ('', '1.0.0', False), (None, '1.0.0', False),
    (5, '1.0.0', False),
])
def test_newer(tag, current, want):
    assert update.newer(tag, current) is want


def test_version_parses():
    assert update.parse(__version__)


@pytest.fixture
def api():
    """A fake api.github.com: set .body (bytes) and .status; .paths records what was asked."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            srv.paths.append((self.path, self.headers.get('User-Agent')))
            self.send_response(srv.status)
            self.end_headers()
            self.wfile.write(srv.body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    srv.status, srv.body, srv.paths = 200, b'{}', []
    srv.url = f'http://127.0.0.1:{srv.server_address[1]}'
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def release(tag, **extra):
    return json.dumps({'tag_name': tag, 'draft': False, 'prerelease': False, **extra}).encode()


def test_check_finds_newer(api):
    api.body = release('v9.0.0')
    assert update.check(api.url, '1.0.0') == 'v9.0.0'
    assert api.paths == [(f'/repos/{update.REPO}/releases/latest', 'ai-usage-updater')]


def test_check_same_or_older_is_none(api):
    for tag in ('v1.0.0', 'v0.5.0', 'nightly'):
        api.body = release(tag)
        assert update.check(api.url, '1.0.0') is None


def test_check_ignores_prerelease_and_draft(api):
    for flag in ('prerelease', 'draft'):
        api.body = release('v9.0.0', **{flag: True})
        assert update.check(api.url, '1.0.0') is None


def test_check_bad_answers_are_none(api):
    for status, body in ((200, b'not json'), (200, b'[]'), (404, b'{"message": "Not Found"}'),
                         (403, b'{"message": "API rate limit exceeded"}')):
        api.status, api.body = status, body
        assert update.check(api.url, '1.0.0') is None


def test_check_offline_is_none(api):
    url = api.url
    api.shutdown()
    api.server_close()
    assert update.check(url, '1.0.0') is None


def test_start_runs_the_offered_release_in_its_own_window(api, monkeypatch):
    monkeypatch.setenv('SystemRoot', r'C:\Win')
    api.body = release('v9.0.0')
    assert update.check(api.url, '1.0.0') == 'v9.0.0'
    calls = []
    update.start(lambda argv, **kw: calls.append((argv, kw)))
    (argv, kw), = calls
    # by full path, never a planted powershell.exe in the current folder; get.ps1 of the offered tag, not of main
    assert argv[0] == r'C:\Win\System32\WindowsPowerShell\v1.0\powershell.exe'
    assert argv[-1].startswith('irm https://raw.githubusercontent.com/Ahmadelmacho/ai-usage/v9.0.0/get.ps1 | iex;')
    assert kw['creationflags'] & update.CREATE_NEW_CONSOLE and kw['creationflags'] & update.CREATE_BREAKAWAY_FROM_JOB
    assert kw['cwd'] and kw['close_fds']
    assert 'PATH' in {k.upper() for k in kw['env']} and 'PSMODULEPATH' not in {k.upper() for k in kw['env']}


def test_start_without_breakaway_when_a_job_refuses_it():
    calls = []

    def popen(argv, **kw):
        calls.append(kw['creationflags'])
        if kw['creationflags'] & update.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, 'Access is denied')
    update.start(popen)
    assert len(calls) == 2 and not calls[1] & update.CREATE_BREAKAWAY_FROM_JOB


def test_strict_check_raises_on_failure_but_not_when_up_to_date(api):
    api.body = release('v1.0.0')
    assert update.check(api.url, '1.0.0', strict=True) is None
    api.status, api.body = 404, b'{"message": "Not Found"}'
    with pytest.raises(Exception):
        update.check(api.url, '1.0.0', strict=True)


def test_check_verifies_certificates_with_windows(monkeypatch):
    """A clean Windows PC holds few root certificates and fetches the rest only when Windows' own check asks for
    them, so Python's bundled OpenSSL failed every check there (CERTIFICATE_VERIFY_FAILED, seen on a fresh VM)."""
    import truststore
    seen = {}

    def urlopen(req, timeout, context=None):
        seen['context'] = context
        raise OSError('stop here')
    monkeypatch.setattr(update.urllib.request, 'urlopen', urlopen)
    update.check()
    assert isinstance(seen['context'], truststore.SSLContext)
    assert seen['context'].verify_mode == ssl.CERT_REQUIRED and seen['context'].check_hostname is True
