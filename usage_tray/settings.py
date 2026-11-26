"""The person's choices (window theme, tray number colors), kept in settings.json in the app's data folder."""
import base64
import ctypes
import json
import os
import re
import winreg
from ctypes import wintypes

from . import draw

THEMES = {'dark': 'Dark', 'light': 'Light', 'system': 'Match Windows'}
DEFAULTS = {'theme': 'dark', **draw.TRAY_TEXT, 'hub_url': '', 'hub_key': ''}
_COLOR = re.compile(r'#[0-9a-fA-F]{6}')


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.c_void_p)]


_crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
for _fn in (_crypt32.CryptProtectData, _crypt32.CryptUnprotectData):
    _fn.restype = wintypes.BOOL
    _fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                    wintypes.DWORD, ctypes.POINTER(_Blob)]
_kernel32.LocalFree.restype, _kernel32.LocalFree.argtypes = ctypes.c_void_p, [ctypes.c_void_p]


def _dpapi(fn, data):
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = _Blob(len(data), ctypes.cast(buf, ctypes.c_void_p)), _Blob()
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), 'Windows could not lock or unlock the value')
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        _kernel32.LocalFree(out.data)


def lock(text):
    return base64.b64encode(_dpapi(_crypt32.CryptProtectData, text.encode())).decode()


def unlock(locked):
    try:
        return _dpapi(_crypt32.CryptUnprotectData, base64.b64decode(locked, validate=True)).decode() if locked else ''
    except (OSError, ValueError):
        return ''


def load(path):
    """Saved choices; every missing or bad value falls back to its default."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = dict(DEFAULTS)
    if isinstance(data.get('theme'), str) and data['theme'] in THEMES:
        out['theme'] = data['theme']
    for p in ('claude', 'codex'):
        v = data.get(p)
        if isinstance(v, str) and _COLOR.fullmatch(v):
            out[p] = v.lower()
    if all(isinstance(data.get(k), str) and data[k] for k in ('hub_url', 'hub_key')):
        out['hub_url'], out['hub_key'] = data['hub_url'], data['hub_key']
    return out


def save(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({k: v for k, v in data.items() if v != ''}, f, indent=1)
    os.replace(tmp, path)


def windows_light():
    """Windows' own "apps" mode. No value set means light, as Windows treats it."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            return bool(winreg.QueryValueEx(k, 'AppsUseLightTheme')[0])
    except OSError:
        return True


def is_light(theme):
    return theme == 'light' or (theme == 'system' and windows_light())
