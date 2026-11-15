"""Draw the README pictures with the app's own drawing code, from made-up numbers, at 2x.

    python docs/screenshots/make.py

Fixed fake time, fake email (shown blurred, as the app does) and a fake PC name, so every run gives the same files
and no real account shows. Nothing here asks Claude, Codex or the internet.
"""
import os
import sys
from datetime import datetime

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from usage_tray import draw as D, limits as L  # noqa: E402

NOW = datetime(2026, 11, 16, 8, 20).timestamp()
H, DAY = 3600, 86400
SCALE, WIDTH = 2.0, 680
WHO = {'claude': {'email': 'you@example.com', 'plan': 'Claude Max Subscription'},
       'codex': {'email': 'you@example.com', 'plan': 'ChatGPT Plus Subscription'}}


def states(codex_missing=False):
    claude = L.ProviderState(
        [L.Row('five_hour', 'Session', 6, NOW + 4 * H + 49 * 60, 300),
         L.Row('seven_day', 'Weekly', 7, NOW + 4 * DAY + 13 * H + 40 * 60, 10080),
         L.Row('seven_day_fable', 'Weekly · Fable', 0, NOW + 4 * DAY + 13 * H + 40 * 60, 10080)], NOW,
        resets=[L.Reset('claude', 'fable', 'Fable reset', '', 1, NOW + 114 * DAY + 3 * H, 'g1'),
                L.Reset('claude', 'full', 'Full reset', '', 1, NOW + 25 * DAY + 17 * H, 'g2')],
        account=WHO['claude'])
    codex = L.ProviderState(missing=L.NOT_INSTALLED) if codex_missing else L.ProviderState(
        [L.Row('codex:primary', 'Session', 0, NOW + 4 * H + 59 * 60, 300),
         L.Row('codex:secondary', 'Weekly', 46, NOW + 18 * H + 3 * 60, 10080)], NOW,
        resets=[L.Reset('codex', 'codex', 'Codex reset', '', 1, NOW + 27 * DAY + 17 * H)], account=WHO['codex'])
    return {'claude': claude, 'codex': codex}


def framed(img):
    """Rounded corners and a thin edge, like a Windows 11 window, on a clear background."""
    w, h = img.size
    r = round(8 * SCALE)
    mask = Image.new('L', (w * 3, h * 3), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * 3 - 1, h * 3 - 1], radius=r * 3, fill=255)
    out = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask.resize((w, h), Image.LANCZOS))
    edge = Image.new('RGBA', (w * 3, h * 3), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle([0, 0, w * 3 - 1, h * 3 - 1], radius=r * 3, width=3,
                                           outline=D._rgba(D.mix(D.BG, D.TEXT, 0.18)))
    out.alpha_composite(edge.resize((w, h), Image.LANCZOS))
    return out


def window(st, card=None):
    img, hits = D.render_window(st, NOW, WIDTH, SCALE)
    if card:
        p, key = card.split(':', 1)
        row = next(r for r in st[p].rows if r.key == key)
        patch, xy, _ = D.render_card(img, D.card_model(p, st[p], row, NOW, 'MY-PC'), hits[f'bar:{card}'], SCALE)
        img.paste(patch, xy)
    return framed(img)


def tray():
    """The two numbers as Windows 11 shows them at 200%, in a slice of a dark taskbar, then made 2x bigger again."""
    n = 32  # a tray icon at 200%
    strip = Image.new('RGB', (190, 48), '#1c1c1c')
    d = ImageDraw.Draw(strip)
    x = 14
    for text, prov in (('94', 'claude'), ('100', 'codex')):
        icon = D.render_tray(text, 'ok', prov, n)
        strip.paste(icon, (x, (48 - n) // 2), icon)
        x += n + 16
    f = D.font('segoeui.ttf', 12)
    d.text((x + 6, 15), '8:20 AM', font=f, fill='#ffffff', anchor='lm')
    d.text((x + 6, 33), '11/16/2026', font=f, fill='#ffffff', anchor='lm')
    return framed(strip.resize((strip.width * 2, strip.height * 2), Image.NEAREST))


def small(img):
    """255 colors, dithered so the glass stays smooth (half the file size). The provider logos join what the colors
    are picked from, so their few orange and blue pixels keep their color. Index 255: the clear corners."""
    a = img.getchannel('A')
    rgb = Image.new('RGB', img.size)
    rgb.paste(img, mask=a.point(lambda v: 255 if v else 0))
    pick = Image.new('RGB', (img.width, img.height + 128), D.BG)
    pick.paste(rgb)
    for i, prov in enumerate(('claude', 'codex')):
        logo = Image.open(os.path.join(D.LOGOS, f'{prov}.png')).convert('RGBA')
        for x in range(0, img.width, 64):
            pick.paste(logo, (x, img.height + 64 * i), logo)
    q = rgb.quantize(palette=pick.quantize(255, method=Image.Quantize.MEDIANCUT), dither=Image.Dither.FLOYDSTEINBERG)
    q.putpalette((q.getpalette() + [0] * 768)[:765] + [0, 0, 0])
    q.paste(255, mask=a.point(lambda v: 255 if v == 0 else 0))
    return q


def save(img, name):
    path = os.path.join(HERE, name)
    small(img).save(path, optimize=True, transparency=255)
    print(f'{name}: {img.width}x{img.height}, {os.path.getsize(path) // 1024} KB')


def main():
    try:
        D.use_palette(False)
        save(window(states(), card='codex:codex:secondary'), 'dark.png')
        D.use_palette(True)
        save(window(states()), 'light.png')
        save(window(states(codex_missing=True)), 'not-installed.png')
        D.use_palette(False)
        save(tray(), 'tray.png')
    finally:
        D.use_palette(False)


if __name__ == '__main__':
    main()
