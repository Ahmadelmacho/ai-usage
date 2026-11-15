"""Pictures: the Limits window (T3's look), its hover card, the tray numbers and the app icon. Pure Pillow."""
import math
import os

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from . import limits as L

FONTS = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
SS = 3  # draw window pictures 3x larger, then shrink: smooth edges on rounded shapes

# window colors, one set per theme; use_palette() makes one the module's BG, TEXT, ...
# T3 Code's tokens (neutral dark / zinc light) at T3's contrast 120 and glass 40, blended to plain colors where
# T3 uses see-through ones on the page; RGBA tuples are drawn see-through. Hover card: the dropdown-glass look.
DARK = {'BG': '#000000', 'TEXT': '#f7f7f7', 'MUTED': '#b3b3b3', 'DIM': '#737373', 'WARN': '#f5a524',
        'LINK': '#6cb6ff', 'BORDER': '#131313', 'TRACK': '#080808',
        'FILL': {'codex': '#5b5b5b', 'claude': '#512f23'}, 'HATCH': {'codex': '#383838', 'claude': '#321e18'},
        'LINE': (251, 251, 251, 19), 'CONTROL': (252, 252, 252, 13), 'CONTROL_HOVER': (252, 252, 252, 25),
        'CONTROL_EDGE': (252, 252, 252, 39), 'POPOVER': (17, 17, 17), 'GLASS': 0.508, 'BLUR': 16, 'SATURATE': 1.08,
        'EDGE': (247, 247, 247, 26), 'SHEEN': ('top', (255, 255, 255, 15)), 'SHADOW': (18, 44, -18, 204)}
LIGHT = {'BG': '#fcfcfc', 'TEXT': '#0f0f11', 'MUTED': '#52525a', 'DIM': '#65656c', 'WARN': '#9a5800',
         'LINK': '#0a5fc2', 'BORDER': '#e8e8e9', 'TRACK': '#fafafa',
         'FILL': {'codex': '#a8a8a8', 'claude': '#eeccc1'}, 'HATCH': {'codex': '#cbcbcb', 'claude': '#f3e0d9'},
         'LINE': (218, 218, 221, 153), 'CONTROL': (255, 255, 255, 255), 'CONTROL_HOVER': (249, 249, 250, 255),
         'CONTROL_EDGE': (203, 203, 207, 255), 'POPOVER': (255, 255, 255), 'GLASS': 0.508, 'BLUR': 12,
         'SATURATE': 1.14, 'EDGE': (15, 15, 17, 26), 'SHEEN': ('bottom', (0, 0, 0, 10)), 'SHADOW': (16, 40, -18, 140)}
AMBER = '#f5a524'  # tray badges: the same in both themes
RED = '#e5484d'
TRAY_TEXT = {'claude': '#d97757', 'codex': '#e6e9ee'}


def use_palette(light):
    globals().update(LIGHT if light else DARK)


use_palette(False)

_font_cache = {}


def font(name, px, variation=None):
    key = (name, px, variation)
    if key not in _font_cache:
        f = ImageFont.truetype(os.path.join(FONTS, name), max(1, round(px)))
        if variation:
            f.set_variation_by_name(variation)
        _font_cache[key] = f
    return _font_cache[key]


def mix(a, b, t):
    """Color t of the way from a to b (hex strings)."""
    if t <= 0:
        return a
    if t >= 1:
        return b
    ca, cb = int(a[1:], 16), int(b[1:], 16)
    return '#' + ''.join(f'{round((ca >> s & 255) + ((cb >> s & 255) - (ca >> s & 255)) * t):02x}' for s in (16, 8, 0))


def _rgba(c):
    if isinstance(c, str):
        return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), 255
    return tuple(c) if len(c) == 4 else (*c, 255)


def lerp(a, b, t):
    """Like mix(), for hex strings or RGBA tuples."""
    if isinstance(a, str) and isinstance(b, str):
        return mix(a, b, t)
    a, b = _rgba(a), _rgba(b)
    return tuple(round(x + (y - x) * max(0.0, min(1.0, t))) for x, y in zip(a, b))


def solid(c, under=None):
    """A see-through color as the plain hex it shows over `under` (default: the window background)."""
    if isinstance(c, str):
        return c
    r, g, b, a = _rgba(c)
    return mix(under or BG, f'#{r:02x}{g:02x}{b:02x}', a / 255)


class Pen:
    """Draws in logical units (1 = one 100%-scale pixel) on a canvas k times larger. (dx, dy): canvas pixels cut
    off the top left, so one part of the window can be drawn on a small canvas of its own, rounded the same way.
    RGBA tuple colors blend with what is under them."""

    def __init__(self, img, k, dx=0, dy=0):
        self.img, self.k, self.d, self.dx, self.dy = img, k, ImageDraw.Draw(img, 'RGBA'), dx, dy

    def X(self, x):
        return x * self.k - self.dx

    def Y(self, y):
        return y * self.k - self.dy

    def _b(self, x0, y0, x1, y1):
        # halves round up, never to even: a part drawn on its own canvas lands on the same pixels as the window
        return [math.floor(v + 0.5) for v in (self.X(x0), self.Y(y0), self.X(x1), self.Y(y1))]

    def rrect(self, x0, y0, x1, y1, r, fill=None, outline=None, width=1):
        self.d.rounded_rectangle(self._b(x0, y0, x1, y1), radius=r * self.k, fill=fill, outline=outline,
                                 width=max(1, round(width * self.k)))

    def rect(self, x0, y0, x1, y1, fill):
        self.d.rectangle(self._b(x0, y0, x1, y1), fill=fill)

    def line(self, pts, fill, width):
        self.d.line([(self.X(x), self.Y(y)) for x, y in pts], fill=fill, width=max(1, round(width * self.k)),
                    joint='curve')
        r = width * self.k / 2  # round caps
        for x, y in (pts[0], pts[-1]):
            self.d.ellipse([self.X(x) - r, self.Y(y) - r, self.X(x) + r, self.Y(y) + r], fill=fill)

    def arc(self, cx, cy, r, start, end, fill, width):
        self.d.arc(self._b(cx - r, cy - r, cx + r, cy + r), start, end, fill=fill, width=max(1, round(width * self.k)))

    def poly(self, pts, fill):
        self.d.polygon([(self.X(x), self.Y(y)) for x, y in pts], fill=fill)

    def text(self, x, y, s, f, fill, anchor='la'):
        self.d.text((self.X(x), self.Y(y)), s, font=f, fill=fill, anchor=anchor)

    def width(self, s, f):
        return self.d.textlength(s, font=f) / self.k


# ---------- small glyphs (lucide-style line icons) ----------

def refresh_icon(p, cx, cy, size, color, angle=0.0):
    """angle: turned clockwise by this many degrees (the spin while a check runs)."""
    r, w = size * 0.36, size * 0.11
    for a0, a1 in ((200, 330), (20, 150)):
        p.arc(cx, cy, r, a0 + angle, a1 + angle, color, w)
    for ang, sign in ((330, 1), (150, 1)):  # arrowheads at the end of each arc, pointing along it
        a = math.radians(ang + angle)
        tx, ty = cx + r * math.cos(a), cy + r * math.sin(a)
        dx, dy = -math.sin(a) * sign, math.cos(a) * sign
        h = size * 0.2
        p.poly([(tx + dx * h, ty + dy * h), (tx - dy * h * 0.75, ty + dx * h * 0.75),
                (tx + dy * h * 0.75, ty - dx * h * 0.75)], color)


def gear_icon(p, cx, cy, size, color, hole=None):
    """A solid gear with a hole in the middle (hole: the color behind it, default the window background)."""
    for i in range(8):  # teeth
        a = math.radians(i * 45)
        p.line([(cx, cy), (cx + size * 0.4 * math.cos(a), cy + size * 0.4 * math.sin(a))], color, size * 0.17)
    for r, fill in ((size * 0.31, color), (size * 0.13, solid(hole or BG))):
        p.rrect(cx - r, cy - r, cx + r, cy + r, r, fill=fill)


def pace_icon(p, x, y, size, kind, color):
    s = size / 24
    w = 2 * s
    if kind == 'on':  # lucide gauge
        p.arc(x + 12 * s, y + 14 * s, 10 * s, 150, 30 + 360, color, w)
        p.line([(x + 12 * s, y + 14 * s), (x + 16 * s, y + 10 * s)], color, w)
        return
    pts = [(2, 7), (8.5, 13.5), (13.5, 8.5), (22, 17)]
    head = [(16, 17), (22, 17), (22, 11)]
    if kind == 'over':
        pts = [(a, 24 - b) for a, b in pts]
        head = [(a, 24 - b) for a, b in head]
    p.line([(x + a * s, y + b * s) for a, b in pts], color, w)
    p.line([(x + a * s, y + b * s) for a, b in head], color, w)


def _arc_pts(cx, cy, r, a0, a1, n=8):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def ticket_icon(p, x, y, size, color):
    """Lucide's ticket (T3 marks banked free resets with it), size logical px square at (x, y)."""
    s = size / 24
    outline = (_arc_pts(2, 12, 3, -90, 90) + _arc_pts(4, 17, 2, 180, 90, 4) + _arc_pts(20, 17, 2, 90, 0, 4)
               + _arc_pts(22, 12, 3, 90, 270) + _arc_pts(20, 7, 2, 0, -90, 4) + _arc_pts(4, 7, 2, 270, 180, 4))
    p.line([(x + a * s, y + b * s) for a, b in outline + outline[:1]], color, 2 * s)
    for y0 in (5, 11, 17):
        p.line([(x + 13 * s, y + y0 * s), (x + 13 * s, y + (y0 + 2) * s)], color, 2 * s)


LOGOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logos')  # claude.png, codex.png: 64 px marks


def provider_logo(p, prov, cx, cy, size):
    """The provider's own logo, size logical px square, centered on (cx, cy)."""
    n = round(size * p.k)
    im = Image.open(os.path.join(LOGOS, f'{prov}.png')).convert('RGBA').resize((n, n), Image.LANCZOS)
    p.img.paste(im, (math.floor(p.X(cx) - n / 2 + 0.5), math.floor(p.Y(cy) - n / 2 + 0.5)), im)


# ---------- the window ----------

W_MIN = 520
PAD = 20
CARD_H = 94  # T3: 1 px border, 16 padding, label line 20, gap 4, number line 36
CARD_GAP = 12
HEAD_H = 32  # provider logo and name (20), then a 12 gap
NOTE_H = 54  # a card with one line of text ("Not installed", "No data yet")
SECTION_GAP = 32  # between providers
LINE_H = 22  # the "Couldn't check" / old-numbers line above the cards
STATUS_H = 28  # a 12 gap, then a reset's note ("Using a reset…") under the cards
BAR_X, BAR_Y, BAR_H = 217, 31, 32  # where a card's bar starts, its top and height (T3: 11rem column, gap 24)
NAMES = {'codex': 'Codex', 'claude': 'Claude'}


def _bar(p, x0, y0, x1, y1, row, provider, now, left=None, banked=0):
    """T3's bar: fill = % left, hatch = the used part that comes back at the reset, a white tick at even pace
    with a gap cut around it, and a chip with the countdown and banked free resets at the right end."""
    k = p.k
    bw, bh = round((x1 - x0) * k), round((y1 - y0) * k)
    if bw <= 0 or bh <= 0:
        return
    bar = Image.new('RGB', (bw, bh), TRACK)
    d = ImageDraw.Draw(bar)
    left = max(0.0, min(100.0, 100 - L.used_now(row, now) if left is None else left)) / 100
    fx = round(left * bw)
    if row.resets_at is not None and fx < bw:
        step, lw = 5 * math.sqrt(2) * k, max(1, round(k))
        x = fx - bh
        while x < bw:
            d.line([(x, bh), (x + bh, 0)], fill=HATCH[provider], width=lw)
            x += step
        d.rectangle([0, 0, fx, bh], fill=TRACK)
    if fx > 0:
        d.rectangle([0, 0, fx - 1, bh], fill=FILL[provider])
    t = L.time_left_frac(row, now)
    if t is not None:
        tx = max(3.0, min((x1 - x0) - 3.0, t * (x1 - x0))) * k
        d.rectangle([round(tx - 3 * k), 0, round(tx + 3 * k) - 1, bh], fill=BG)  # the gap shows the card under it
        d.rounded_rectangle([round(tx - k), round(2 * k), round(tx + k) - 1, bh - round(2 * k) - 1], radius=k,
                            fill=TEXT)
    mask = Image.new('L', (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=round(6 * k), fill=255)
    p.img.paste(bar, (math.floor(p.X(x0) + 0.5), math.floor(p.Y(y0) + 0.5)), mask)
    cd = L.countdown(row, now)
    if not cd and not banked:
        return
    f, fb, fs = font('segoeui.ttf', 11 * k), font('seguisb.ttf', 11 * k), font('seguisym.ttf', 11 * k)
    first = f' {cd}' if cd else ''  # after a ↻ (Segoe UI has none; Segoe UI Symbol does)
    num = str(banked) if banked else ''
    w = (p.width('↻', fs) + p.width(first, f) if first else 0) + (6 + p.width('·', f) + 6 if first and banked else 0) \
        + (12 + 2 + p.width(num, fb) if banked else 0)
    cx1, cy = x1 - 8, (y0 + y1) / 2
    cx0 = cx1 - w - 12
    p.rrect(cx0, cy - 9, cx1, cy + 9, 4, fill=(*_rgba(BG)[:3], 217))  # T3: bg-background/85
    x = cx0 + 6
    if first:
        p.text(x, cy, '↻', fs, TEXT, anchor='lm')
        x += p.width('↻', fs)
        p.text(x, cy, first, f, TEXT, anchor='lm')
        x += p.width(first, f)
        if banked:
            p.text(x + 6, cy, '·', f, MUTED, anchor='lm')
            x += 6 + p.width('·', f) + 6
    if banked:
        ticket_icon(p, x, cy - 6, 12, TEXT)
        p.text(x + 14, cy, num, fb, TEXT, anchor='lm')


def _card(p, x0, y0, x1, row, provider, now, left=None, banked=0):
    """left: the % left to show while it glides to a new value (None: the row's own)."""
    k = p.k
    p.rrect(x0, y0, x1, y0 + CARD_H, 8, outline=BORDER)
    p.text(x0 + 17, y0 + 27, row.label, font('seguisb.ttf', 14 * k), TEXT, anchor='lm')
    big = font('seguisb.ttf', 30 * k)
    pct = f'{L.left_pct(row, now) if left is None else round(left)}%'
    base = y0 + 70
    p.text(x0 + 17, base, pct, big, TEXT, anchor='ls')
    lx = x0 + 17 + p.width(pct, big) + 8
    small = font('segoeui.ttf', 14 * k)
    p.text(lx, base, 'left', small, MUTED, anchor='ls')
    kind = L.pace(row, now)
    if kind:
        pace_icon(p, lx + p.width('left', small) + 8, base - 12, 14, kind, MUTED)
    _bar(p, x0 + BAR_X, y0 + BAR_Y, x1 - 17, y0 + BAR_Y + BAR_H, row, provider, now, left, banked)


def _button(p, box, name, fx, r=8):
    """T3's outline button; hover lifts its fill, a click darkens it for a moment. Returns its fill color."""
    hover, press = fx.get(name, 0.0), fx.get(name + '!', 0.0)
    fill = lerp(lerp(CONTROL, CONTROL_HOVER, hover), CONTROL_EDGE, press * 0.6)
    p.rrect(*box, r, fill=fill, outline=CONTROL_EDGE)
    return fill


def _link(p, x, y, text, f, name, fx, fade):
    """Link text; hover fades in an underline, a click dims it for a moment. fade: the fx key of its fade-in."""
    color = mix(BG, mix(LINK, BG, fx.get(name + '!', 0.0) * 0.4), fx.get(fade, 1.0))
    p.text(x, y, text, f, color, anchor='lm')
    if fx.get(name, 0.0):
        p.rect(x, y + 8, x + p.width(text, f), y + 9, mix(BG, color, fx[name]))


def _note_card(p, x0, y0, x1, text):
    p.rrect(x0, y0, x1, y0 + NOTE_H, 8, outline=BORDER)
    p.text(x0 + 17, y0 + NOTE_H / 2, text, font('segoeui.ttf', 14 * p.k), MUTED, anchor='lm')
    return NOTE_H


def window_height(states, now=None, notes=None):
    h = 64
    for i, (prov, st) in enumerate(states.items()):
        h += (SECTION_GAP if i else 0) + HEAD_H
        if st.missing:  # name and a small card that says why, nothing else
            h += NOTE_H
            continue
        h += LINE_H if st.error or (now is not None and L.stale(st, now)) else 0
        h += (len(st.rows) * (CARD_H + CARD_GAP) - CARD_GAP) if st.rows else NOTE_H
        h += STATUS_H if (notes or {}).get(prov) else 0
    return h + PAD


MAX_W = 960  # content column width on a big or maximized window, like T3's centered panel


def render_window(states, now, width=640, scale=1.0, busy=False, height=0, update=None, check=None, notes=None,
                  fx=None, parts=None):
    """states: {'claude': ProviderState, 'codex': ProviderState} in display order. width/height = window size
    in logical pixels; content is a column up to MAX_W wide, centered, on a background that fills the window.

    update: a newer release tag, shown as an "Update to vX" link by the title.
    check: else, the text of a small "Check for updates" button in that place (None: no button).
    notes: {provider: text} shown under that provider's cards ("Using a reset…", an outcome).
    fx: what moves, by name: hover 0-1 per clickable (its hit name), press 0-1 ('<name>!'), 'spin' (refresh icon
    degrees), 'fade:check' / 'fade:<provider>' (0-1 fade-in of that text), 'card:<provider>:<row key>' (% left
    while it glides; None: the row's own).
    parts: a list to fill with the moving parts, for render_part() to redraw just one of them.

    Returns (image at screen pixels, {'refresh': (x0, y0, x1, y1), 'settings', 'update' or 'check',
    'bar:<provider>:<row key>': ...} in screen pixels). A bar's box is where its hover card opens.
    """
    full_w = max(W_MIN, width)
    found = []
    content, hits = _render_content(states, now, min(full_w, MAX_W), scale, busy, update, check, notes or {},
                                    fx or {}, found)
    full = (round(full_w * scale), max(content.height, round(height * scale)))
    ox = (full[0] - content.width) // 2
    if parts is not None:
        parts.extend((keys, box, fn, ox, scale) for keys, box, fn in found)
    if full == content.size:
        return content, hits
    img = Image.new('RGB', full, BG)
    img.paste(content, (ox, 0))
    return img, {name: (x0 + ox, y0, x1 + ox, y1) for name, (x0, y0, x1, y1) in hits.items()}


def render_part(part, fx):
    """Redraw one moving part from render_window(parts=...) with new fx: (patch image, its top-left on screen).
    Every part sits on the plain background, so a small canvas of its own draws it as the whole window would,
    in a fraction of the time."""
    keys, (x0, y0, x1, y1), fn, ox, scale = part
    patch = Image.new('RGB', ((x1 - x0) * SS, (y1 - y0) * SS), BG)
    fn(Pen(patch, scale * SS, x0 * SS, y0 * SS), fx)
    return patch.resize((x1 - x0, y1 - y0), Image.LANCZOS), (x0 + ox, y0)


def _render_content(states, now, width, scale, busy, update, check, notes, fx, parts):
    height = window_height(states, now, notes)
    W, H = round(width * scale), round(height * scale)
    k = scale * SS
    img = Image.new('RGB', (W * SS, H * SS), BG)  # exactly SS x the screen size, so parts line up to the pixel
    p = Pen(img, k)

    def part(box, keys, fn):
        """Draw a moving part now, and keep it with its screen box (2 px of background around it) for redraws."""
        fn(p, fx)
        x0, y0, x1, y1 = box
        parts.append((keys, (max(0, math.floor(x0 * scale) - 2), max(0, math.floor(y0 * scale) - 2),
                             min(W, math.ceil(x1 * scale) + 2), min(H, math.ceil(y1 * scale) + 2)), fn))

    title = font('seguisb.ttf', 16 * k)
    p.text(PAD, 32, 'Usage limits', title, TEXT, anchor='lm')
    hits = {}
    if update:
        f = font('segoeui.ttf', 13 * k)
        text = f'Update to {update}'
        lx = PAD + p.width('Usage limits', title) + 14
        hits['update'] = (lx - 4, 20, lx + p.width(text, f) + 4, 44)
        part(hits['update'], ('update', 'update!'), lambda q, fx: _link(q, lx, 32, text, f, 'update', fx, 'fade:update'))
    elif check:
        f = font('segoeui.ttf', 12 * k)
        bx = PAD + p.width('Usage limits', title) + 14
        hits['check'] = btn_c = (bx, 21, bx + p.width(check, f) + 20, 43)

        def check_btn(q, fx):
            fill = solid(_button(q, btn_c, 'check', fx, 6))
            q.text(bx + 10, 32, check, f, mix(fill, DIM if check == 'Checking…' else MUTED, fx.get('fade:check', 1.0)),
                   anchor='lm')
        part(btn_c, ('check', 'check!', 'fade:check'), check_btn)
    bx1 = width - PAD
    btn = (bx1 - 30, 17, bx1, 47)
    part(btn, ('refresh', 'refresh!', 'spin'), lambda q, fx: (
        _button(q, btn, 'refresh', fx), refresh_icon(q, bx1 - 15, 32, 16, DIM if busy else TEXT, fx.get('spin', 0.0))))
    hits['settings'] = gear = (bx1 - 66, 17, bx1 - 36, 47)
    part(gear, ('settings', 'settings!'),
         lambda q, fx: gear_icon(q, bx1 - 51, 32, 16, TEXT, _button(q, gear, 'settings', fx)))
    checked = [s.checked_at for s in states.values() if s.checked_at]
    status = 'Checking…' if busy else f'Checked {L.clock(max(checked))}' if checked else ''  # old numbers say so below
    p.text(bx1 - 78, 32, status, font('segoeui.ttf', 13 * k), DIM, anchor='rm')

    y = 64
    small = font('segoeui.ttf', 12 * k)
    for i, (prov, st) in enumerate(states.items()):
        y += SECTION_GAP if i else 0
        provider_logo(p, prov, PAD + 10, y + 10, 16)
        p.text(PAD + 28, y + 10, NAMES[prov], font('seguisb.ttf', 14 * k), TEXT, anchor='lm')
        y += HEAD_H
        if st.missing:  # "Not installed" / "Not signed in": no bars, resets or error line
            y += _note_card(p, PAD, y, width - PAD, st.missing)
            continue
        if st.error:
            shown = f' Showing {L.clock(st.checked_at)}.' if st.checked_at and st.rows else ''
            p.text(PAD + 1, y + 8, f"Couldn't check: {st.error}.{shown}", small, WARN, anchor='lm')
            y += LINE_H
        elif L.stale(st, now):
            who = 'Anthropic' if prov == 'claude' else 'OpenAI'
            p.text(PAD + 1, y + 8, f"Numbers from {L.clock(st.checked_at)}. {who} hasn't sent newer ones yet.",
                   small, MUTED, anchor='lm')
            y += LINE_H
        if st.rows:
            banked = sum(r.count for r in st.resets)
            for row in st.rows:
                key = f'card:{prov}:{row.key}'
                part((PAD, y, width - PAD, y + CARD_H), (key,),
                     lambda q, fx, y=y, row=row, key=key, prov=prov, banked=banked: _card(
                         q, PAD, y, width - PAD, row, prov, now, fx.get(key), banked))
                hits[f'bar:{prov}:{row.key}'] = (PAD + BAR_X, y + BAR_Y, width - PAD - 17, y + BAR_Y + BAR_H)
                y += CARD_H + CARD_GAP
            y -= CARD_GAP
        else:
            y += _note_card(p, PAD, y, width - PAD, 'Checking…' if busy or not st.error else 'No data yet')
        note = notes.get(prov)
        if note:
            fade = f'fade:{prov}'
            part((PAD, y + 12, PAD + 2 + p.width(note, small), y + 28), (fade,),
                 lambda q, fx, y=y, note=note, fade=fade: q.text(PAD + 1, y + 20, note, small,
                                                                  mix(BG, MUTED, fx.get(fade, 1.0)), anchor='lm'))
            y += STATUS_H

    out = img.resize((W, H), Image.LANCZOS)
    hits['refresh'] = btn
    return out, {name: tuple(round(v * scale) for v in box) for name, box in hits.items()}


# ---------- the hover card ----------

CARD_W = 320  # T3: w-72 (288) plus 16 padding each side
CARD_PAD = 16
VALUE_X = 100  # T3: a 4.5rem label column and a 12 gap
CARD_OFFSET = 6  # gap between the bar and the card
CARD_MARGIN = 8  # the card stays this far inside the window


def card_model(prov, st, row, now, host=None, busy=False):
    """What the hover card of one bar says, as T3 words it. Fields we can't know honestly are left out."""
    account = st.account or {}
    rows = [('Left', f'{L.left_pct(row, now)}%')]
    cd = L.countdown(row, now)
    if cd:
        rows.append(('Resets', f"{L.when(row.resets_at, now)} · {'resets now' if cd == 'now' else 'in ' + cd}"))
    t = L.time_left_frac(row, now)
    if t is not None:
        rows.append(('Even pace', f'{round(t * 100)}% left by now'))
    back = round(L.used_now(row, now))
    if row.resets_at is not None and back > 0:
        rows.append(('Restores', f'+{back}% of pool'))
    credits = []
    for r in st.resets:
        exp = f' · expires in {L.span(r.expires_at - now)}' if r.expires_at else ''
        credits.append((f'{r.label} · {r.count} left{exp}', 'Use', r.kind) if prov == 'claude'
                       else (f'{r.count} banked{exp}', 'Use reset', r.kind))
    return {'prov': prov, 'name': NAMES[prov], 'email': account.get('email'), 'plan': account.get('plan'),
            'host': host, 'rows': rows, 'credits': credits, 'busy': busy}


def _card_sections(m):
    who = [(label, m[key]) for label, key in (('Plan', 'plan'), ('Signed in', 'host')) if m[key]]
    return [s for s in (who, m['rows']) if s]


def card_height(m):
    h = CARD_PAD + 20 + (2 + 16.5 if m['email'] else 0)
    for rows in _card_sections(m):
        h += 21 + len(rows) * 20 - 4  # divider (10 + 1 + 10), then rows of 16 with gaps of 4
    if m['credits']:
        h += 21 + len(m['credits']) * 30 - 6  # rows of 24 (the button) with gaps of 6
    return math.ceil(h + CARD_PAD)


def card_room(states, now, host=None):
    """Window height (logical px) the tallest hover card needs, so it always fits with its last button."""
    return max((card_height(card_model(p, st, r, now, host)) + 2 * CARD_MARGIN
                for p, st in states.items() if not st.missing for r in st.rows), default=0)


def scramble(s):
    """T3's stand-in text for a hidden email: same length and punctuation, made-up letters (FNV hash seeded)."""
    t = 2166136261
    for ch in s:
        t = ((t ^ ord(ch)) * 16777619) & 0xffffffff
    out = []
    for ch in s:
        if ch in '@.-_':
            out.append(ch)
            continue
        t = ((t ^ (t >> 13)) * 2246822507) & 0xffffffff
        t = ((t ^ (t >> 16)) * 3266489909) & 0xffffffff
        out.append('abcdefghjkmnpqrstuvwxyz23456789'[abs(t - (1 << 32) if t >= 1 << 31 else t) % 31])
    return ''.join(out)


def _fit(q, s, f, room):
    """s, cut with an ellipsis to fit room logical px."""
    if q.width(s, f) <= room:
        return s
    while s and q.width(s + '…', f) > room:
        s = s[:-1]
    return s.rstrip() + '…'


def place_card(size, anchor, scale, m):
    """Top-left of the card in screen px: above the bar like T3, below it when there is no room, always inside."""
    W, H = size
    cw, ch = round(min(CARD_W, W / scale - 2 * CARD_MARGIN) * scale), round(card_height(m) * scale)
    gap, margin = CARD_OFFSET * scale, CARD_MARGIN * scale
    x = round(max(margin, min(W - margin - cw, (anchor[0] + anchor[2]) / 2 - cw / 2)))
    y = anchor[1] - gap - ch
    if y < margin:
        y = anchor[3] + gap if anchor[3] + gap + ch <= H - margin else max(margin, H - margin - ch)
    return x, round(y), cw, ch


def render_card(screen, m, anchor, scale=1.0, fx=None, reveal=False):
    """The hover card over one bar, glass included, on a patch of the window picture `screen` (screen px).
    anchor: the bar's box. reveal: show the email (else T3's blurred stand-in).
    Returns (patch image, its top-left, {'card': box, 'email': box, 'use:<prov>:<kind>': box} in screen px)."""
    fx = fx or {}
    x, y, cw, ch = place_card(screen.size, anchor, scale, m)
    # T3's drop shadow: the card shrunk by -spread, moved down dy, blurred (CSS blur = 2 sigma)
    dy, blur, spread = (v * scale for v in SHADOW[:3])
    alpha = SHADOW[3]
    shadow = (x - spread, y - spread + dy, x + cw + spread, y + ch + spread + dy)
    ox, oy = max(0, math.floor(min(x, shadow[0] - blur))), max(0, math.floor(min(y, shadow[1] - blur)))
    region = screen.crop((ox, oy, min(screen.width, math.ceil(max(x + cw, shadow[2] + blur))),
                          min(screen.height, math.ceil(max(y + ch, shadow[3] + blur)))))
    shade = Image.new('L', region.size, 0)
    ImageDraw.Draw(shade).rectangle([shadow[0] - ox, shadow[1] - oy, shadow[2] - ox, shadow[3] - oy], fill=alpha)
    region.paste((0, 0, 0), (0, 0), shade.filter(ImageFilter.GaussianBlur(blur / 2)))

    # frosted glass: the window behind the card blurred, a little more saturated, under T3's see-through popover
    b = round(BLUR * scale)
    cb = (max(0, x - 2 * b), max(0, y - 2 * b), min(screen.width, x + cw + 2 * b), min(screen.height, y + ch + 2 * b))
    glass = screen.crop(cb).filter(ImageFilter.GaussianBlur(b)).crop((x - cb[0], y - cb[1], x - cb[0] + cw,
                                                                      y - cb[1] + ch))
    glass = Image.blend(ImageEnhance.Color(glass).enhance(SATURATE), Image.new('RGB', glass.size, POPOVER), GLASS)

    canvas = glass.resize((cw * SS, ch * SS), Image.BILINEAR)
    q = Pen(canvas, scale * SS)
    w = cw / scale
    hits = {'card': (x, y, x + cw, y + ch)}
    under = '#%02x%02x%02x' % POPOVER  # what small solid shapes sit on, close enough on glass
    yy = CARD_PAD
    provider_logo(q, m['prov'], CARD_PAD + 10, yy + 10, 16)
    q.text(CARD_PAD + 28, yy + 10, m['name'], font('seguisb.ttf', 14 * q.k), TEXT, anchor='lm')
    yy += 20
    if m['email']:
        yy += 2
        f = font('consola.ttf', 11 * q.k)
        shown = m['email'] if reveal else scramble(m['email'])
        tw = q.width(shown, f)
        if reveal:
            q.text(CARD_PAD, yy + 8.25, shown, f, MUTED, anchor='lm')
        else:  # T3: blur-xs over made-up letters, so the real address is never drawn
            pad = 12
            ink = Image.new('L', (round((tw + 2 * pad) * q.k), round((16.5 + 2 * pad) * q.k)), 0)
            ImageDraw.Draw(ink).text((pad * q.k, (pad + 8.25) * q.k), shown, font=f, fill=255, anchor='lm')
            canvas.paste(MUTED, (round((CARD_PAD - pad) * q.k), round((yy - pad) * q.k)),
                         ink.filter(ImageFilter.GaussianBlur(4 * q.k)))
        hits['email'] = tuple(round(v) for v in (x + CARD_PAD * scale, y + yy * scale, x + (CARD_PAD + tw) * scale,
                                                  y + (yy + 16.5) * scale))
        yy += 16.5
    f = font('segoeui.ttf', 12 * q.k)

    def divider(yy):
        q.rect(CARD_PAD, yy + 10, w - CARD_PAD, yy + 11, LINE)
        return yy + 21

    for rows in _card_sections(m):
        yy = divider(yy)
        for label, value in rows:
            q.text(CARD_PAD, yy + 8, label, f, MUTED, anchor='lm')
            q.text(VALUE_X, yy + 8, _fit(q, value, f, w - CARD_PAD - VALUE_X), f, TEXT, anchor='lm')
            yy += 20
        yy -= 4
    if m['credits']:
        yy = divider(yy)
        fb = font('seguisb.ttf', 12 * q.k)
        for summary, action, kind in m['credits']:
            action = 'Using…' if m['busy'] else action
            bw = q.width(action, fb) + 16
            bbox = (w - CARD_PAD - bw, yy, w - CARD_PAD, yy + 24)
            name = f"use:{m['prov']}:{kind}"
            fill = _button(q, bbox, name, {} if m['busy'] else fx, 6)
            q.text(bbox[0] + bw / 2, yy + 12, action, fb, solid(lerp(fill, TEXT, 0.45 if m['busy'] else 1), under),
                   anchor='mm')
            q.text(CARD_PAD, yy + 12, _fit(q, summary, f, bbox[0] - 12 - CARD_PAD), f, MUTED, anchor='lm')
            if not m['busy']:
                hits[name] = tuple(round(v) for v in (x + bbox[0] * scale, y + yy * scale, x + bbox[2] * scale,
                                                      y + bbox[3] * scale))
            yy += 30
    e = 1 / q.k  # one canvas pixel: the last row and column
    q.rrect(0, 0, w - e, ch / scale - e, 8, outline=EDGE)
    side, color = SHEEN  # T3: a 1 px light line on the top edge (dark) or a dark one on the bottom edge (light)
    ly = 0 if side == 'top' else (ch - 1) / scale
    q.rect(8, ly, w - 8, ly + 1 / scale - e, color)

    card = canvas.resize((cw, ch), Image.LANCZOS)
    mask = Image.new('L', (cw * SS, ch * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, cw * SS - 1, ch * SS - 1], radius=8 * scale * SS, fill=255)
    region.paste(card, (x - ox, y - oy), mask.resize((cw, ch), Image.LANCZOS))
    return region, (ox, oy), hits


# ---------- tray numbers and app icon ----------

def render_tray(text, lvl, provider, n, color=None):
    """One tray icon, n x n pixels: the number in color (default: the provider's), or a colored badge when the
    limit runs low."""
    img = Image.new('RGBA', (n, n), (0, 0, 0, 0))
    color = color or TRAY_TEXT[provider]
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
