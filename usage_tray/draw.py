"""Pictures: the Limits window (T3's look), the tray numbers and the app icon. Pure Pillow, no UI toolkit."""
import math
import os

from PIL import Image, ImageDraw, ImageFont

from . import limits as L

FONTS = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
SS = 3  # draw window pictures 3x larger, then shrink: smooth edges on rounded shapes

BG = '#17212b'
BORDER = '#2f404f'
TEXT = '#fffaff'
MUTED = '#969ca6'
DIM = '#6c7078'
HATCH_BG = '#223444'
HATCH = '#4f5c69'
CHIP = '#19242f'
CHIP_EDGE = '#2a3644'
AMBER = '#f5a524'
RED = '#e5484d'
CLAUDE = '#d97757'
LINK = '#6cb6ff'
FILL = {'codex': '#707b85', 'claude': '#634c4a'}
TRAY_TEXT = {'claude': CLAUDE, 'codex': '#e6e9ee'}

_font_cache = {}


def font(name, px, variation=None):
    key = (name, px, variation)
    if key not in _font_cache:
        f = ImageFont.truetype(os.path.join(FONTS, name), max(1, round(px)))
        if variation:
            f.set_variation_by_name(variation)
        _font_cache[key] = f
    return _font_cache[key]


class Pen:
    """Draws in logical units (1 = one 100%-scale pixel) on a canvas k times larger."""

    def __init__(self, img, k):
        self.img, self.k, self.d = img, k, ImageDraw.Draw(img)

    def _b(self, x0, y0, x1, y1):
        k = self.k
        return [round(x0 * k), round(y0 * k), round(x1 * k), round(y1 * k)]

    def rrect(self, x0, y0, x1, y1, r, fill=None, outline=None, width=1):
        self.d.rounded_rectangle(self._b(x0, y0, x1, y1), radius=r * self.k, fill=fill, outline=outline,
                                 width=max(1, round(width * self.k)))

    def rect(self, x0, y0, x1, y1, fill):
        self.d.rectangle(self._b(x0, y0, x1, y1), fill=fill)

    def line(self, pts, fill, width):
        self.d.line([(x * self.k, y * self.k) for x, y in pts], fill=fill, width=max(1, round(width * self.k)),
                    joint='curve')
        r = width * self.k / 2  # round caps
        for x, y in (pts[0], pts[-1]):
            self.d.ellipse([x * self.k - r, y * self.k - r, x * self.k + r, y * self.k + r], fill=fill)

    def arc(self, cx, cy, r, start, end, fill, width):
        self.d.arc(self._b(cx - r, cy - r, cx + r, cy + r), start, end, fill=fill, width=max(1, round(width * self.k)))

    def poly(self, pts, fill):
        self.d.polygon([(x * self.k, y * self.k) for x, y in pts], fill=fill)

    def text(self, x, y, s, f, fill, anchor='la'):
        self.d.text((x * self.k, y * self.k), s, font=f, fill=fill, anchor=anchor)

    def width(self, s, f):
        return self.d.textlength(s, font=f) / self.k


# ---------- small glyphs (lucide-style line icons) ----------

def refresh_icon(p, cx, cy, size, color):
    r, w = size * 0.36, size * 0.11
    for a0, a1 in ((200, 330), (20, 150)):
        p.arc(cx, cy, r, a0, a1, color, w)
    for ang, sign in ((330, 1), (150, 1)):  # arrowheads at the end of each arc, pointing along it
        a = math.radians(ang)
        tx, ty = cx + r * math.cos(a), cy + r * math.sin(a)
        dx, dy = -math.sin(a) * sign, math.cos(a) * sign
        h = size * 0.2
        p.poly([(tx + dx * h, ty + dy * h), (tx - dy * h * 0.75, ty + dx * h * 0.75),
                (tx + dy * h * 0.75, ty - dx * h * 0.75)], color)


def pace_icon(p, x, y, size, kind, color):
    s = size / 24
    w = 2 * s
    if kind == 'on':  # gauge
        p.arc(x + 12 * s, y + 13 * s, 9.5 * s, 150, 30 + 360, color, w)
        p.line([(x + 12 * s, y + 14 * s), (x + 16 * s, y + 10 * s)], color, w)
        return
    pts = [(2, 7), (8.5, 13.5), (13.5, 8.5), (22, 17)]
    head = [(16, 17), (22, 17), (22, 11)]
    if kind == 'over':
        pts = [(a, 24 - b) for a, b in pts]
        head = [(a, 24 - b) for a, b in head]
    p.line([(x + a * s, y + b * s) for a, b in pts], color, w)
    p.line([(x + a * s, y + b * s) for a, b in head], color, w)


LOGOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logos')  # claude.png, codex.png: 64 px marks


def provider_logo(p, prov, cx, cy, size):
    """The provider's own logo, size logical px square, centered on (cx, cy)."""
    n = round(size * p.k)
    im = Image.open(os.path.join(LOGOS, f'{prov}.png')).convert('RGBA').resize((n, n), Image.LANCZOS)
    p.img.paste(im, (round(cx * p.k - n / 2), round(cy * p.k - n / 2)), im)


# ---------- the window ----------

W_MIN = 520
PAD = 20
CARD_H = 84
CARD_GAP = 12
LEFT_COL = 220


def _bar(p, x0, y0, x1, y1, row, provider, now):
    k = p.k
    bw, bh = round((x1 - x0) * k), round((y1 - y0) * k)
    if bw <= 0 or bh <= 0:
        return
    bar = Image.new('RGB', (bw, bh), HATCH_BG)
    d = ImageDraw.Draw(bar)
    step, lw = 8.5 * k, max(1, round(1.6 * k))
    x = -bh
    while x < bw + bh:
        d.line([(x, bh), (x + bh, 0)], fill=HATCH, width=lw)
        x += step
    left = 1 - L.used_now(row, now) / 100
    if left > 0:
        d.rectangle([0, 0, round(left * bw), bh], fill=FILL[provider])
    t = L.time_left_frac(row, now)
    if t is not None:
        mx = round(t * bw)
        d.rectangle([mx - round(2.5 * k), 0, mx + round(2.5 * k), bh], fill=BG)
        d.rectangle([mx - round(1 * k), 0, mx + round(1 * k), bh], fill=TEXT)
    mask = Image.new('L', (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=round(6 * k), fill=255)
    p.img.paste(bar, (round(x0 * k), round(y0 * k)), mask)
    cd = L.countdown(row, now)
    if cd:
        f = font('seguisb.ttf', 12 * k)
        tw = p.width(cd, f)
        cw, ch = tw + 28, (y1 - y0) - 12
        cx1, cy0 = x1 - 6, y0 + 6
        p.rrect(cx1 - cw, cy0, cx1, cy0 + ch, 4, fill=CHIP, outline=CHIP_EDGE)
        refresh_icon(p, cx1 - cw + 11, cy0 + ch / 2, 12, TEXT)
        p.text(cx1 - 7, cy0 + ch / 2, cd, f, TEXT, anchor='rm')


def _card(p, x0, y0, x1, row, provider, now):
    k = p.k
    p.rrect(x0, y0, x1, y0 + CARD_H, 8, outline=BORDER)
    p.text(x0 + 19, y0 + 14, row.label, font('seguisb.ttf', 14.5 * k), TEXT)
    big = font('segoeuib.ttf', 31 * k)
    pct = f'{L.left_pct(row, now)}%'
    base = y0 + 70
    p.text(x0 + 18, base, pct, big, TEXT, anchor='ls')
    lx = x0 + 18 + p.width(pct, big) + 9
    small = font('segoeui.ttf', 14.5 * k)
    p.text(lx, base - 1, 'left', small, MUTED, anchor='ls')
    kind = L.pace(row, now)
    if kind:
        pace_icon(p, lx + p.width('left', small) + 8, base - 13, 14, kind, MUTED)
    _bar(p, x0 + LEFT_COL + 20, y0 + 25, x1 - 20, y0 + CARD_H - 25, row, provider, now)


def _note_card(p, x0, y0, x1, text):
    p.rrect(x0, y0, x1, y0 + 60, 8, outline=BORDER)
    p.text(x0 + 19, y0 + 30, text, font('segoeui.ttf', 14 * p.k), MUTED, anchor='lm')
    return 60


def window_height(states, now=None):
    h = 64
    for st in states.values():
        h += 32 + (22 if st.error or (now is not None and L.stale(st, now)) else 0)
        h += (len(st.rows) * (CARD_H + CARD_GAP) - CARD_GAP) if st.rows else 60
        h += 22
    return h + PAD - 22


MAX_W = 960  # content column width on a big or maximized window, like T3's centered panel


def render_window(states, now, width=640, scale=1.0, busy=False, height=0, update=None, check=None):
    """states: {'codex': ProviderState, 'claude': ProviderState} in display order. width/height = window size
    in logical pixels; content is a column up to MAX_W wide, centered, on a background that fills the window.

    update: a newer release tag, shown as an "Update to vX" link by the title.
    check: else, the text of a small "Check for updates" button in that place (None: no button).

    Returns (image at screen pixels, {'refresh': (x0, y0, x1, y1), 'update' or 'check': ...} in screen pixels).
    """
    full_w = max(W_MIN, width)
    content, hits = _render_content(states, now, min(full_w, MAX_W), scale, busy, update, check)
    full = (round(full_w * scale), max(content.height, round(height * scale)))
    if full == content.size:
        return content, hits
    img = Image.new('RGB', full, BG)
    ox = (full[0] - content.width) // 2
    img.paste(content, (ox, 0))
    return img, {name: (x0 + ox, y0, x1 + ox, y1) for name, (x0, y0, x1, y1) in hits.items()}


def _render_content(states, now, width, scale, busy, update=None, check=None):
    height = window_height(states, now)
    k = scale * SS
    img = Image.new('RGB', (round(width * k), round(height * k)), BG)
    p = Pen(img, k)

    title = font('seguisb.ttf', 16 * k)
    p.text(PAD, 32, 'Usage limits', title, TEXT, anchor='lm')
    hits = {}
    if update:
        f = font('segoeui.ttf', 13 * k)
        text = f'Update to {update}'
        lx = PAD + p.width('Usage limits', title) + 14
        p.text(lx, 32, text, f, LINK, anchor='lm')
        hits['update'] = (lx - 4, 20, lx + p.width(text, f) + 4, 44)
    elif check:
        f = font('segoeui.ttf', 12 * k)
        bx = PAD + p.width('Usage limits', title) + 14
        btn_c = (bx, 21, bx + p.width(check, f) + 20, 43)
        p.rrect(*btn_c, 6, outline=BORDER)
        p.text(bx + 10, 32, check, f, DIM if check == 'Checking…' else MUTED, anchor='lm')
        hits['check'] = btn_c
    bx1 = width - PAD
    btn = (bx1 - 30, 17, bx1, 47)
    p.rrect(*btn, 6, outline=BORDER)
    refresh_icon(p, bx1 - 15, 32, 16, DIM if busy else TEXT)
    checked = [s.checked_at for s in states.values() if s.checked_at]
    status = 'Checking…' if busy else f'Checked {L.clock(max(checked))}' if checked else ''  # old numbers say so below
    p.text(bx1 - 42, 32, status, font('segoeui.ttf', 13 * k), DIM, anchor='rm')

    y = 64
    names = {'codex': 'Codex', 'claude': 'Claude'}
    for prov, st in states.items():
        provider_logo(p, prov, PAD + 9, y + 11, 17)
        p.text(PAD + 26, y + 11, names[prov], font('seguisb.ttf', 15.5 * k), TEXT, anchor='lm')
        y += 32
        if st.error:
            shown = f' Showing {L.clock(st.checked_at)}.' if st.checked_at and st.rows else ''
            p.text(PAD + 1, y + 8, f"Couldn't check: {st.error}.{shown}", font('segoeui.ttf', 13 * k), AMBER,
                   anchor='lm')
            y += 22
        elif L.stale(st, now):
            who = 'Anthropic' if prov == 'claude' else 'OpenAI'
            p.text(PAD + 1, y + 8, f"Numbers from {L.clock(st.checked_at)}. {who} hasn't sent newer ones yet.",
                   font('segoeui.ttf', 13 * k), MUTED, anchor='lm')
            y += 22
        if st.rows:
            for row in st.rows:
                _card(p, PAD, y, width - PAD, row, prov, now)
                y += CARD_H + CARD_GAP
            y -= CARD_GAP
        else:
            y += _note_card(p, PAD, y, width - PAD, 'Checking…' if busy or not st.error else 'No data yet')
        y += 22

    out = img.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
    hits['refresh'] = btn
    return out, {name: tuple(round(v * scale) for v in box) for name, box in hits.items()}


# ---------- tray numbers and app icon ----------

def render_tray(text, lvl, provider, n):
    """One tray icon, n x n pixels: the number, or a colored badge when the limit runs low."""
    img = Image.new('RGBA', (n, n), (0, 0, 0, 0))
    color = TRAY_TEXT.get(provider, TEXT)
    if lvl in ('amber', 'red'):
        big = Image.new('RGBA', (n * 4, n * 4), (0, 0, 0, 0))
        ImageDraw.Draw(big).rounded_rectangle([0, 0, n * 4 - 1, n * 4 - 1], radius=n * 0.8,
                                              fill=AMBER if lvl == 'amber' else RED)
        img = big.resize((n, n), Image.LANCZOS)
        color = '#14171c' if lvl == 'amber' else '#ffffff'
    elif lvl == 'none':
        color = '#8b949e'
    ref = '8' * max(2, len(text))  # size as if it were "88" or "888", so every number and the dash match
    variation = 'Bold SemiCondensed' if len(ref) == 2 else 'Bold Condensed'
    room = n - (2 if lvl in ('amber', 'red') else 0)
    f = font('bahnschrift.ttf', 5, variation)
    for px in range(n * 2, 4, -1):  # biggest size that fits
        cand = font('bahnschrift.ttf', px, variation)
        box = _ink(ref, cand, color)
        if box.width <= room and box.height <= n * 0.82:
            f = cand
            break
    ink = _ink(text, f, color)
    img.alpha_composite(ink, ((n - ink.width + 1) // 2, (n - ink.height + 1) // 2))  # center the drawn pixels
    return img


def _ink(text, f, color):
    """The text drawn and cropped to its visible pixels (font boxes include empty ascender space)."""
    size = round(f.size * 3)
    scratch = Image.new('RGBA', (size * len(text) + size, size), (0, 0, 0, 0))
    ImageDraw.Draw(scratch).text((size // 2, size // 3), text, font=f, fill=color)
    return scratch.crop(scratch.getbbox() or (0, 0, 1, 1))


LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')  # app logo


def app_icon(n=256):
    return Image.open(LOGO).convert('RGBA').resize((n, n), Image.LANCZOS)
