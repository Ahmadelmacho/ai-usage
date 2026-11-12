import json

import pytest

from usage_tray import settings as S


def test_missing_file_gives_defaults(tmp_path):
    assert S.load(str(tmp_path / 'nope.json')) == S.DEFAULTS


def test_save_then_load(tmp_path):
    path = str(tmp_path / 'settings.json')
    S.save(path, {'theme': 'light', 'claude': '#112233', 'codex': '#aabbcc'})
    assert S.load(path) == {'theme': 'light', 'claude': '#112233', 'codex': '#aabbcc'}
    assert not (tmp_path / 'settings.json.tmp').exists()


@pytest.mark.parametrize('content', ['{not json', '[]', '5', '', '{"theme": 3}', '{"theme": []}', '{"theme": {}}'])
def test_broken_file_gives_defaults(tmp_path, content):
    (tmp_path / 's.json').write_text(content)
    assert S.load(str(tmp_path / 's.json')) == S.DEFAULTS


def test_each_bad_value_falls_back_alone(tmp_path):
    (tmp_path / 's.json').write_text(json.dumps({'theme': 'pink', 'claude': '#ABCDEF', 'codex': 'red'}))
    assert S.load(str(tmp_path / 's.json')) == {'theme': 'dark', 'claude': '#abcdef', 'codex': S.DEFAULTS['codex']}
    (tmp_path / 's.json').write_text(json.dumps({'theme': 'system', 'claude': '#12345', 'codex': ['#123456']}))
    assert S.load(str(tmp_path / 's.json')) == {**S.DEFAULTS, 'theme': 'system'}


@pytest.mark.parametrize('theme, windows, light', [('dark', True, False), ('light', False, True),
                                                   ('system', True, True), ('system', False, False)])
def test_match_windows_reads_the_windows_mode(monkeypatch, theme, windows, light):
    monkeypatch.setattr(S, 'windows_light', lambda: windows)
    assert S.is_light(theme) is light


def test_windows_light_reads_the_registry(monkeypatch):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(S.winreg, 'OpenKey', lambda *a: Key())
    monkeypatch.setattr(S.winreg, 'QueryValueEx', lambda k, name: (0, 4))
    assert S.windows_light() is False
    monkeypatch.setattr(S.winreg, 'QueryValueEx', lambda k, name: (1, 4))
    assert S.windows_light() is True

    def missing(*a):
        raise FileNotFoundError
    monkeypatch.setattr(S.winreg, 'OpenKey', missing)
    assert S.windows_light() is True  # no value set: Windows treats apps as light
