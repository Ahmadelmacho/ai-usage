"""Runs get.ps1 in Windows PowerShell 5.1 against a fake GitHub on 127.0.0.1 with a small fake AIUsage.zip."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which('powershell.exe') is None, reason='needs Windows PowerShell')


def fake_zip():
    """Same layout as pack.ps1 makes: one 'AI Usage' folder with the app and the installer."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('AI Usage/AIUsage.exe', b'fake exe v2')
        z.writestr('AI Usage/_internal/base_library.zip', b'fake lib')
        for name in ('install.ps1', 'Install.cmd'):
            z.write(ROOT / 'share' / name, f'AI Usage/{name}')
    return buf.getvalue()


@pytest.fixture
def github():
    """Serves /repos/.../releases/latest and the zip; set .digest to change what the API lists."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith('/releases/latest'):
                body = json.dumps({'tag_name': 'v9.0.0', 'draft': False, 'prerelease': False, 'assets': [
                    {'name': 'AIUsage.zip', 'digest': srv.digest,
                     'browser_download_url': f'{srv.url}/download/AIUsage.zip'}]}).encode()
            elif self.path == '/download/AIUsage.zip':
                body = srv.zip
                srv.zip_requests += 1
                if srv.stall_first and srv.zip_requests == 1:  # half the file, then silence
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body[:len(body) // 2])
                    self.wfile.flush()
                    time.sleep(90)
                    return
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    srv.zip = fake_zip()
    srv.zip_requests, srv.stall_first = 0, False
    srv.digest = 'sha256:' + hashlib.sha256(srv.zip).hexdigest()
    srv.url = f'http://127.0.0.1:{srv.server_address[1]}'
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def overrides(api, dest, env=None):
    """get.ps1's test overrides: the fake API, the test folder and copy only, never the real install."""
    return {**(os.environ if env is None else env),
            'AIUSAGE_TEST_API': api, 'AIUSAGE_TEST_DEST': str(dest), 'AIUSAGE_TEST_COPYONLY': '1'}


def get(api, dest, shell='powershell.exe', env=None):
    out = subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'get.ps1')],
                         capture_output=True, text=True, timeout=120, env=overrides(api, dest, env))
    return out.stdout + out.stderr


@pytest.mark.parametrize('shell', ['powershell.exe', pytest.param('pwsh.exe', marks=pytest.mark.skipif(
    shutil.which('pwsh.exe') is None, reason='PowerShell 7 not installed'))])
def test_good_digest_installs(github, tmp_path, shell):
    dest = tmp_path / 'AIUsage'
    out = get(github.url, dest, shell)
    assert 'Install failed' not in out, out
    assert 'Downloading AI Usage v9.0.0' in out
    assert (dest / 'AIUsage.exe').read_bytes() == b'fake exe v2'
    assert (dest / 'install.ps1').exists() and (dest / '_internal' / 'base_library.zip').exists()


def test_update_replaces_the_old_copy(github, tmp_path):
    dest = tmp_path / 'AIUsage'
    dest.mkdir()
    (dest / 'AIUsage.exe').write_bytes(b'old exe')
    out = get(github.url, dest)
    assert 'Install failed' not in out, out
    assert (dest / 'AIUsage.exe').read_bytes() == b'fake exe v2'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['AIUsage']  # no .new or .old left behind


@pytest.mark.skipif(not (Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'curl.exe').exists(),
                    reason='the retry comes from curl.exe; the Invoke-WebRequest fallback has none')
def test_a_download_that_stalls_starts_again(github, tmp_path):
    """The first download sends half the zip, then nothing (a dropped connection). get.ps1 must give up on it and
    download again, not wait forever on a window that says "Downloading"."""
    github.stall_first = True
    dest = tmp_path / 'AIUsage'
    out = get(github.url, dest)
    assert 'Install failed' not in out, out
    assert (dest / 'AIUsage.exe').read_bytes() == b'fake exe v2'
    assert github.zip_requests >= 2


@pytest.mark.parametrize('digest', ['sha256:' + '0' * 64, None, '', 'md5:abc'])
def test_wrong_or_missing_digest_installs_nothing(github, tmp_path, digest):
    github.digest = digest
    dest = tmp_path / 'AIUsage'
    dest.mkdir()
    (dest / 'AIUsage.exe').write_bytes(b'old exe')
    out = get(github.url, dest)
    assert 'Install failed:' in out
    assert (dest / 'AIUsage.exe').read_bytes() == b'old exe'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['AIUsage']  # no AIUsage.new left behind


def test_api_down_says_install_failed(github, tmp_path):
    url = github.url
    github.shutdown()
    github.server_close()
    out = get(url, tmp_path / 'AIUsage')
    assert 'Install failed:' in out
    assert not (tmp_path / 'AIUsage').exists()


def test_works_through_irm_iex(github, tmp_path):
    """The README command pipes the script text into iex, in the user's own shell: it must install, and leave that
    shell's settings and variables (even ones named $Dest or $Api) as they were."""
    dest = tmp_path / 'AIUsage'
    script = ROOT / 'get.ps1'
    out = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Restricted', '-Command',  # Windows' default
                          "$Dest = 'D:/backup'; $Api = 'mine'; $CopyOnly = 'keep'; "
                          f"Get-Content -Raw -LiteralPath '{script}' | iex; "
                          "'|' + (@($ErrorActionPreference, $Dest, $Api, $CopyOnly) -join '|')"],
                         capture_output=True, text=True, timeout=120, env=overrides(github.url, dest))
    assert 'Install failed' not in out.stdout + out.stderr, out.stdout + out.stderr
    assert (dest / 'AIUsage.exe').read_bytes() == b'fake exe v2'
    assert out.stdout.strip().endswith('|Continue|D:/backup|mine|keep')


@pytest.mark.skipif(shutil.which('pwsh.exe') is None, reason='PowerShell 7 not installed')
def test_windows_powershell_under_a_pwsh7_module_path(github, tmp_path):
    """The app started from a pwsh 7 terminal hands pwsh 7's PSModulePath to Windows PowerShell, which then
    loads pwsh 7's script modules (Get-FileHash failed there). get.ps1 must still install."""
    pwsh_path = subprocess.run(['pwsh.exe', '-NoProfile', '-Command', '$env:PSModulePath'],
                               capture_output=True, text=True, timeout=60).stdout.strip()
    assert 'PowerShell' in pwsh_path
    env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
    env['PSModulePath'] = pwsh_path
    dest = tmp_path / 'AIUsage'
    out = get(github.url, dest, env=env)
    assert 'Install failed' not in out, out
    assert (dest / 'AIUsage.exe').read_bytes() == b'fake exe v2'
