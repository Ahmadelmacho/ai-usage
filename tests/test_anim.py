from usage_tray import anim


class FakeRoot:
    """after() that runs nothing by itself: the test calls run() to fire one frame."""

    def __init__(self):
        self.jobs = {}
        self.n = 0

    def after(self, ms, fn):
        self.n += 1
        self.jobs[self.n] = fn
        return self.n

    def after_cancel(self, job):
        self.jobs.pop(job, None)

    def run(self):
        jobs, self.jobs = self.jobs, {}
        for fn in jobs.values():
            fn()


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def make(motion=True):
    root, clock, frames = FakeRoot(), Clock(), []
    a = anim.Animator(root, lambda: frames.append(1), motion=lambda: motion, clock=clock)
    return a, root, clock, frames


def play(a, root, clock, secs, step=0.016):
    while a.running and secs > 0:
        clock.t += step
        secs -= step
        root.run()


def test_ease_out_ends():
    assert anim.ease_out(0) == 0 and anim.ease_out(1) == 1
    assert anim.ease_out(0.5) > 0.5  # fast start


def test_glide_ends_on_exact_value_and_timer_stops():
    a, root, clock, frames = make()
    a.set('bar', 10.0)
    a.to('bar', 73.0, 300)
    assert root.jobs  # a frame is scheduled
    clock.t += 0.15
    root.run()
    assert 10 < a.value('bar') < 73
    play(a, root, clock, 1)
    assert a.value('bar') == 73.0
    assert not a.running and not root.jobs  # idle: no timer left
    n = len(frames)
    root.run()
    assert len(frames) == n


def test_first_value_shows_at_once():
    a, root, *_ = make()
    a.to('bar', 50.0, 300)  # nothing to glide from
    assert a.value('bar') == 50.0 and not root.jobs


def test_same_target_does_not_restart():
    a, root, clock, _ = make()
    a.set('x', 0.0)
    a.to('x', 1.0, 100)
    clock.t += 0.05
    root.run()
    v = a.value('x')
    a.to('x', 1.0, 100)
    assert a.value('x') == v
    clock.t += 0.06
    root.run()
    assert a.value('x') == 1.0


def test_new_target_glides_from_where_it_is():
    a, root, clock, _ = make()
    a.set('x', 0.0)
    a.to('x', 1.0, 100)
    clock.t += 0.05
    root.run()
    mid = a.value('x')
    a.to('x', 0.0, 100)  # e.g. mouse left the button half way through its hover fade
    clock.t += 0.01
    root.run()
    assert a.value('x') < mid
    play(a, root, clock, 1)
    assert a.value('x') == 0.0


def test_one_timer_for_many_tweens():
    a, root, clock, frames = make()
    for k in 'abc':
        a.set(k, 0.0)
        a.to(k, 1.0, 100)
    assert len(root.jobs) == 1
    clock.t += 0.02
    root.run()
    assert len(frames) == 1 and len(root.jobs) == 1


def test_spin_runs_until_settled_then_rests_at_zero():
    a, root, clock, frames = make()
    a.spin('refresh', 900)
    play(a, root, clock, 5)
    assert a.running and 0 <= a.value('refresh') < 360  # still spinning, angle wraps
    a.spin('refresh')  # second start while spinning: no change
    a.settle('refresh')
    play(a, root, clock, 10)
    assert not a.running and not root.jobs
    assert a.value('refresh') == 0.0


def test_settle_without_spin_does_nothing():
    a, root, *_ = make()
    a.settle('refresh')
    assert not a.running and not root.jobs


def test_reduced_motion_shows_end_state_at_once():
    a, root, _, frames = make(motion=False)
    a.set('bar', 10.0)
    a.to('bar', 73.0, 300)
    a.spin('refresh')
    assert a.value('bar') == 73.0 and a.value('refresh') == 0.0
    assert not a.running and not root.jobs


def test_cancel_jumps_to_end_and_stops_timer():
    a, root, clock, _ = make()
    a.set('x', 0.0)
    a.to('x', 5.0, 300)
    a.spin('r')
    a.cancel()
    assert a.value('x') == 5.0 and a.value('r') == 0.0
    assert not a.running and not root.jobs


def test_motion_on_reads_windows_setting():
    assert anim.motion_on() in (True, False)


def test_motion_turned_off_mid_glide_ends_at_once():
    root, clock, frames, on = FakeRoot(), Clock(), [], [True]
    a = anim.Animator(root, lambda: frames.append(1), motion=lambda: on[0], clock=clock)
    a.set('x', 0.0)
    a.to('x', 9.0, 300)
    a.spin('r')
    on[0] = False
    clock.t += 0.016
    root.run()
    assert a.value('x') == 9.0 and a.value('r') == 0.0 and frames
    assert not a.running and not root.jobs


def test_zero_ms_spin_and_settle_do_not_hang():
    a, root, *_ = make()
    a.spin('r', 0)
    assert not a.running and not root.jobs
    a.spin('r')
    a.settle('r', 0)
    assert a.value('r') == 0.0 and not a.running and not root.jobs


def test_jump_leaves_no_idle_frame():
    a, root, *_ = make()
    a.set('x', 0.0)
    a.to('x', 1.0, 100)
    a.set('x', 1.0)
    assert not root.jobs
