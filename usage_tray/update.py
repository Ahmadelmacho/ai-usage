"""Update check against GitHub Releases, and the one-click update (runs get.ps1 in a visible PowerShell window)."""
import json
import logging
import os
import re
import ssl
import subprocess
import tempfile
import urllib.request

import truststore

from . import __version__

REPO = 'Ahmadelmacho/ai-usage'
API = 'https://api.github.com'
offered = None  # the newer tag check() last found: the update click runs get.ps1 from that release, not from main
log = logging.getLogger('ai-usage')
# Windows' own certificate check. A clean Windows PC ships few root certificates and fetches the others only when
# Windows' own check asks; Python's bundled OpenSSL never asks, so it failed on GitHub's (CERTIFICATE_VERIFY_FAILED).
TLS = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def parse(tag):
    """'v1.2.3' -> (1, 2, 3); None for anything else (e.g. 'v1.2-beta', 'latest')."""
    m = re.fullmatch(r'v?(\d+(?:\.\d+)*)', tag.strip()) if isinstance(tag, str) else None
    return tuple(int(x) for x in m.group(1).split('.')) if m else None


def newer(tag, current=__version__):
    a, b = parse(tag), parse(current)
    return bool(a and b) and a > b


def check(api=API, current=__version__, strict=False):
    """The latest release tag when it is newer than this app, else None. Never raises, unless strict
    (a check the user asked for, so they can be told it failed)."""
    req = urllib.request.Request(f'{api}/repos/{REPO}/releases/latest',
                                 headers={'User-Agent': 'ai-usage-updater', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(req, timeout=10, context=TLS) as r:
            rel = json.load(r)
        if rel.get('draft') or rel.get('prerelease'):
            return None
        tag = rel.get('tag_name')
    except Exception as e:  # offline, 404 (no release yet), rate limit, bad JSON: just try again later
        log.info('update check failed: %s', e)
        if strict:
            raise
        return None
    if not newer(tag, current):
        return None
    global offered
    offered = tag
    return tag


CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def start(popen=subprocess.Popen, tag=None):
    """Run get.ps1 of the offered release (tag, else the one check() found) in its own visible window. The installer
    stops this app and starts the new one, so the PowerShell must outlive this app: its own console and process
    group, out of any job the app may be in."""
    url = f'https://raw.githubusercontent.com/{REPO}/{(tag or offered or "main").strip()}/get.ps1'  # check() passed it parse()
    ps = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'WindowsPowerShell', 'v1.0',
                      'powershell.exe')  # by full path: never a powershell.exe in the current folder or on PATH
    argv = [ps, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', f"irm {url} | iex; Read-Host 'Press Enter to close'"]
    flags = CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP
    cwd = tempfile.gettempdir()  # never the install folder, which the installer replaces
    # without PSModulePath, Windows PowerShell builds its own; a pwsh 7 one (app started from a pwsh 7 terminal)
    # makes it load pwsh 7's modules, and their commands fail
    env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
    try:
        return popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, cwd=cwd, close_fds=True, env=env)
    except OSError:  # a job that forbids breakaway refuses the flag; the app never puts itself in a job
        return popen(argv, creationflags=flags, cwd=cwd, close_fds=True, env=env)
