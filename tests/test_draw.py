import os

import pytest
from PIL import Image

from usage_tray import draw, limits as L

NOW = 1_800_000_000.0


def states(n_codex=2, n_claude=2, err=None):
    mk = lambda n, mins: [L.Row(f'r{i}', 'Weekly · Sonnet' if i else 'Session', 30 + i, NOW + 3600, mins) for i in range(n)]
    return {'claude': L.ProviderState(mk(n_claude, 10080), NOW - 60),
            'codex': L.ProviderState(mk(n_codex, 300), NOW - 60, err)}


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


@pytest.mark.parametrize('text', ['Check for updates', 'Checking…', 'Up to date (v10.10.10)', "Couldn't check",
                                  'GitHub busy, try at 12:59 PM'])
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
    st = {'claude': L.ProviderState(), 'codex': L.ProviderState([], None, 'no answer in 25 s')}
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


def rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


@pytest.mark.parametrize('light', [False, True])
@pytest.mark.parametrize('scale', [1.0, 1.5])
def test_every_bar_is_a_hover_target_and_resets_show_as_a_chip(scale, light):
    draw.use_palette(light)
    try:
        st = with_resets()
        assert draw.window_height(st) == draw.window_height(states())  # free resets live in the chip and the card
        img, hits = draw.render_window(st, NOW, 640, scale)
        plain, _ = draw.render_window(states(), NOW, 640, scale)
        assert img.height == round(draw.window_height(st) * scale) and not any(k.startswith('use:') for k in hits)
        bars = {k: v for k, v in hits.items() if k.startswith('bar:')}
        assert set(bars) == {'bar:claude:r0', 'bar:claude:r1', 'bar:codex:r0', 'bar:codex:r1'}
        assert bars['bar:claude:r0'][1] < bars['bar:codex:r0'][1]  # Claude first, as before
        x0, y0, x1, y1 = bars['bar:claude:r0']
        assert y1 - y0 == round(draw.BAR_H * scale) and x1 - x0 > 200 * scale
        # the ticket count is drawn in the chip at the bar's right end, nowhere else
        assert img.crop((x0, y0, x1, y1)).tobytes() != plain.crop((x0, y0, x1, y1)).tobytes()
        assert img.crop((0, 0, x0, img.height)).tobytes() == plain.crop((0, 0, x0, img.height)).tobytes()
    finally:
        draw.use_palette(False)


def window_case(case):
    """'both' work, 'claude' only, 'codex' only, or 'none'; the other one is not installed / not signed in."""
    st = with_resets()
    for p, text in (('claude', L.NOT_SIGNED_IN), ('codex', L.NOT_INSTALLED)):
        if case not in ('both', p):
            st[p] = L.ProviderState(missing=text, error='old error', resets=list(st[p].resets))  # none of it shows
    return st


@pytest.mark.parametrize('light', [False, True])
@pytest.mark.parametrize('case', ['both', 'claude', 'codex', 'none'])
def test_missing_provider_is_a_small_card(case, light):
    draw.use_palette(light)
    try:
        st = window_case(case)
        img, hits = draw.render_window(st, NOW, 640, 1.0)
        assert img.height == draw.window_height(st, NOW)
        for p in ('claude', 'codex'):
            shown = case in ('both', p)
            assert any(k.startswith(f'bar:{p}:') for k in hits) == shown  # no bars, so no hover card
            h = draw.window_height({p: st[p]}, NOW)
            assert (h == 64 + draw.HEAD_H + draw.NOTE_H + draw.PAD) != shown  # logo, name and one small card
        fill = {rgb(draw.FILL[p]) for p in ('claude', 'codex')}
        has_bars = sum(px in fill for px in img.convert('RGB').get_flattened_data()) > 500  # gray text edges match a few
        assert has_bars == (case != 'none')
    finally:
        draw.use_palette(False)


def test_a_note_shows_under_the_cards():
    st = with_resets()
    assert draw.window_height(st, notes={'claude': 'Using a reset…'}) == draw.window_height(st) + draw.STATUS_H
    img, hits = draw.render_window(st, NOW, 640, 1.0, notes={'claude': 'Using a reset…'})
    assert img.height == draw.window_height(st, notes={'claude': 'x'})
    plain, plain_hits = draw.render_window(st, NOW, 640, 1.0)
    assert hits['bar:claude:r1'] == plain_hits['bar:claude:r1']  # Claude's cards stay put...
    assert hits['bar:codex:r0'][1] == plain_hits['bar:codex:r0'][1] + draw.STATUS_H  # ...Codex moves down for it


# ---------- the hover card ----------

def card_state(email='you@example.com', plan='Claude Max Subscription'):
    st = with_resets()['claude']
    st.account = {'email': email, 'plan': plan}
    return st


def test_card_words_from_fake_data():
    from datetime import datetime
    now = datetime(2026, 11, 15, 8, 20).timestamp()
    row = L.Row('seven_day', 'Weekly', 7.4, datetime(2026, 11, 19, 22, 0).timestamp(), 10080)
    st = card_state()
    st.resets = [L.Reset('claude', 'fable', 'Fable reset', '', 1, now + 114 * 86400 + 3 * 3600 + 59, 'g1'),
                 L.Reset('claude', 'full', 'Full reset', '', 2, None, 'g2')]
    m = draw.card_model('claude', st, row, now, 'MY-PC')
    assert (m['name'], m['email'], m['plan'], m['host']) == ('Claude', 'you@example.com', 'Claude Max Subscription', 'MY-PC')
    assert m['rows'] == [('Left', '93%'), ('Resets', '11/19 10:00 PM · in 4d 13h'), ('Even pace', '65% left by now'),
                         ('Restores', '+7% of pool')]
    assert m['credits'] == [('Fable reset · 1 left · expires in 114d 3h', 'Use', 'fable'),
                            ('Full reset · 2 left', 'Use', 'full')]
    codex = L.ProviderState(resets=[L.Reset('codex', 'codex', 'Codex reset', '', 1, now + 27 * 86400 + 17 * 3600)])
    m = draw.card_model('codex', codex, L.Row('p', 'Session', 0, now + 3600, 300), now)
    assert m['credits'] == [('1 banked · expires in 27d 17h', 'Use reset', 'codex')]
    assert ('Restores', '+0% of pool') not in m['rows'] and m['rows'][2] == ('Even pace', '20% left by now')


def test_card_leaves_out_what_it_cannot_know():
    st = L.ProviderState([L.Row('m', 'Monthly spend', 40, None, None)], NOW)  # no reset time, no account read
    m = draw.card_model('claude', st, st.rows[0], NOW)
    assert m['rows'] == [('Left', '60%')] and m['credits'] == []
    assert (m['email'], m['plan'], m['host']) == (None, None, None)
    assert draw.card_height(m) < draw.card_height(draw.card_model('claude', card_state(), states()['claude'].rows[0],
                                                                  NOW, 'MY-PC'))


def test_scramble_keeps_the_shape_and_hides_the_letters():
    s = draw.scramble('you.name@example.com')
    assert len(s) == 20 and s[3] == '.' and s[8] == '@' and s[16] == '.' and s != 'you.name@example.com'
    assert s == draw.scramble('you.name@example.com') and s != draw.scramble('you.other@example.com')


@pytest.mark.parametrize('light', [False, True])
@pytest.mark.parametrize('scale', [1.0, 1.5])
def test_card_sits_above_its_bar_and_inside_the_window(scale, light):
    draw.use_palette(light)
    try:
        st = with_resets()
        st['claude'].account = {'email': 'you@example.com', 'plan': 'Claude Max Subscription'}
        img, hits = draw.render_window(st, NOW, 640, scale)
        for name in ('bar:claude:r0', 'bar:codex:r1'):
            p, key = name.split(':')[1:]
            row = next(r for r in st[p].rows if r.key == key)
            m = draw.card_model(p, st[p], row, NOW, 'MY-PC')
            patch, (px, py), ch = draw.render_card(img, m, hits[name], scale)
            x0, y0, x1, y1 = ch['card']
            assert 0 <= px <= x0 and 0 <= py <= y0 and x1 <= px + patch.width <= img.width and y1 <= py + patch.height <= img.height
            assert y1 - y0 == round(draw.card_height(m) * scale) and x1 - x0 == round(draw.CARD_W * scale)
            bx0, by0, bx1, by1 = hits[name]
            above = y1 <= by0 - round(draw.CARD_OFFSET * scale) + 1
            below = y0 >= by1 + round(draw.CARD_OFFSET * scale) - 1
            assert above or below
            assert above == (name == 'bar:codex:r1')  # the top bar has no room above it: its card flips below
            uses = sorted(k for k in ch if k.startswith('use:'))
            assert uses == ([f'use:{p}:{r.kind}' for r in sorted(st[p].resets, key=lambda r: r.kind)])
            for k in uses + ['email'] * (p == 'claude'):
                a0, b0, a1, b1 = ch[k]
                assert x0 <= a0 < a1 <= x1 and y0 <= b0 < b1 <= y1
            assert ('email' in ch) == (p == 'claude')  # Codex has no account here: no email line
    finally:
        draw.use_palette(False)


def test_the_tallest_card_gets_room_in_a_short_window():
    kinds = ['session', 'weekly', 'fable', 'full']
    st = {'claude': L.ProviderState([L.Row('r0', 'Session', 30, NOW + 3600, 300)], NOW,
                                    resets=[L.Reset('claude', k, f'{k} reset', '', 1, None, f'g{k}') for k in kinds]),
          'codex': L.ProviderState(missing=L.NOT_INSTALLED)}
    st['claude'].account = {'email': 'you@example.com', 'plan': 'Claude Max Subscription'}
    room = draw.card_room(st, NOW, 'MY-PC')
    assert room > draw.window_height(st, NOW)  # this card needs more than the window's content
    img, hits = draw.render_window(st, NOW, draw.W_MIN, 1.0, height=room)  # the app makes the window this tall
    m = draw.card_model('claude', st['claude'], st['claude'].rows[0], NOW, 'MY-PC')
    _, _, ch = draw.render_card(img, m, hits['bar:claude:r0'], 1.0)
    x0, y0, x1, y1 = ch['card']
    assert draw.CARD_MARGIN <= x0 and x1 <= img.width - draw.CARD_MARGIN
    assert draw.CARD_MARGIN <= y0 and y1 <= img.height - draw.CARD_MARGIN  # all of it on screen...
    for k in kinds:
        assert y0 <= ch[f'use:claude:{k}'][1] < ch[f'use:claude:{k}'][3] <= y1  # ...down to the last button


def test_card_glass_blurs_what_is_behind_it_and_email_shows_only_on_reveal():
    from PIL import ImageChops
    st = with_resets()
    st['claude'].account = {'email': 'you@example.com', 'plan': None}
    img, hits = draw.render_window(st, NOW, 640, 1.0)
    m = draw.card_model('claude', st['claude'], st['claude'].rows[1], NOW, None)
    hidden, pos, ch = draw.render_card(img, m, hits['bar:claude:r1'], 1.0)
    shown, _, _ = draw.render_card(img, m, hits['bar:claude:r1'], 1.0, reveal=True)
    ex0, ey0, ex1, ey1 = ch['email']
    box = (ex0 - pos[0], ey0 - pos[1], ex1 - pos[0], ey1 - pos[1])
    assert hidden.crop(box).tobytes() != shown.crop(box).tobytes()
    assert ImageChops.difference(hidden, shown).getbbox() is not None
    other = img.copy()
    other.paste((255, 0, 0), (0, 0, img.width, img.height))  # a different window behind: the glass shows it
    tinted, _, _ = draw.render_card(other, m, hits['bar:claude:r1'], 1.0)
    cx = (ch['card'][0] + ch['card'][2]) // 2 - pos[0]
    r, g, b = tinted.getpixel((cx, 4 + ch['card'][1] - pos[1]))
    assert r > g + 40  # red shows through the frosted glass


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
