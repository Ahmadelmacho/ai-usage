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


@pytest.mark.parametrize('light', [False, True])
def test_both_palettes_render_the_window_with_a_gear_button(light):
    try:
        draw.use_palette(light)
        st = states()
        img, hits = draw.render_window(st, NOW, 640, 1.0)
        bg = tuple(int(draw.BG[i:i + 2], 16) for i in (1, 3, 5))
        assert img.getpixel((2, img.height - 2)) == bg
        x0, y0, x1, y1 = hits['settings']
        assert x1 < hits['refresh'][0] and y0 == hits['refresh'][1]  # just left of refresh, same row
        assert img.getpixel(((x0 + x1) // 2, y0 + 1)) != bg  # its border is drawn where the click area says
    finally:
        draw.use_palette(False)


def test_light_palette_text_has_enough_contrast():
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    for name in ('TEXT', 'MUTED', 'DIM', 'WARN', 'LINK'):
        a, b = sorted((lum(draw.LIGHT[name]), lum(draw.LIGHT['BG'])))
        assert (b + 0.05) / (a + 0.05) >= 4.5, name


def test_tray_number_takes_a_custom_color():
    img = draw.render_tray('84', 'ok', 'codex', 24, '#00ff00')
    assert (0, 255, 0, 255) in [img.getpixel((x, y)) for x in range(24) for y in range(24)]
    assert img.tobytes() != draw.render_tray('84', 'ok', 'codex', 24).tobytes()
    badge = draw.render_tray('7', 'red', 'codex', 24, '#00ff00')
    assert badge.tobytes() == draw.render_tray('7', 'red', 'codex', 24).tobytes()  # low-limit badges stay as they are


RESETS = [L.Reset('claude', 'weekly', 'Weekly reset', '', 1, NOW + 86400 * 8, 'g1'),
          L.Reset('claude', 'full', 'Full reset', '', 1, None, 'g2')]


def with_resets():
    st = states()
    st['claude'].resets = list(RESETS)
    st['codex'].resets = [L.Reset('codex', 'codex', 'Codex reset', '', 1)]
    return st


@pytest.mark.parametrize('light', [False, True])
@pytest.mark.parametrize('scale', [1.0, 1.5])
def test_resets_line_and_use_links(scale, light):
    draw.use_palette(light)
    try:
        st = with_resets()
        assert draw.window_height(st) == draw.window_height(states()) + 2 * (10 + 22 + 22)
        img, hits = draw.render_window(st, NOW, 640, scale)
        assert img.height == round(draw.window_height(st) * scale)
        uses = {k: v for k, v in hits.items() if k.startswith('use:')}
        assert set(uses) == {'use:claude:weekly', 'use:claude:full', 'use:codex:codex'}
        (cx0, cy0, cx1, cy1), (wx0, wy0, wx1, wy1) = uses['use:claude:full'], uses['use:claude:weekly']
        assert wy0 == cy0 and wx1 < cx0  # one line, side by side
        assert hits['use:codex:codex'][1] < wy0  # Codex comes first, as its cards do
        link = tuple(int(draw.LINK[i:i + 2], 16) for i in (1, 3, 5))
        box = img.crop(uses['use:claude:weekly'])
        assert any(sum(abs(a - b) for a, b in zip(px, link)) < 60 for px in box.get_flattened_data())  # drawn in the link color
    finally:
        draw.use_palette(False)


def test_no_resets_no_line():
    _, hits = draw.render_window(states(), NOW, 640, 1.0)
    assert not any(k.startswith('use:') for k in hits)


def test_note_takes_the_links_place():
    st = with_resets()
    img, hits = draw.render_window(st, NOW, 640, 1.0, notes={'claude': 'Using a reset…'})
    assert img.height == draw.window_height(st, notes={'claude': 'x'}) == draw.window_height(st)
    assert 'use:codex:codex' in hits and not any(k.startswith('use:claude') for k in hits)
    st['claude'].resets = []  # the last one used: the note still shows, on its own line
    assert draw.window_height(st, notes={'claude': 'Reset used'}) == draw.window_height(st) + 10 + 22
    img, hits = draw.render_window(st, NOW, 640, 1.0, notes={'claude': 'Reset used'})
    assert img.height == draw.window_height(st, notes={'claude': 'Reset used'})


@pytest.mark.parametrize('scale', [1.0, 1.25, 1.5])
@pytest.mark.parametrize('width', [680, 1300])
def test_a_redrawn_part_matches_a_full_redraw(scale, width):
    """Hover, press, spin and a gliding card, redrawn part by part, look the same as the whole window redrawn."""
    from PIL import ImageChops
    st = states()
    card = 'card:codex:r0'
    a = {}
    b = {'refresh': 0.6, 'spin': 133.0, card: 12.4, 'settings': 1.0, 'check': 1.0, 'check!': 0.5}
    parts = []
    img, _ = draw.render_window(st, NOW, width, scale, check='Check for updates', fx=a, parts=parts)
    want, _ = draw.render_window(st, NOW, width, scale, check='Check for updates', fx=b)
    redrawn = 0
    for part in parts:
        if any(a.get(k) != b.get(k) for k in part[0]):
            im, xy = draw.render_part(part, b)
            img.paste(im, xy)
            redrawn += 1
    assert redrawn == 4
    diff = ImageChops.difference(img, want)
    assert max(hi for _, hi in diff.getextrema()) <= 3  # resampling at the part's edge, invisible
