"""Write preview PNGs from the saved fixtures: window at 100% and 150%, and every tray icon state.
Run: .venv\\Scripts\\python tests\\preview.py <out_dir>"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from usage_tray import draw, limits as L  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'preview')
os.makedirs(out, exist_ok=True)
fx = os.path.join(ROOT, 'tests', 'fixtures')
claude = L.parse_claude(json.load(open(os.path.join(fx, 'claude_usage.json'), encoding='utf-8')))
codex = L.parse_codex(json.load(open(os.path.join(fx, 'codex_ratelimits.json'), encoding='utf-8')))
now = time.time()
states = {'codex': L.ProviderState(codex, now - 120), 'claude': L.ProviderState(claude, now - 120)}
for scale in (1.0, 1.5):
    img, hits = draw.render_window(states, now, 760, scale)
    img.save(os.path.join(out, f'window_{scale}.png'))
    print('window', scale, img.size, hits)
err = {'codex': L.ProviderState(codex, now - 900, 'no answer in 25 s'), 'claude': L.ProviderState([], None, 'Claude Code not found')}
draw.render_window(err, now, 640, 1.0)[0].save(os.path.join(out, 'window_errors.png'))
draw.render_window({'codex': L.ProviderState(), 'claude': L.ProviderState()}, now, 640, 1.0, busy=True)[0].save(
    os.path.join(out, 'window_first_run.png'))
strip = []
for n in (16, 20, 24, 32):
    for text, lvl, prov in (('84', 'ok', 'claude'), ('20', 'ok', 'codex'), ('100', 'ok', 'codex'), ('23', 'amber', 'claude'),
                            ('7', 'red', 'codex'), ('–', 'none', 'claude')):
        strip.append(draw.render_tray(text, lvl, prov, n))
# tray icons on a taskbar-colored strip, 8x zoom too
from PIL import Image  # noqa: E402
sheet = Image.new('RGBA', (6 * 40, 4 * 40), '#1c1c1c')
for i, im in enumerate(strip):
    sheet.alpha_composite(im, ((i % 6) * 40 + 4, (i // 6) * 40 + 4))
sheet.save(os.path.join(out, 'tray_sheet.png'))
sheet.resize((sheet.width * 4, sheet.height * 4), Image.NEAREST).save(os.path.join(out, 'tray_sheet_zoom.png'))
draw.app_icon(64).save(os.path.join(out, 'app_icon.png'))
print('tooltips:')
for name, prov in (('Claude Code', 'claude'), ('Codex', 'codex')):
    t = L.tooltip(name, states[prov], now)
    print(f'--- {len(t)} chars\n{t}')
