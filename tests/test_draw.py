import os

import pytest
from PIL import Image

from usage_tray import draw, limits as L

NOW = 1_800_000_000.0


def states(n_codex=2, n_claude=2, err=None):
    mk = lambda n, mins: [L.Row(f'r{i}', 'Weekly · Sonnet' if i else 'Session', 30 + i, NOW + 3600, mins) for i in range(n)]
    return {'codex': L.ProviderState(mk(n_codex, 300), NOW - 60, err),
            'claude': L.ProviderState(mk(n_claude, 10080), NOW - 60)}


@pytest.mark.parametrize('scale', [1.0, 1.25, 1.5, 2.0])
def test_window_size_and_refresh_button(scale):
    st = states()
    img, hits = draw.render_window(st, NOW, 700, scale)
    assert img.size == (round(700 * scale), round(draw.window_height(st) * scale))
    x0, y0, x1, y1 = hits['refresh']
    assert 0 < x0 < x1 <= img.width and 0 < y0 < y1 < 80 * scale
    # the refresh button is drawn where the click area says: its border differs from the background
    px = img.getpixel(((x0 + x1) // 2, y0 + 1))
    assert px != tuple(int(draw.BG[i:i + 2], 16) for i in (1, 3, 5))


@pytest.mark.parametrize('scale', [1.0, 1.5])
def test_maximized_window_is_filled_and_centered(scale):
    st = states()
    img, hits = draw.render_window(st, NOW, 2560 / scale, scale, height=1400 / scale)
    assert img.size == (2560, max(1400, round(draw.window_height(st) * scale)))
    col = round(draw.MAX_W * scale)
    x0, _, x1, _ = hits['refresh']
    assert abs(x1 - ((2560 + col) // 2 - draw.PAD * scale)) <= 2  # button sits at the right edge of the centered column
    bg = tuple(int(draw.BG[i:i + 2], 16) for i in (1, 3, 5))
    assert img.getpixel((10, 700)) == bg and img.getpixel((2550, 1390)) == bg


@pytest.mark.parametrize('text', ['Check for updates', 'Checking…', 'Up to date (v10.10.10)', "Couldn't check"])
def test_check_button_fits_by_the_title_and_moves_nothing(text):
    st = states()
    plain, _ = draw.render_window(st, NOW, draw.W_MIN, 1.0)
    img, hits = draw.render_window(st, NOW, draw.W_MIN, 1.0, check=text)
    assert img.size == plain.size  # pushes nothing down
    x0, y0, x1, y1 = hits['check']
    assert 110 < x0 < x1 < 320 and 17 <= y0 < y1 <= 47  # by the title, clear of "Checked 12:00 PM" and refresh
    assert img.crop((0, 60, img.width, img.height)).tobytes() == plain.crop((0, 60, img.width, img.height)).tobytes()
    _, hits = draw.render_window(st, NOW, draw.W_MIN, 1.0, update='v9.0.0', check=text)
    assert 'check' not in hits and 'update' in hits  # an update link takes the button's place


def test_window_never_narrower_than_minimum():
    img, _ = draw.render_window(states(), NOW, 100, 1.0)
    assert img.width == draw.W_MIN


def test_old_numbers_get_a_line_saying_when_they_are_from():
    st = states()
    st['claude'].checked_at = NOW - 1500
    assert draw.window_height(st, NOW) == draw.window_height(states(), NOW) + 22
    img, _ = draw.render_window(st, NOW, 640, 1.0)
    assert img.height == draw.window_height(st, NOW)


def test_window_height_grows_with_rows_and_errors():
    assert draw.window_height(states(3, 2)) > draw.window_height(states(2, 2))
    assert draw.window_height(states(err='x')) == draw.window_height(states()) + 22
    assert draw.window_height(states(0, 0)) < draw.window_height(states(1, 1))


@pytest.mark.parametrize('busy', [True, False])
def test_empty_and_failed_states_render(busy):
    st = {'codex': L.ProviderState([], None, 'Codex not found'), 'claude': L.ProviderState()}
    img, _ = draw.render_window(st, NOW, 640, 1.0, busy)
    assert img.size[1] == draw.window_height(st)


def test_rows_with_odd_values_render():
    rows = [L.Row('a', 'Monthly spend', 100, None, None), L.Row('b', 'Weekly', 0, NOW - 5, 10080),
            L.Row('c', 'Session', 99.6, NOW + 10, 300)]
    draw.render_window({'codex': L.ProviderState(rows, NOW), 'claude': L.ProviderState(rows, NOW)}, NOW, 520, 1.0)


@pytest.mark.parametrize('n', [16, 20, 24, 32, 40])
@pytest.mark.parametrize('text, lvl', [('84', 'ok'), ('100', 'ok'), ('7', 'red'), ('23', 'amber'), ('–', 'none')])
def test_tray_icon_is_square_and_ink_is_centered(n, text, lvl):
    img = draw.render_tray(text, lvl, 'claude', n)
    assert img.size == (n, n) and img.mode == 'RGBA'
    if lvl in ('amber', 'red'):
        assert img.getpixel((n // 2, 1))[3] == 255  # badge fills the icon
        return
    x0, y0, x1, y1 = img.getchannel('A').point(lambda a: 255 if a > 60 else 0).getbbox()
    assert abs(x0 - (n - x1)) <= 2 and abs(y0 - (n - y1)) <= 2  # centered within 1 px each way
    if text != '–':
        assert y1 - y0 >= n * 0.55  # digits fill most of the height


def test_tray_text_colors_differ_by_provider():
    a = draw.render_tray('84', 'ok', 'claude', 24)
    b = draw.render_tray('84', 'ok', 'codex', 24)
    assert a.tobytes() != b.tobytes()


@pytest.mark.parametrize('n', [16, 32, 64, 256])
def test_app_icon_is_the_logo_with_clear_corners(n):
    img = draw.app_icon(n)
    assert img.size == (n, n) and img.mode == 'RGBA'
    assert img.getpixel((0, 0))[3] == 0 and img.getpixel((n // 2, n // 2))[3] > 200


@pytest.mark.parametrize('prov', ['claude', 'codex'])
def test_provider_logo_files_load(prov):
    img = Image.open(os.path.join(draw.LOGOS, f'{prov}.png'))
    assert img.size == (64, 64) and img.mode == 'RGBA'
    assert img.getpixel((0, 0))[3] == 0 and img.getpixel((32, 32))[3] == 255  # clear corner, solid middle
