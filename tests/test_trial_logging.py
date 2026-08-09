"""Trial-row logging: a trial_start must close the previous trial's row.

Regression test for the ``pos_idx`` mislabelling found in the widefield analysis (2026-08-09):
the position written to ``trials.csv`` was the NEXT trial's position on every trial where the
position changed (~15% of trials, all sessions, GUI v40..v46). Fixed in v47; v46 is kept as it
shipped, so this suite runs against v47.

Cause: the firmware emits ``trial_start`` BEFORE ``totalTrials++``, so it reports the PREVIOUS
trial's id while that trial's own cue/hit/reward events (emitted after the increment) report
id+1 — the trial_start of trial N+1 therefore collides with the id already on trial N's open
row. ``_update_trial_from_event_row`` only finalized the open row when the ids DIFFERED, so the
row stayed open and the position lines overwrote it with the new trial's position.

Run:  python -m pytest tests/ -q      (from the repo root)
"""
import importlib.util
import sys
from pathlib import Path

import pytest

GUI = (Path(__file__).resolve().parents[1] / "gui"
       / "BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v47.py")


def _load_gui():
    spec = importlib.util.spec_from_file_location("gui_v47", GUI)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gui_v47"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gui():
    return _load_gui()


class _Rows:
    """Stand-in for csv.DictWriter that just collects the finalized trial rows.

    Deliberately NOT a list subclass: `_finalize_trial` early-returns on a falsy writer, and an
    empty list is falsy — a list-based stub silently records nothing.
    """

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


def _event(logger, name, trial_id, pos_idx, t_ms):
    """Feed one device event, mimicking what `log_event` builds and passes on.

    `trial` is present in kv for every firmware `emitEvent`, which is what marks the trial id
    as explicit for the trial logger.
    """
    row = {"event_name": name, "trial_id": str(trial_id), "pos_idx": str(pos_idx),
           "pos_name": f"p{pos_idx}", "pos_dist_mm": "2.0", "device_t_ms": str(t_ms),
           "gui_timestamp_iso": "", "state": "st_wait_for_lick"}
    logger._update_trial_from_event_row(row, {"trial": str(trial_id), "pos": str(pos_idx)})


def _run_session(logger, positions):
    """Replay the real device ordering for a list of per-trial positions.

    Mirrors events.csv exactly: trial_start for trial N carries id N-1, its body carries id N.
    """
    for n, pos in enumerate(positions):
        _event(logger, "trial_start", n, pos, 1000 * n)          # id lags by one
        _event(logger, "cue", n + 1, pos, 1000 * n + 100)
        _event(logger, "hit", n + 1, pos, 1000 * n + 200)
    logger._finalize_trial(force=True)
    return [r for r in logger.trials_writer.rows if str(r.get("hit", "")) == "1"]  # scored only


def test_position_is_the_trials_own_when_it_changes(logger):
    """The bug: each scored row took the NEXT trial's position whenever the position changed."""
    positions = [1, 4, 4, 2, 5, 0]
    scored = _run_session(logger, positions)
    assert [r["pos_idx"] for r in scored] == [str(p) for p in positions]
    assert [r["pos_name"] for r in scored] == [f"p{p}" for p in positions]


def test_one_scored_row_per_trial(logger):
    scored = _run_session(logger, [1, 4, 2])
    assert len(scored) == 3


def test_repeated_position_is_unaffected(logger):
    """The bug was invisible when consecutive trials shared a position — keep it that way."""
    scored = _run_session(logger, [3, 3, 3])
    assert [r["pos_idx"] for r in scored] == ["3", "3", "3"]


def test_trial_start_closes_the_open_row_even_on_a_colliding_id(logger):
    """The specific collision: trial_start reports the id already on the open row."""
    _event(logger, "trial_start", 0, 1, 0)
    _event(logger, "cue", 1, 1, 100)
    _event(logger, "hit", 1, 1, 200)
    open_before = dict(logger._current_trial)
    _event(logger, "trial_start", 1, 5, 300)        # SAME id (1), new position (5)
    assert open_before["pos_idx"] == "1"
    # the trial-1 row was written out, and kept its own position
    written = [r for r in logger.trials_writer.rows if str(r.get("hit", "")) == "1"]
    assert len(written) == 1 and written[0]["pos_idx"] == "1"
    # and the newly opened row belongs to the next trial
    assert logger._current_trial["pos_idx"] == "5"
