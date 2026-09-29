from array import array

import pytest

import config as C
import control as K
import ui


def snap(**kw):
    sn = array("f", [0.0] * K.SN_LEN)
    sn[K.SN_TFET] = 30.0
    sn[K.SN_RUN] = 1.0
    for k, v in kw.items():
        sn[getattr(K, k)] = v
    return sn


def test_all_well_shows_no_problems():
    assert ui.problems(snap()) == []


@pytest.mark.parametrize("kw, line", [
    ({"SN_RUN": 0.0}, "NO LINK"),
    ({"SN_RUN": 0.0, "SN_FAULT": 5}, "VESC FAULT 5"),
    ({"SN_TFET": C.TEMP_HOT + 1}, "HOT 81 C"),
    ({"SN_TFET": C.TEMP_COLD - 2}, "COLD -12 C"),
    ({"SN_BAD": 3}, "BAD FRAMES 3"),
    ({"SN_LATE": 2}, "LATE TICKS 2"),
])
def test_each_problem_has_its_line(kw, line):
    assert ui.problems(snap(**kw)) == [line]


def test_temperatures_inside_the_limits_are_quiet():
    assert ui.problems(snap(SN_TFET=C.TEMP_HOT)) == []
    assert ui.problems(snap(SN_TFET=C.TEMP_COLD)) == []


def test_most_serious_first():
    sn = snap(SN_RUN=0.0, SN_TFET=90.0, SN_BAD=7, SN_LATE=1)
    assert ui.problems(sn, errors=2) == [
        "NO LINK", "HOT 90 C", "BAD FRAMES 7", "LATE TICKS 1", "SCREEN ERR 2"]


def test_every_line_fits_the_screen():
    sn = snap(SN_RUN=0.0, SN_FAULT=255, SN_TFET=-40.0,
              SN_BAD=99999, SN_LATE=99999)
    for line in ui.problems(sn, errors=99999):
        assert len(line) * 8 <= 128, line


# --- the screen, drawn into a stand-in framebuffer -------------------------------
class FakeFb:
    def __init__(self):
        self.lines = []

    def fill(self, c):
        self.lines = []

    def text(self, s, x, y):
        self.lines.append((y, s))


def test_riding_screen():
    fb = FakeFb()
    ui.draw(fb, snap(SN_WHEEL=150.0, SN_VIN=31.5, SN_IMOTOR=-12.5), 0)
    assert [s for _, s in fb.lines] == [" 18.9 km/h", " 31.5 V", "-12.5 A"]


def test_problems_appear_below_at_most_three():
    fb = FakeFb()
    ui.draw(fb, snap(SN_RUN=0.0, SN_BAD=4, SN_LATE=1), 1)
    lines = fb.lines
    assert [s for _, s in lines[3:]] == ["NO LINK", "BAD FRAMES 4", "LATE TICKS 1"]
    assert all(y + 8 <= 64 for y, _ in lines)                      # on the screen
    assert min(y for y, _ in lines[3:]) >= max(y for y, _ in lines[:3]) + 8
