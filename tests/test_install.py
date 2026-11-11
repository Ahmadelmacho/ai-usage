"""Runs share/install.ps1 on a fake unzipped app, in Windows PowerShell 5.1, the one every Windows PC has.
-Dest and -Menus keep every run inside the test folder: no real install, Start menu, Startup folder or registry."""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SHARE = Path(__file__).resolve().parent.parent / 'share'
pytestmark = pytest.mark.skipif(shutil.which('powershell.exe') is None, reason='needs Windows PowerShell')
PS = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass']
# A real, windowless AIUsage.exe: notes the folder it was started in, then waits a minute.
FAKE_APP = '''class P { static void Main() {
    System.IO.File.AppendAllText(System.IO.Path.Combine(System.AppDomain.CurrentDomain.BaseDirectory, "started.txt"),
                                 System.Environment.CurrentDirectory + "\\n");
    System.Threading.Thread.Sleep(60000); } }'''


def compile_exe(folder, code):
    exe = folder / 'AIUsage.exe'
    subprocess.run(PS + ['-Command', f"Add-Type -OutputType WindowsApplication -OutputAssembly '{exe}' "
                                     f"-TypeDefinition @'\n{code}\n'@"], check=True, timeout=120)
    return exe.read_bytes()


@pytest.fixture(scope='module')
def app_exe(tmp_path_factory):
    return compile_exe(tmp_path_factory.mktemp('fakeapp'), FAKE_APP)


@pytest.fixture(scope='module')
def crash_exe(tmp_path_factory):
    """A real AIUsage.exe that fails at start, as a broken build or an antivirus kill would."""
    return compile_exe(tmp_path_factory.mktemp('crashapp'), 'class P { static int Main() { return 3; } }')


@pytest.fixture
def stop_fakes(tmp_path):
    """Stops the fake apps this test started (only those under its own folder)."""
    yield
    subprocess.run(PS + ['-Command', "Get-Process AIUsage -ErrorAction SilentlyContinue | Where-Object { "
                                     f"\"$($_.Path)\".StartsWith('{tmp_path}\\') }} | Stop-Process -Force"], timeout=60)


def fake_unzip(folder, exe=b'fake exe'):
    folder.mkdir()
    if exe is not None:
        (folder / 'AIUsage.exe').write_bytes(exe)
    (folder / '_internal').mkdir()
    (folder / '_internal' / 'base_library.zip').write_bytes(b'fake lib')
    for name in ('install.ps1', 'Install.cmd'):
        shutil.copy(SHARE / name, folder / name)
    return folder


def install(folder, dest, *extra):
    out = subprocess.run(PS + ['-File', str(folder / 'install.ps1'), '-Dest', str(dest), *extra],
                         capture_output=True, text=True, timeout=120)
    return out.stdout + out.stderr


def starts(dest, n, wait=10):
    """The folders the fake app at dest was started in, once it has started n times (or after wait seconds)."""
    log = dest / 'started.txt'
    for _ in range(wait * 10):
        lines = log.read_text().splitlines() if log.exists() else []
        if len(lines) >= n:
            return lines
        time.sleep(0.1)
    return lines


def test_install_then_update_leaves_one_clean_copy(tmp_path):
    dest = tmp_path / 'Programs' / 'AIUsage'
    dest.parent.mkdir()
    first = fake_unzip(tmp_path / 'zip1')
    out = install(first, dest, '-CopyOnly')
    assert 'Install failed' not in out, out
    assert (dest / 'AIUsage.exe').read_bytes() == b'fake exe'

    second = fake_unzip(tmp_path / 'zip2')
    (second / 'AIUsage.exe').write_bytes(b'new exe')
    (dest / 'old_leftover.txt').write_text('from the first install')
    out = install(second, dest, '-CopyOnly')
    assert 'Install failed' not in out, out
    assert (dest / 'AIUsage.exe').read_bytes() == b'new exe'
    assert (dest / '_internal' / 'base_library.zip').exists()
    assert (dest / 'install.ps1').exists() and (dest / 'Install.cmd').exists()
    assert not (dest / 'old_leftover.txt').exists()
    assert sorted(p.name for p in dest.parent.iterdir()) == ['AIUsage']  # no .new or .old left behind


def test_install_from_inside_the_zip_says_unzip_first(tmp_path):
    folder = fake_unzip(tmp_path / 'zip', exe=None)
    dest = tmp_path / 'AIUsage'
    out = install(folder, dest, '-CopyOnly')
    assert 'Install failed: Unzip first' in out
    assert not dest.exists()


def test_install_starts_the_app_in_its_folder_and_update_keeps_startup_off(tmp_path, app_exe, stop_fakes):
    dest, menus = tmp_path / 'AIUsage', tmp_path / 'menus'
    out = install(fake_unzip(tmp_path / 'zip1', app_exe), dest, '-Menus', str(menus))
    assert 'Install failed' not in out, out
    assert starts(dest, 1) == [str(dest)]  # started in the install folder, not the unzipped one
    assert (menus / 'Programs' / 'AI Usage.lnk').exists() and (menus / 'Startup' / 'AI Usage.lnk').exists()

    (menus / 'Startup' / 'AI Usage.lnk').unlink()  # the user turned off the start at sign-in
    out = install(fake_unzip(tmp_path / 'zip2', app_exe), dest, '-Menus', str(menus))  # stops the running copy first
    assert 'Install failed' not in out, out
    assert starts(dest, 1) == [str(dest)]  # a fresh folder: the update replaced started.txt with the zip's files
    assert (menus / 'Programs' / 'AI Usage.lnk').exists()
    assert not (menus / 'Startup' / 'AI Usage.lnk').exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['AIUsage', 'menus', 'zip1', 'zip2']


def test_failed_swap_starts_the_old_app_again(tmp_path, app_exe, stop_fakes):
    """A file held open in the install folder (a terminal, antivirus) blocks the swap: the old app must run again."""
    dest, menus = tmp_path / 'AIUsage', tmp_path / 'menus'
    install(fake_unzip(tmp_path / 'zip1', app_exe), dest, '-Menus', str(menus))
    assert len(starts(dest, 1)) == 1
    with open(dest / 'held.txt', 'w'):
        out = install(fake_unzip(tmp_path / 'zip2', b'new exe'), dest, '-Menus', str(menus))
        assert 'Install failed' in out
        assert len(starts(dest, 2)) == 2, out
    assert (dest / 'AIUsage.exe').read_bytes() == app_exe
    assert not (tmp_path / 'AIUsage.old').exists() and not (tmp_path / 'AIUsage.new').exists()


def test_new_app_that_wont_start_puts_the_old_one_back(tmp_path, app_exe, stop_fakes):
    """Windows or antivirus refuses the new exe (here: not a program at all): the old copy comes back and runs."""
    dest, menus = tmp_path / 'AIUsage', tmp_path / 'menus'
    install(fake_unzip(tmp_path / 'zip1', app_exe), dest, '-Menus', str(menus))
    assert len(starts(dest, 1)) == 1
    out = install(fake_unzip(tmp_path / 'zip2', b'not a program'), dest, '-Menus', str(menus))
    assert 'Install failed' in out
    assert len(starts(dest, 2)) == 2, out
    assert (dest / 'AIUsage.exe').read_bytes() == app_exe
    assert not (tmp_path / 'AIUsage.old').exists()


def test_new_app_that_stops_at_start_puts_the_old_one_back(tmp_path, app_exe, crash_exe, stop_fakes):
    dest, menus = tmp_path / 'AIUsage', tmp_path / 'menus'
    install(fake_unzip(tmp_path / 'zip1', app_exe), dest, '-Menus', str(menus))
    assert len(starts(dest, 1)) == 1
    out = install(fake_unzip(tmp_path / 'zip2', crash_exe), dest, '-Menus', str(menus))
    assert 'Install failed: the new version stopped at start (exit code 3)' in out, out
    assert len(starts(dest, 2)) == 2, out
    assert (dest / 'AIUsage.exe').read_bytes() == app_exe
    assert not (tmp_path / 'AIUsage.old').exists() and not (tmp_path / 'AIUsage.new').exists()


def test_update_leaves_a_copy_from_another_folder_running(tmp_path, app_exe, stop_fakes):
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'AIUsage.exe').write_bytes(app_exe)
    subprocess.Popen([str(other / 'AIUsage.exe')], cwd=other)
    assert len(starts(other, 1)) == 1
    dest = tmp_path / 'AIUsage'
    out = install(fake_unzip(tmp_path / 'zip1', app_exe), dest, '-Menus', str(tmp_path / 'menus'))
    assert 'Install failed' not in out, out
    alive = subprocess.run(PS + ['-Command', f"@(Get-Process AIUsage | Where-Object Path -eq '{other}\\AIUsage.exe').Count"],
                           capture_output=True, text=True, timeout=60).stdout.strip()
    assert alive == '1'
