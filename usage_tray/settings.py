"""The person's choices (window theme, tray number colors), kept in settings.json in the app's data folder."""
import json
import os
import re
import winreg

from . import draw

THEMES = {'dark': 'Dark', 'light': 'Light', 'system': 'Match Windows'}
DEFAULTS = {'theme': 'dark', **draw.TRAY_TEXT}
_COLOR = re.compile(r'#[0-9a-fA-F]{6}')


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
    return out


def save(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1)
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
