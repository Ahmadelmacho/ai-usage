"""Save real answers from both CLIs to tests/fixtures/local/ (gitignored: they show your plan and use).
Copy what you need into the committed fixtures by hand, with made-up numbers. Run: .venv\\Scripts\\python tests\\capture_fixtures.py"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from usage_tray import limits  # noqa: E402

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'local')
os.makedirs(out, exist_ok=True)
codex = limits.ask_codex()
codex['accountId'] = '00000000-0000-0000-0000-000000000000'
for name, data in (('claude_usage.json', limits.ask_claude()), ('codex_ratelimits.json', codex)):
    with open(os.path.join(out, name), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1)
    print('saved', name)
