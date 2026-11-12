"""Never spend one of the owner's real free resets from a test. Both real spend paths are blocked for every test,
and a test that reaches one fails, even when the call happened on another thread. Tests that spend use fakes,
which replace these guards."""
import pytest

from usage_tray import limits as L

CODEX_SPEND = 'account/rateLimitResetCredit/consume'


@pytest.fixture(autouse=True)
def no_real_reset_spend():
    mp = pytest.MonkeyPatch()  # its own, so a test's monkeypatch.undo() can't lift the guard
    hits = []
    real_open, real_send = L._urlopen, L.Session._send

    def guarded_open(req, timeout):
        if req.get_method() != 'GET' or 'reset_rate_limits' in req.full_url:
            hits.append(req.full_url.split('?')[0])
            raise RuntimeError('blocked: a test tried to spend a real Claude reset')
        return real_open(req, timeout)

    def guarded_send(self, msgs):
        if any(isinstance(m, dict) and m.get('method') == CODEX_SPEND for m in msgs):
            hits.append(CODEX_SPEND)
            raise RuntimeError('blocked: a test tried to spend a real Codex reset')
        return real_send(self, msgs)

    mp.setattr(L, '_urlopen', guarded_open)
    mp.setattr(L.Session, '_send', guarded_send)
    yield
    mp.undo()
    assert not hits, f'a test reached a real reset spend: {hits}'
