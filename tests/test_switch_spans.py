"""Alt+Tab and the taskbar previews freeze the screen track: events become spans in recording time."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402


class Session:
    def __init__(self):
        self.spans, self.alttab_open = [], False

    def add_span(self, a, b):
        self.spans.append((round(a, 2), round(b, 2)))


def _core(monkeypatch, screen=(0, 0, 1920, 1200)):
    x, y, w, h = screen
    monkeypatch.setattr(core.monitors, "match_obs_name",
                        lambda name: {"x": x, "y": y, "width": w, "height": h} if name else None)
    c = types.SimpleNamespace(session=Session(), offset=100.0, alttab_start=None, popup_start=None,
                              choice={"screen": "Monitor: 1920x1200 @ 0,0"})
    for name in ("_handle", "_add_switch_span", "_on_recorded_screen"):
        setattr(c, name, getattr(core.Core, name).__get__(c))
    return c


def test_a_preview_on_the_recorded_screen_is_frozen_like_alt_tab(monkeypatch):
    c = _core(monkeypatch)
    c._handle("popup", ("down", 110.0, (900, 1176)))
    assert c.session.alttab_open                                  # no segment is cut while it is open
    c._handle("popup", ("up", 111.5, None))
    d = core.VIDEO_DELAY
    assert c.session.spans == [(round(10 + d - 0.25, 2), round(11.5 + d + 0.5, 2))] and not c.session.alttab_open


def test_a_preview_on_the_other_screen_is_left_alone(monkeypatch):
    c = _core(monkeypatch)
    c._handle("popup", ("down", 110.0, (2900, 1176)))
    c._handle("popup", ("up", 111.0, None))
    assert c.session.spans == [] and not c.session.alttab_open


def test_alt_tab_and_a_preview_overlapping_keep_the_cut_waiting(monkeypatch):
    c = _core(monkeypatch)
    c._handle("alttab", ("down", 110.0))
    c._handle("popup", ("down", 110.5, (900, 1176)))
    c._handle("alttab", ("up", 111.0))
    assert c.session.alttab_open                                  # the preview is still open
    c._handle("popup", ("up", 112.0, None))
    assert len(c.session.spans) == 2 and not c.session.alttab_open
