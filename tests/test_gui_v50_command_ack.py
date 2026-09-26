"""Regression test for acknowledged GUI v50 SET batches."""

import importlib.util
import sys
from collections import deque
from pathlib import Path


GUI = Path(__file__).resolve().parents[1] / "gui" / "BehaviorGUI_MobileSpouts_Arduino_vs_Teensy_v50.py"


def _load_gui():
    spec = importlib.util.spec_from_file_location("gui_v50_command_ack", GUI)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _ConnectedClient:
    @staticmethod
    def is_connected():
        return True


class _Var:
    def set(self, value):
        self.value = value


class _Scheduler:
    def __init__(self):
        self.next_id = 0
        self.pending = []
        self.cancelled = set()

    def after(self, _delay_ms, callback):
        self.next_id += 1
        self.pending.append((self.next_id, callback))
        return self.next_id

    def cancel(self, callback_id):
        self.cancelled.add(callback_id)

    def run_next(self):
        while self.pending:
            callback_id, callback = self.pending.pop(0)
            if callback_id not in self.cancelled:
                callback()
                return
        raise AssertionError("No scheduled callback available")


def _app(gui):
    app = object.__new__(gui.App)
    scheduler = _Scheduler()
    sent = []
    logs = []
    app.client = _ConnectedClient()
    app._command_batch_active = False
    app._pending_command_batches = deque()
    app._command_batch_waiting = None
    app.zaber_status_var = _Var()
    app._ensure_connected_for_command = lambda: True
    app.send = lambda command: sent.append(command) or True
    app.after = scheduler.after
    app.after_cancel = scheduler.cancel
    app._log_local = logs.append
    return app, scheduler, sent, logs


def test_motion_busy_retries_same_set_and_waits_for_matching_ack():
    gui = _load_gui()
    app, scheduler, sent, logs = _app(gui)
    completed = []
    commands = ["SET task.pre_cue_min_ms=2000", "SET task.pre_cue_max_ms=2000"]

    app._start_command_batch(commands, on_complete=lambda: completed.append(True))
    assert sent == [commands[0]]

    app._handle_command_batch_response(
        "ERR cmd=busy code=motion_wait detail=SET",
        {"cmd": "busy", "code": "motion_wait", "detail": "SET"},
    )
    scheduler.run_next()
    assert sent == [commands[0], commands[0]]

    # An acknowledgment for another key must not advance the batch.
    app._handle_command_batch_response(
        "OK cmd=set key=task.iti_min_ms value=1000",
        {"cmd": "set", "key": "task.iti_min_ms", "value": "1000"},
    )
    assert sent == [commands[0], commands[0]]

    app._handle_command_batch_response(
        "OK cmd=set key=task.pre_cue_min_ms value=2000",
        {"cmd": "set", "key": "task.pre_cue_min_ms", "value": "2000"},
    )
    scheduler.run_next()
    assert sent[-1] == commands[1]
    assert not completed

    app._handle_command_batch_response(
        "OK cmd=set key=task.pre_cue_max_ms value=2000",
        {"cmd": "set", "key": "task.pre_cue_max_ms", "value": "2000"},
    )
    scheduler.run_next()
    assert completed == [True]
    assert not app._command_batch_active
    assert any("Device moving" in line for line in logs)


def test_sync_state_is_device_timestamped_or_left_unknown():
    gui = _load_gui()
    source = GUI.read_text()
    firmware = (GUI.parents[1] / "firmware" / "teensy_smc02"
                / "Behavior_MobileSpouts_2pRAM_Teensy_v42"
                / "Behavior_MobileSpouts_2pRAM_Teensy_v42.ino").read_text()

    assert 'if name == "sync":' in source
    assert 'data["state"] = ""' in source
    sync_body = firmware.split("void updateSync() {", 1)[1].split("// Protocol output", 1)[0]
    assert 'Serial.print(" state=");' in sync_body
    assert "Serial.print(stateName(runState));" in sync_body

    # The event writer must respect an explicit unknown value instead of
    # falling back to a stale polled status.
    logger = gui.SessionLogger()
    rows = []

    class _Writer:
        def writerow(self, row):
            rows.append(dict(row))

    class _File:
        @staticmethod
        def flush():
            pass

    logger.active = True
    logger.event_writer = _Writer()
    logger.event_fh = _File()
    logger.log_event(
        {"name": "sync", "state": "", "t_ms": "1190"},
        {},
        "EVT name=sync t_ms=1190",
        {"latest_status": {"state": "pre_cue"}},
    )
    assert rows[0]["state"] == ""


def test_setting_failure_is_nonmodal_during_active_session():
    gui = _load_gui()
    app, _scheduler, _sent, logs = _app(gui)
    dialogs = []
    gui.messagebox.showerror = lambda *args: dialogs.append(args)

    app.latest_status = {"run": "1", "state": "wait_for_lick"}
    app._report_command_batch_error("No device acknowledgment")
    assert not dialogs
    assert any("GUI ERROR" in line for line in logs)

    app.latest_status = {"run": "0", "state": "idle"}
    app._report_command_batch_error("No device acknowledgment")
    assert len(dialogs) == 1


def test_block_start_metadata_is_written_to_events_csv():
    gui = _load_gui()
    logger = gui.SessionLogger()
    rows = []

    class _Writer:
        def writerow(self, row):
            rows.append(dict(row))

    class _File:
        @staticmethod
        def flush():
            pass

    logger.active = True
    logger.event_writer = _Writer()
    logger.event_fh = _File()
    logger.log_event(
        {
            "name": "block_start", "t_ms": "5000", "state": "iti",
            "block_number": "7", "block_pos": "2", "block_size": "3",
            "block_trial": "0", "pos": "2",
        },
        {},
        "EVT name=block_start t_ms=5000 block_number=7 block_pos=2 block_size=3 block_trial=0",
        {"latest_status": {"block_number": "6", "block_pos": "1"}},
    )
    row = rows[0]
    assert row["event_name"] == "block_start"
    assert (row["block_number"], row["block_pos"], row["block_size"], row["block_trial"]) == ("7", "2", "3", "0")
