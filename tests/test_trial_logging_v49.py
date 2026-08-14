"""v49: trials.csv holds ONE row per trial, not two.

The v47 fix (close the open row on every ``trial_start``) was correct and is kept -- see
``test_trial_logging.py``. But because the firmware emits ``trial_start`` carrying the PREVIOUS
trial's id, closing on it necessarily writes a placeholder row per trial that never receives a cue or
an outcome. Measured on real sessions: PS92 2026-08-12 wrote 160 rows for 81 trials in one segment and
563 rows for 283 trials across the day, so anyone reading row counts read double the real trial count
(and did -- the widefield analysis reported "563 trials" for a 283-trial session).

v49 drops those placeholders at write time. This suite pins BOTH that they are gone AND that every
v47 guarantee still holds, since the placeholder is only safe to drop if the scored row is complete.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

GUI = (Path(__file__).resolve().parents[1] / "gui"
       / "BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v49.py")


@pytest.fixture(scope="module")
def gui():
    spec = importlib.util.spec_from_file_location("gui_v49", GUI)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gui_v49"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Rows:
    def __init__(self):
        self.rows = []

    def writerow(self, row):
        self.rows.append(dict(row))


@pytest.fixture
def logger(gui):
    lg = gui.SessionLogger()
    lg.trials_writer = _Rows()
    lg.trials_fh = None
    lg._current_trial = None
    return lg


def _event(logger, name, trial_id, pos_idx, t_ms, state="st_wait_for_lick"):
    row = {"event_name": name, "trial_id": str(trial_id), "pos_idx": str(pos_idx),
           "pos_name": f"p{pos_idx}", "pos_dist_mm": "2.0", "device_t_ms": str(t_ms),
           "gui_timestamp_iso": "", "state": state}
    logger._update_trial_from_event_row(row, {"trial": str(trial_id), "pos": str(pos_idx)})


def _run_session(logger, positions, outcome="hit"):
    """Replay the real device ordering: trial_start for trial N carries id N-1, its body carries N."""
    for n, pos in enumerate(positions):
        _event(logger, "trial_start", n, pos, 1000 * n)
        _event(logger, "cue", n + 1, pos, 1000 * n + 100)
        _event(logger, outcome, n + 1, pos, 1000 * n + 200)
    logger._finalize_trial(force=True)
    return logger.trials_writer.rows


def test_exactly_one_row_per_trial_no_placeholders(logger):
    """THE v49 change. Before this, a 3-trial session wrote 6 rows."""
    rows = _run_session(logger, [1, 4, 2])
    assert len(rows) == 3, f"expected 1 row per trial, got {len(rows)}"
    assert all(str(r["hit"]) == "1" for r in rows)


def test_a_miss_is_still_written(logger):
    """Placeholders are dropped for having NO outcome -- a miss IS an outcome and must survive."""
    rows = _run_session(logger, [1, 4], outcome="miss")
    assert len(rows) == 2
    assert all(str(r["miss"]) == "1" for r in rows)


def test_positions_are_still_the_trials_own(logger):
    """The v47 guarantee. Dropping placeholders must not disturb which position a row carries."""
    positions = [1, 4, 4, 2, 5, 0]
    rows = _run_session(logger, positions)
    assert [r["pos_idx"] for r in rows] == [str(p) for p in positions]
    assert [r["pos_name"] for r in rows] == [f"p{p}" for p in positions]


def test_a_trial_that_never_got_a_cue_is_not_written(logger):
    """An aborted/partial trial has no outcome. events.csv keeps its trial_start in full, so the
    scored-trial table is the right place to omit it -- every consumer already filters it out."""
    _event(logger, "trial_start", 0, 3, 0)
    logger._finalize_trial(force=True)
    assert logger.trials_writer.rows == []


def test_row_has_outcome_treats_falsey_strings_as_absent(logger, gui):
    """Fields arrive as STRINGS from the event stream; "0" must not read as an outcome."""
    has = gui.SessionLogger._row_has_outcome
    assert not has({"hit": "0", "miss": "0", "reward_delivered": "", "lick_in_response_window": "0"})
    assert not has({})
    for k in ("hit", "miss", "reward_delivered", "lick_in_response_window"):
        assert has({k: "1"}), k
