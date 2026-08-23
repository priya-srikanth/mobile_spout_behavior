import csv
import json
import math
import queue
import threading
import time
from datetime import datetime
from dataclasses import dataclass
from collections import deque
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

try:
    import serial
    from serial.tools import list_ports
except Exception as e:
    serial = None
    list_ports = None

APP_TITLE = "Spout Task GUI (Teensy / Mega-Zaber)"
DEFAULT_BAUD = 115200
CONFIG_PATH = Path.home() / ".spout_task_gui_backend.json"
MOUSE_PROFILE_DIR = Path(__file__).resolve().parent / "mouse_profiles"
SESSION_ROOT_DEFAULT = Path.home() / "spout_task_sessions"


@dataclass
class AxisStatus:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class TeensyClient:
    def __init__(self):
        self.ser = None
        self.rx_queue = queue.Queue()
        self._reader_thread = None
        self._stop = threading.Event()
        self.connected_port = None

    def connect(self, port: str, baud: int = DEFAULT_BAUD, timeout: float = 0.1, backend: str | None = None):
        if serial is None:
            raise RuntimeError("pyserial is not installed. Install with: pip install pyserial")
        self.disconnect()
        self.ser = serial.Serial(port=port, baudrate=baud, timeout=timeout, write_timeout=0.5)
        self.connected_port = port
        self._stop.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def disconnect(self):
        self._stop.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=0.5)
        self._reader_thread = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.connected_port = None

    def is_connected(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def send(self, line: str):
        if not self.is_connected():
            raise RuntimeError("Not connected to device")
        if not line.endswith("\n"):
            line += "\n"
        self.ser.write(line.encode("utf-8"))
        try:
            self.ser.flush()
        except Exception:
            pass

    def _reader_loop(self):
        while not self._stop.is_set() and self.ser is not None:
            try:
                raw = self.ser.readline()
                if raw:
                    self.rx_queue.put(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            except Exception as e:
                self.rx_queue.put(f"[ERROR] serial read failed: {e}")
                break

    @staticmethod
    def available_ports():
        if list_ports is None:
            return []
        return [p.device for p in list_ports.comports()]


class NumberVar(tk.StringVar):
    def get_float(self, default=0.0):
        try:
            return float(self.get())
        except Exception:
            return default

    def get_int(self, default=0):
        try:
            return int(float(self.get()))
        except Exception:
            return default





class SessionLogger:
    EVENT_FIELDS = [
        "gui_timestamp_iso", "gui_timestamp_unix", "device_t_ms", "event_name", "event_source", "reward_type",
        "state", "run", "trial_id", "current_pos", "pos_idx", "pos_name",
        "free_reward_trial", "free_reward_delivered", "sync_status", "lick_state",
        "x_mm", "y_mm", "z_mm",
        "pos_x_mm", "pos_y_mm", "pos_z_mm", "pos_dist_mm", "pos_az_deg", "pos_down_deg",
        "raw_line",
    ]
    TIMESERIES_FIELDS = [
        "gui_timestamp_iso", "gui_timestamp_unix", "sample_interval_ms",
        "run", "state", "current_pos", "pos_name", "x_mm", "y_mm", "z_mm",
        "sync_state", "lick_state",
    ]
    POSITION_STATS_FIELDS = [
        "pos_idx", "pos_name", "enabled", "dist_mm", "trials", "hits", "misses", "free_rewards",
        "adaptive_hit_counter", "hit_rate",
    ]
    TRIAL_FIELDS = [
        "trial_id", "device_trial_start_ms", "device_trial_end_ms",
        "trial_start_gui_iso", "trial_end_gui_iso",
        "pos_idx", "pos_name",
        "pos_dist_mm_before_trial", "pos_dist_mm_after_trial",
        "adaptive_advance_this_trial",
        "adaptive_decrease_this_trial",
        "free_reward_trial", "free_reward_delivered",
        "lick_in_response_window",
        "hit", "miss",
        "reward_delivered", "reward_type",
    ]
    CONFIG_CHANGE_FIELDS = [
        "gui_timestamp_iso", "gui_timestamp_unix",
        "change_type", "direction", "source",
        "key", "value", "command", "acknowledged",
        "run", "state", "trial_id",
        "raw_line",
    ]

    def __init__(self):
        self.active = False
        self.session_dir = None
        self.raw_fh = None
        self.event_fh = None
        self.timeseries_fh = None
        self.trials_fh = None
        self.config_change_fh = None
        self.event_writer = None
        self.timeseries_writer = None
        self.trials_writer = None
        self.config_change_writer = None
        self.raw_enabled = False
        self.timeseries_enabled = True
        self.timeseries_interval_ms = 20
        self.manifest = {}
        self.start_unix = None
        self.event_counts = {}
        self._latest_status = {}
        self._current_trial = None
        self._last_seen_trial_id = None

    def _write_json(self, path: Path, payload: dict):
        path.write_text(json.dumps(payload, indent=2))

    def _file_inventory(self):
        if not self.session_dir or not Path(self.session_dir).exists():
            return []
        return sorted(p.name for p in Path(self.session_dir).iterdir() if p.is_file())

    def _expected_files(self):
        files = [
            "events.csv",
            "trials.csv",
            "config_changes.csv",
            "gui_config.json",
            "gui_config_end.json",
            "device_snapshot_start.json",
            "device_snapshot_end.json",
            "session_manifest.json",
            "summary_end.json",
            "position_stats_end.csv",
        ]
        if self.timeseries_enabled:
            files.append("timeseries.csv")
        if self.raw_enabled:
            files.append("raw_protocol.log")
        return files

    def _write_manifest(self):
        if self.session_dir:
            self._write_json(Path(self.session_dir) / "session_manifest.json", self.manifest)

    def start(self, base_dir: str, prefix: str, gui_config: dict, manifest_payload: dict,
              raw_log_enabled: bool, timeseries_enabled: bool, timeseries_interval_ms: int):
        base = Path(base_dir).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_prefix = prefix.strip() or "session"
        self.session_dir = base / f"{safe_prefix}_{stamp}"
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self.start_unix = time.time()
        self.event_counts = {}
        self._latest_status = {}
        self._current_trial = None
        self._last_seen_trial_id = None

        self.raw_enabled = bool(raw_log_enabled)
        self.timeseries_enabled = bool(timeseries_enabled)
        self.timeseries_interval_ms = max(1, int(timeseries_interval_ms))

        if self.raw_enabled:
            self.raw_fh = open(self.session_dir / "raw_protocol.log", "a", encoding="utf-8", buffering=1)
        self.event_fh = open(self.session_dir / "events.csv", "a", encoding="utf-8", newline="")
        self.trials_fh = open(self.session_dir / "trials.csv", "a", encoding="utf-8", newline="")
        self.config_change_fh = open(self.session_dir / "config_changes.csv", "a", encoding="utf-8", newline="")
        if self.timeseries_enabled:
            self.timeseries_fh = open(self.session_dir / "timeseries.csv", "a", encoding="utf-8", newline="")

        self.event_writer = csv.DictWriter(self.event_fh, fieldnames=self.EVENT_FIELDS)
        self.event_writer.writeheader()
        self.trials_writer = csv.DictWriter(self.trials_fh, fieldnames=self.TRIAL_FIELDS)
        self.trials_writer.writeheader()
        self.config_change_writer = csv.DictWriter(self.config_change_fh, fieldnames=self.CONFIG_CHANGE_FIELDS)
        self.config_change_writer.writeheader()

        if self.timeseries_enabled:
            self.timeseries_writer = csv.DictWriter(self.timeseries_fh, fieldnames=self.TIMESERIES_FIELDS)
            self.timeseries_writer.writeheader()
        else:
            self.timeseries_writer = None

        (self.session_dir / "gui_config.json").write_text(json.dumps(gui_config, indent=2))
        device_snapshot = dict((manifest_payload or {}).get("device_snapshot", {}) or {})
        self._write_json(self.session_dir / "device_snapshot_start.json", device_snapshot)

        self.manifest = dict(manifest_payload or {})
        self.manifest["schema_version"] = 2
        self.manifest["session_id"] = self.session_dir.name
        self.manifest["session_dir"] = str(self.session_dir)
        self.manifest["start_time_iso"] = datetime.now().isoformat()
        self.manifest["start_time_unix"] = self.start_unix
        self.manifest["logging"] = {
            **dict(self.manifest.get("logging", {})),
            "raw_protocol_log_enabled": self.raw_enabled,
            "timeseries_enabled": self.timeseries_enabled,
            "timeseries_interval_ms": self.timeseries_interval_ms,
        }
        self.manifest["files_expected"] = self._expected_files()
        self.manifest["files_present"] = self._file_inventory()
        self._write_manifest()

        self.active = True
        self.log_config_snapshot("snapshot_start", gui_config, source="gui_config")
        if device_snapshot:
            self.log_config_snapshot("snapshot_start", device_snapshot, source="device_snapshot")
        return self.session_dir

    def stop(self, summary_end: dict | None = None, position_stats_rows: list | None = None,
             final_manifest_updates: dict | None = None, final_gui_config: dict | None = None,
             final_device_snapshot: dict | None = None):
        if not self.session_dir:
            return

        if summary_end is not None:
            self._write_json(Path(self.session_dir) / "summary_end.json", summary_end)

        if position_stats_rows is not None:
            with open(Path(self.session_dir) / "position_stats_end.csv", "w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=self.POSITION_STATS_FIELDS)
                writer.writeheader()
                for row in position_stats_rows:
                    writer.writerow({k: row.get(k, "") for k in self.POSITION_STATS_FIELDS})

        self._finalize_trial(force=True)

        if final_manifest_updates:
            self.manifest.update(final_manifest_updates)
        if final_gui_config is not None:
            self._write_json(Path(self.session_dir) / "gui_config_end.json", final_gui_config)
            self.log_config_snapshot("snapshot_end", final_gui_config, source="gui_config")
        if final_device_snapshot is not None:
            self._write_json(Path(self.session_dir) / "device_snapshot_end.json", final_device_snapshot)
            self.log_config_snapshot("snapshot_end", final_device_snapshot, source="device_snapshot")
        self.manifest["end_time_iso"] = datetime.now().isoformat()
        self.manifest["end_time_unix"] = time.time()
        if self.start_unix is not None:
            self.manifest["duration_s"] = round(time.time() - self.start_unix, 3)
        self.manifest["event_counts"] = dict(self.event_counts)
        if self._latest_status:
            self.manifest["latest_status_snapshot"] = dict(self._latest_status)
        self.manifest["files_present"] = self._file_inventory()
        self._write_manifest()

        for fh in (self.raw_fh, self.event_fh, self.timeseries_fh, self.trials_fh, self.config_change_fh):
            try:
                if fh:
                    fh.close()
            except Exception:
                pass
        self.raw_fh = self.event_fh = self.timeseries_fh = self.trials_fh = self.config_change_fh = None
        self.event_writer = self.timeseries_writer = self.trials_writer = self.config_change_writer = None
        self._current_trial = None
        self._last_seen_trial_id = None
        self.active = False

    def log_raw(self, line: str):
        if not self.active or not self.raw_fh:
            return
        now = datetime.now()
        self.raw_fh.write(f"{now.isoformat()}\t{line}\n")

    def update_latest_status(self, status: dict):
        if not isinstance(status, dict):
            return
        self._latest_status = dict(status)
        self.manifest["latest_status_snapshot"] = dict(status)

    def log_timeseries(self, row: dict):
        if not self.active or not self.timeseries_writer:
            return
        self.timeseries_writer.writerow(row)
        self.timeseries_fh.flush()

    def _flatten_snapshot(self, payload, prefix=""):
        if isinstance(payload, dict):
            for key, value in payload.items():
                new_prefix = f"{prefix}.{key}" if prefix else str(key)
                yield from self._flatten_snapshot(value, new_prefix)
            return
        if isinstance(payload, (list, tuple)):
            for idx, value in enumerate(payload):
                new_prefix = f"{prefix}[{idx}]"
                yield from self._flatten_snapshot(value, new_prefix)
            return
        yield prefix, payload

    def log_config_change(self, change_type: str, direction: str, source: str,
                          key="", value="", command="", raw_line="", latest_status: dict | None = None,
                          acknowledged=""):
        if not self.active or not self.config_change_writer:
            return
        latest_status = latest_status if isinstance(latest_status, dict) else {}
        now = datetime.now()
        row = {
            "gui_timestamp_iso": now.isoformat(),
            "gui_timestamp_unix": f"{time.time():.6f}",
            "change_type": change_type,
            "direction": direction,
            "source": source,
            "key": str(key),
            "value": "" if value in (None, "") else json.dumps(value) if isinstance(value, (dict, list, tuple, bool)) else str(value),
            "command": command,
            "acknowledged": acknowledged,
            "run": latest_status.get("run", ""),
            "state": latest_status.get("state", ""),
            "trial_id": latest_status.get("total_trials", ""),
            "raw_line": raw_line,
        }
        self.config_change_writer.writerow(row)
        if self.config_change_fh:
            self.config_change_fh.flush()

    def log_config_snapshot(self, change_type: str, payload: dict, source: str):
        if not isinstance(payload, dict):
            return
        for key, value in self._flatten_snapshot(payload):
            self.log_config_change(change_type=change_type, direction="snapshot", source=source, key=key, value=value)

    def log_outbound_command(self, cmd: str, latest_status: dict | None = None):
        text = str(cmd or "").strip()
        if not text:
            return
        upper = text.upper()
        if upper.startswith("SET "):
            body = text[4:]
            key, _, value = body.partition("=")
            self.log_config_change(
                change_type="set_command",
                direction="outbound",
                source="gui",
                key=key.strip(),
                value=value.strip(),
                command=text,
                raw_line=text,
                latest_status=latest_status,
            )
        elif upper in ("START", "STOP", "RESETSESSION"):
            self.log_config_change(
                change_type="session_command",
                direction="outbound",
                source="gui",
                command=text,
                raw_line=text,
                latest_status=latest_status,
            )

    def _infer_event_source(self, name: str, kv: dict):
        if kv.get("event_source"):
            return kv.get("event_source", "")
        if name in ("cue_only", "manual_reward", "manual_reference_set"):
            return "manual"
        if name == "reward_cal_pulse":
            return "calibration"
        if name == "sync":
            return "sync"
        if name.startswith("button_"):
            return "button"
        return "task"

    def _infer_reward_type(self, name: str, kv: dict):
        if kv.get("reward_type"):
            return kv.get("reward_type", "")
        if name in ("free_reward_trial", "free_reward"):
            return "free"
        if name == "manual_reward":
            return "manual"
        if name == "reward_cal_pulse":
            return "calibration"
        if name == "hit":
            return "contingent"
        if name == "reward" and kv.get("free_reward_delivered", "") in ("1", "true", "True", "on"):
            return "free"
        return ""

    def _normalize_trial_id(self, value):
        s = str(value).strip()
        if not s or s in ("?", "None"):
            return ""
        return s

    def _effective_trial_row_id(self, event_name: str, trial_id: str):
        s = self._normalize_trial_id(trial_id)
        if not s:
            last = self._normalize_trial_id(self._last_seen_trial_id)
            if event_name == "trial_start" and last:
                try:
                    return str(int(float(last)) + 1)
                except Exception:
                    return last
            return ""
        try:
            value = int(float(s))
        except Exception:
            return s
        if event_name == "trial_start":
            value += 1
        return str(value)

    def _state_name_is_wait_for_lick(self, state_val):
        s = str(state_val or "").strip().lower()
        return ("wait_for_lick" in s) or (s == "st_wait_for_lick")

    def _trial_template(self, trial_id: str, row: dict):
        return {
            "trial_id": trial_id,
            "device_trial_start_ms": row.get("device_t_ms", ""),
            "device_trial_end_ms": row.get("device_t_ms", ""),
            "trial_start_gui_iso": row.get("gui_timestamp_iso", ""),
            "trial_end_gui_iso": row.get("gui_timestamp_iso", ""),
            "pos_idx": row.get("pos_idx", ""),
            "pos_name": row.get("pos_name", ""),
            "pos_dist_mm_before_trial": row.get("pos_dist_mm", ""),
            "pos_dist_mm_after_trial": row.get("pos_dist_mm", ""),
            "adaptive_advance_this_trial": 0,
            "adaptive_decrease_this_trial": 0,
            "free_reward_trial": row.get("free_reward_trial", ""),
            "free_reward_delivered": row.get("free_reward_delivered", ""),
            "lick_in_response_window": 0,
            "hit": 0,
            "miss": 0,
            "reward_delivered": 0,
            "reward_type": row.get("reward_type", ""),
        }

    def _finalize_trial(self, force=False):
        if not self.trials_writer or not self._current_trial:
            return
        row = dict(self._current_trial)
        if not force and not row.get("trial_id", ""):
            return
        self.trials_writer.writerow({k: row.get(k, "") for k in self.TRIAL_FIELDS})
        if self.trials_fh:
            self.trials_fh.flush()
        self._current_trial = None

    def _update_trial_from_event_row(self, row: dict, kv: dict):
        event_name = str(row.get("event_name", "") or "")
        trial_id = self._normalize_trial_id(row.get("trial_id", ""))
        explicit_flag = kv.get("_trial_id_explicit_for_logger", None)
        if explicit_flag is None:
            trial_id_explicit = "trial" in kv
        else:
            trial_id_explicit = str(explicit_flag).strip().lower() in ("1", "true", "on", "yes")
        if not trial_id_explicit:
            return
        trial_row_id = self._effective_trial_row_id(event_name, trial_id)
        if not trial_row_id:
            return
        if event_name == "trial_start":
            # A trial_start ALWAYS begins a new trial row.
            #
            # The firmware emits trial_start BEFORE `totalTrials++`, so it carries the PREVIOUS
            # trial's id, while that same trial's cue/hit/reward events (emitted after the
            # increment) carry id+1. In events.csv:
            #     trial_start    trial_id=0     <- trial 1 begins
            #     cue/reward/hit trial_id=1     <- trial 1's body
            #     trial_start    trial_id=1     <- trial 2 begins
            # so trials.csv needs the ACTUAL trial number for trial_start rows, i.e. raw+1.
            # Using the raw device trial id here duplicates every trial: once for trial_start and
            # once again for the first cue/reward/hit event. We keep events.csv unchanged for
            # traceability, but normalize trials.csv rows to the actual trial number.
            if self._current_trial is not None:
                self._finalize_trial(force=True)
            self._current_trial = self._trial_template(trial_row_id, row)
        elif self._current_trial is None:
            self._current_trial = self._trial_template(trial_row_id, row)
        elif self._normalize_trial_id(self._current_trial.get("trial_id", "")) != trial_row_id:
            self._finalize_trial(force=True)
            self._current_trial = self._trial_template(trial_row_id, row)

        t = self._current_trial
        self._last_seen_trial_id = trial_row_id
        t["device_trial_end_ms"] = row.get("device_t_ms", t.get("device_trial_end_ms", ""))
        t["trial_end_gui_iso"] = row.get("gui_timestamp_iso", t.get("trial_end_gui_iso", ""))
        if row.get("pos_idx", "") != "":
            t["pos_idx"] = row.get("pos_idx", t.get("pos_idx", ""))
        if row.get("pos_name", ""):
            t["pos_name"] = row.get("pos_name", t.get("pos_name", ""))
        if row.get("pos_dist_mm", "") != "":
            if t.get("pos_dist_mm_before_trial", "") in ("", None):
                t["pos_dist_mm_before_trial"] = row.get("pos_dist_mm", "")
            t["pos_dist_mm_after_trial"] = row.get("pos_dist_mm", t.get("pos_dist_mm_after_trial", ""))
        if str(row.get("free_reward_trial", "")) not in ("", "0", "False", "false"):
            t["free_reward_trial"] = row.get("free_reward_trial", "")
        if str(row.get("free_reward_delivered", "")) not in ("", "0", "False", "false"):
            t["free_reward_delivered"] = row.get("free_reward_delivered", "")
            t["reward_delivered"] = 1
        state_val = row.get("state", "")
        if event_name in ("lick", "lick_on") and self._state_name_is_wait_for_lick(state_val):
            t["lick_in_response_window"] = 1
        elif event_name == "hit":
            t["hit"] = 1
        elif event_name == "miss":
            t["miss"] = 1
        elif event_name in ("reward", "free_reward", "manual_reward"):
            t["reward_delivered"] = 1
            if row.get("reward_type", ""):
                t["reward_type"] = row.get("reward_type", t.get("reward_type", ""))
        elif event_name == "adapt_advance":
            t["adaptive_advance_this_trial"] = 1
            dist_after = kv.get("dist_mm", row.get("pos_dist_mm", ""))
            if dist_after not in ("", None):
                t["pos_dist_mm_after_trial"] = dist_after
        elif event_name == "adapt_decrease":
            t["adaptive_decrease_this_trial"] = 1
            dist_after = kv.get("dist_mm", row.get("pos_dist_mm", ""))
            if dist_after not in ("", None):
                t["pos_dist_mm_after_trial"] = dist_after

    def log_event(self, kv: dict, positions: dict, raw_line: str, context: dict | None = None):
        if not self.active or not self.event_writer:
            return
        context = context or {}
        now = datetime.now()
        latest_status = context.get("latest_status", {}) if isinstance(context.get("latest_status", {}), dict) else {}
        pos_idx = kv.get("pos", kv.get("idx", latest_status.get("current_pos", "")))
        pos = positions.get(int(pos_idx), {}) if str(pos_idx).lstrip("-").isdigit() and int(pos_idx) in positions else {}
        row = {
            "gui_timestamp_iso": now.isoformat(),
            "gui_timestamp_unix": f"{time.time():.6f}",
            "device_t_ms": kv.get("t_ms", ""),
            "event_name": kv.get("name", ""),
            "event_source": self._infer_event_source(kv.get("name", ""), kv),
            "reward_type": self._infer_reward_type(kv.get("name", ""), kv),
            "state": kv.get("state", latest_status.get("state", "")),
            "run": latest_status.get("run", ""),
            "trial_id": kv.get("trial", latest_status.get("total_trials", "")),
            "current_pos": latest_status.get("current_pos", ""),
            "pos_idx": pos_idx,
            "pos_name": pos.get("name", pos.get("label", context.get("pos_name", ""))),
            "free_reward_trial": kv.get("free_reward_trial", latest_status.get("free_reward_trial", "")),
            "free_reward_delivered": kv.get("free_reward_delivered", latest_status.get("free_reward_delivered", "")),
            "sync_status": "1" if kv.get("name", "") == "sync" else latest_status.get("sync_state", ""),
            "lick_state": context.get("lick_state", latest_status.get("lick", "")),
            "x_mm": context.get("x_mm", ""),
            "y_mm": context.get("y_mm", ""),
            "z_mm": context.get("z_mm", ""),
            "pos_x_mm": pos.get("x_mm", ""),
            "pos_y_mm": pos.get("y_mm", ""),
            "pos_z_mm": pos.get("z_mm", ""),
            "pos_dist_mm": pos.get("dist_mm", kv.get("pos_dist_mm", "")),
            "pos_az_deg": pos.get("az_deg", ""),
            "pos_down_deg": pos.get("down_deg", ""),
            "raw_line": raw_line,
        }
        self.event_writer.writerow(row)
        self.event_fh.flush()
        name = row["event_name"]
        self.event_counts[name] = self.event_counts.get(name, 0) + 1
        self._update_trial_from_event_row(row, kv)

    def update_positions_snapshot(self, positions: dict):
        if not self.active:
            return
        self.manifest["latest_positions_snapshot"] = positions
        self._write_manifest()

    def update_device_config_snapshot(self, device_cfg: dict):
        if not self.active:
            return
        self.manifest["device_snapshot"] = device_cfg
        self._write_manifest()


class BaseApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1450x980")
        self.minsize(1200, 820)

        self.client = TeensyClient()
        self.session_logger = SessionLogger()
        self.axis_status = AxisStatus()
        self.latest_status = {}
        self._last_status_poll = 0.0
        self.protocol_version = 1
        self.timeline_events = deque(maxlen=800)
        self.session_timeline_events = []
        self._session_timeline_start_unix = None
        self._session_timeline_end_unix = None
        self.lick_samples = deque(maxlen=800)
        self._last_lick_state = 0
        self.position_stats = {i: {} for i in range(6)}
        self.current_active_pos_idx = None
        self.current_at_dock = False
        self._pending_free_reward_marker = None
        self._command_batch_active = False
        self._pending_command_batches = deque()
        self.device_config_cache = {}
        self._adaptive_verify_after_id = None
        self._sync_active_until = 0.0
        self._lick_state_current = 0
        self._timeseries_sample_ms = 20
        self.summary_stats = {}
        self._connect_waiting = False
        self._connect_attempt = 0
        self._connect_after_id = None
        self._last_probe_port = ""
        self._last_probe_time = 0.0
        self.ports = {}
        self.position_labels = [f"Pos {i}" for i in range(6)]

        self._build_vars()
        self._build_ui()
        self._load_config_if_exists()
        # Sync indicators / name refresh traces
        for v in self.position_name_vars:
            v.trace_add("write", self._refresh_position_names)
        for v in (self.mouth_x_var, self.mouth_y_var, self.mouth_z_var):
            v.trace_add("write", self._mark_mouth_dirty)
        for v in (self.dock_x_var, self.dock_y_var, self.dock_z_var):
            v.trace_add("write", self._mark_dock_dirty)
        self.safe_z_var.trace_add("write", self._mark_safez_dirty)
        for v in (self.dist_close_var, self.dist_far_var, self.az_center_var, self.az_left_var, self.az_right_var, self.down_angle_var, self.head_roll_var):
            v.trace_add("write", self._mark_geometry_dirty)
        for v in self.pos_enabled_vars:
            v.trace_add("write", self._mark_enabled_dirty)
        self.adaptive_enabled_var.trace_add("write", lambda *_args: self._refresh_position_stats_tree())
        for v in (
            self.adaptive_enabled_var,
            self.adapt_hits_var,
            self.adapt_misses_var,
            self.adapt_step_var,
            self.adapt_step_down_var,
            self.adapt_min_var,
            self.adapt_max_var,
        ):
            v.trace_add("write", self._on_global_adaptive_value_changed)
        self.adapt_use_per_position_var.trace_add("write", self._on_adaptive_scope_changed)
        self.adapt_selected_pos_var.trace_add("write", self._load_selected_adaptive_position_into_editor)
        for v in (
            self.adapt_edit_enabled_var,
            self.adapt_edit_hits_var,
            self.adapt_edit_misses_var,
            self.adapt_edit_step_var,
            self.adapt_edit_step_down_var,
            self.adapt_edit_min_var,
            self.adapt_edit_max_var,
        ):
            v.trace_add("write", self._store_adaptive_editor_to_selected_position)
        self._sync_all_adaptive_positions_from_global()
        self._load_selected_adaptive_position_into_editor()
        self._refresh_position_names()

        self.refresh_ports()
        self._update_port_info()
        self.after(100, self._process_serial)
        self.after(500, self._poll_status_loop)
        self.after(120, self._redraw_visuals_loop)
        self.after(self._timeseries_sample_ms, self._timeseries_log_loop)
        self._recompute_geometry_preview()
        self._recompute_stepper_calculator()
        self._update_scheduler_summary()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- UI vars ----------------
    def _build_vars(self):
        self.port_var = tk.StringVar()
        self.port_info_var = tk.StringVar(value="No port selected")
        self.port_display_to_device = {}
        self.baud_var = NumberVar(value=str(DEFAULT_BAUD))
        self.autopoll_var = tk.BooleanVar(value=False)
        self.autopoll_interval_var = NumberVar(value="1.0")
        self.save_dir_var = tk.StringVar(value=str(SESSION_ROOT_DEFAULT))
        self.session_prefix_var = tk.StringVar(value="session")
        self.auto_log_on_start_var = tk.BooleanVar(value=True)
        self.raw_protocol_log_var = tk.BooleanVar(value=False)
        self.timeseries_enabled_var = tk.BooleanVar(value=True)
        self.timeseries_interval_var = NumberVar(value="20")
        self.logging_state_var = tk.StringVar(value="Logging: off")
        self._logging_auto_started = False

        self.curr_x_var = tk.StringVar(value="0.000")
        self.curr_y_var = tk.StringVar(value="0.000")
        self.curr_z_var = tk.StringVar(value="0.000")
        self.status_line_var = tk.StringVar(value="Disconnected")
        self.last_event_var = tk.StringVar(value="No events yet")
        self.current_pos_label_var = tk.StringVar(value="Pos: --")
        self.block_summary_var = tk.StringVar(value="Block: --")
        self.reward_summary_var = tk.StringVar(value="Rewards: --")
        self.scheduler_summary_var = tk.StringVar(value="Schedule: --")
        self.remaining_summary_var = tk.StringVar(value="Target remaining: --")
        self.visual_pos_var = tk.StringVar(value="Current spout pos: --")
        self.visual_block_var = tk.StringVar(value="Current block #: --")
        self.visual_trial_var = tk.StringVar(value="Current trial #: --")
        self.visual_elapsed_var = tk.StringVar(value="Time since task start: -- min")
        self._task_start_wall_unix = None
        self._last_status_run = None
        self._start_requested_wall_unix = None
        self._awaiting_run_confirmation = False
        self.monitor_pos_var = tk.StringVar(value="0")
        self.sequence_dwell_var = NumberVar(value="1200")
        self.sequence_cycles_var = NumberVar(value="1")
        self.sequence_with_cue_var = tk.BooleanVar(value=True)
        self.sequence_status_var = tk.StringVar(value="Sequence: idle")
        self.reward_hold_var = tk.BooleanVar(value=False)
        self.reward_hold_button_var = tk.StringVar(value="Hold rewards")
        self._sequence_running = False
        self._sequence_after_id = None
        self._sequence_cue_after_id = None
        self._sequence_queue = []
        self._sequence_step_delay_ms = 1200
        self._sequence_total_steps = 0
        self._sequence_completed_steps = 0
        self._sequence_waiting_idx = None
        self._sequence_move_inflight = False
        self._sequence_token = 0

        # Motion / coordinate vars
        self.safe_z_var = NumberVar(value="-5.0")
        self.mouth_x_var = NumberVar(value="0.0")
        self.mouth_y_var = NumberVar(value="0.0")
        self.mouth_z_var = NumberVar(value="0.0")
        self.current_mouse_profile_var = tk.StringVar(value="default")
        self.ref_x_var = NumberVar(value="0.0")
        self.ref_y_var = NumberVar(value="0.0")
        self.ref_z_var = NumberVar(value="0.0")
        self.dock_x_var = NumberVar(value="0.0")
        self.dock_y_var = NumberVar(value="-10.0")
        self.dock_z_var = NumberVar(value="-5.0")
        self.jog_step_var = NumberVar(value="1.0")

        # Task vars
        self.reward_ms_var = NumberVar(value="25")
        self.reward_ul_var = NumberVar(value="2.5")
        self.water_limit_ul_var = NumberVar(value="1000")
        self.reward_cal_pulses_var = NumberVar(value="100")
        self.reward_mode_var = tk.StringVar(value="contingent")
        self.auto_reward_delay_var = NumberVar(value="500")
        self.auto_hold_after_miss_enabled_var = tk.BooleanVar(value=False)
        self.auto_hold_after_miss_threshold_var = NumberVar(value="3")
        self.cue_hz_var = NumberVar(value="10000")
        self.cue_duration_var = NumberVar(value="100")
        self.cue_volume_var = NumberVar(value="50")
        self.enforce_var = tk.BooleanVar(value=True)
        self.settle_ms_var = NumberVar(value="150")
        self.posthold_ms_var = NumberVar(value="10000")
        self.precue_min_var = NumberVar(value="3000")
        self.precue_max_var = NumberVar(value="5000")
        self.response_window_var = NumberVar(value="5000")
        self.iti_min_var = NumberVar(value="1500")
        self.iti_jitter_var = NumberVar(value="1500")
        self.sync_pulse_min_var = NumberVar(value="20")
        self.sync_pulse_max_var = NumberVar(value="20")
        self.sync_interval_min_var = NumberVar(value="40")
        self.sync_interval_max_var = NumberVar(value="360")
        self.block_size_var = NumberVar(value="5")
        self.block_size_min_var = NumberVar(value="5")
        self.block_size_max_var = NumberVar(value="5")
        self.target_trials_enabled_var = tk.BooleanVar(value=True)
        self.target_trials_per_pos_var = NumberVar(value="50")
        self.max_duration_enabled_var = tk.BooleanVar(value=False)
        self.max_duration_min_var = NumberVar(value="60")
        self.scheduling_mode_var = tk.StringVar(value="balanced_block_cycles")
        self.stop_mode_var = tk.StringVar(value="end_of_current_block")
        self.scheduler_summary_var = tk.StringVar(value="")

        # Geometry vars
        self.dist_close_var = NumberVar(value="3.0")
        self.dist_far_var = NumberVar(value="6.0")
        self.az_center_var = NumberVar(value="0.0")
        self.az_left_var = NumberVar(value="-45.0")
        self.az_right_var = NumberVar(value="45.0")
        self.down_angle_var = NumberVar(value="30.0")
        self.head_roll_var = NumberVar(value="15.0")

        self.pos_enabled_vars = [tk.BooleanVar(value=True) for _ in range(6)]

        # Adaptive + free reward vars
        self.adaptive_enabled_var = tk.BooleanVar(value=False)
        self.adapt_use_per_position_var = tk.BooleanVar(value=False)
        self.adapt_selected_pos_var = tk.StringVar(value="0")
        self.adapt_hits_var = NumberVar(value="2")
        self.adapt_misses_var = NumberVar(value="2")
        self.adapt_step_var = NumberVar(value="0.5")
        self.adapt_step_down_var = NumberVar(value="0.5")
        self.adapt_min_var = NumberVar(value="3.0")
        self.adapt_max_var = NumberVar(value="8.0")
        self.adapt_pos_enabled_vars = [tk.BooleanVar(value=True) for _ in range(6)]
        self.adapt_pos_hits_vars = [NumberVar(value="2") for _ in range(6)]
        self.adapt_pos_misses_vars = [NumberVar(value="2") for _ in range(6)]
        self.adapt_pos_step_vars = [NumberVar(value="0.5") for _ in range(6)]
        self.adapt_pos_step_down_vars = [NumberVar(value="0.5") for _ in range(6)]
        self.adapt_pos_min_vars = [NumberVar(value="3.0") for _ in range(6)]
        self.adapt_pos_max_vars = [NumberVar(value="8.0") for _ in range(6)]
        self.adapt_edit_enabled_var = tk.BooleanVar(value=True)
        self.adapt_edit_hits_var = NumberVar(value="2")
        self.adapt_edit_misses_var = NumberVar(value="2")
        self.adapt_edit_step_var = NumberVar(value="0.5")
        self.adapt_edit_step_down_var = NumberVar(value="0.5")
        self.adapt_edit_min_var = NumberVar(value="3.0")
        self.adapt_edit_max_var = NumberVar(value="8.0")
        self._adapt_editor_syncing = False
        self.free_reward_enabled_var = tk.BooleanVar(value=True)
        self.free_after_misses_var = NumberVar(value="6")
        self.free_delay_var = NumberVar(value="500")

        # Lick vars
        self.lick_thresh_var = NumberVar(value="500")
        self.lick_hyst_var = NumberVar(value="150")
        self.lick_polarity_var = tk.StringVar(value="-1")
        self.lick_alpha_var = NumberVar(value="0.005")
        self.lick_refract_var = NumberVar(value="20")
        self.lick_debug_var = tk.BooleanVar(value=False)
        self.lick_thresh_volts_var = tk.StringVar(value="≈ 0.403 V")
        self.lick_hyst_volts_var = tk.StringVar(value="≈ 0.121 V")

        # Axis calibration vars
        self.axis_cal = {}
        for axis in ("X", "Y", "Z"):
            self.axis_cal[axis] = {
                "ms_pos": NumberVar(value="220"),
                "ms_neg": NumberVar(value="220"),
                "overhead": NumberVar(value="15"),
                "cw_pos": tk.BooleanVar(value=True),
            }

        # SMC02 / stage calculator
        self.stage_label_var = tk.StringVar(value="T6*1 (default)")
        self.step_angle_var = NumberVar(value="1.8")
        self.microstep_var = NumberVar(value="8")
        self.screw_lead_var = NumberVar(value="1.0")
        self.rpm_x_var = NumberVar(value="400")
        self.rpm_y_var = NumberVar(value="400")
        self.rpm_z_var = NumberVar(value="300")
        self.accel_text_var = tk.StringVar(value="020")
        self.mode_text_var = tk.StringVar(value="P02")

        # Calculator outputs
        self.pulses_rev_var = tk.StringVar(value="")
        self.f09_display_var = tk.StringVar(value="")
        self.steps_per_mm_var = tk.StringVar(value="")
        self.mm_per_s_x_var = tk.StringVar(value="")
        self.mm_per_s_y_var = tk.StringVar(value="")
        self.mm_per_s_z_var = tk.StringVar(value="")
        self.ms_per_mm_x_var = tk.StringVar(value="")
        self.ms_per_mm_y_var = tk.StringVar(value="")
        self.ms_per_mm_z_var = tk.StringVar(value="")

        # Position preview / naming variables
        self.position_name_vars = [
            tk.StringVar(value="close_center"),
            tk.StringVar(value="close_L"),
            tk.StringVar(value="close_R"),
            tk.StringVar(value="far_center"),
            tk.StringVar(value="far_L"),
            tk.StringVar(value="far_R"),
        ]
        self.position_labels = [v.get() for v in self.position_name_vars]
        self.position_preview_vars = []
        for i in range(6):
            self.position_preview_vars.append(
                {"label": tk.StringVar(value=self.position_name_vars[i].get()), "xyz": tk.StringVar(), "dist": tk.StringVar(), "down": tk.StringVar()}
            )
        self.mouth_sync_var = tk.StringVar(value="MOUTH: GUI changed / not applied")
        self.dock_sync_var = tk.StringVar(value="DOCK: GUI changed / not applied")
        self.safez_sync_var = tk.StringVar(value="SAFE Z: GUI changed / not applied")
        self.geometry_sync_var = tk.StringVar(value="Geometry: GUI changed / not applied")
        self.enabled_sync_var = tk.StringVar(value="Enabled positions: GUI changed / not applied")
        self.device_apply_state_var = tk.StringVar(value="Device settings not yet pushed since connect")
        self._applied_categories_since_connect = set()


    def _refresh_position_names(self, *_args):
        try:
            self.position_labels = [v.get().strip() or f"pos{i}" for i, v in enumerate(self.position_name_vars)]
            for i, name in enumerate(self.position_labels):
                self.position_preview_vars[i]["label"].set(name)
            if hasattr(self, "pos_stats_tree"):
                for i, name in enumerate(self.position_labels):
                    try:
                        vals = list(self.pos_stats_tree.item(f"pos{i}", "values"))
                        if vals:
                            vals[0] = name
                            self.pos_stats_tree.item(f"pos{i}", values=vals)
                    except Exception:
                        pass
            if hasattr(self, "timeline_canvas"):
                self._draw_task_raster()
            if hasattr(self, "xy_canvas"):
                self._draw_position_diagrams()
        except Exception:
            pass

    def _refresh_position_stats_tree(self):
        if not hasattr(self, "pos_stats_tree"):
            return
        for i in range(6):
            data = dict(self.position_stats.get(i, {}) or {})
            adapt_enabled = bool(self.adaptive_enabled_var.get()) and (
                not bool(self.adapt_use_per_position_var.get()) or bool(self.adapt_pos_enabled_vars[i].get())
            )
            vals = (
                self.position_labels[i] if i < len(self.position_labels) else f"pos{i}",
                data.get("enabled", ""),
                data.get("dist_mm", ""),
                data.get("trials", ""),
                data.get("hits", ""),
                data.get("misses", ""),
                data.get("free_rewards", ""),
                data.get("adaptive_hit_counter", "") if adapt_enabled else "",
            )
            try:
                self.pos_stats_tree.item(f"pos{i}", values=vals)
            except Exception:
                pass

    def _mark_mouth_dirty(self, *_args):
        try: self.mouth_sync_var.set("MOUTH: GUI changed / not applied")
        except Exception: pass

    def _mark_dock_dirty(self, *_args):
        try: self.dock_sync_var.set("DOCK: GUI changed / not applied")
        except Exception: pass

    def _mark_safez_dirty(self, *_args):
        try: self.safez_sync_var.set("SAFE Z: GUI changed / not applied")
        except Exception: pass

    def _mark_geometry_dirty(self, *_args):
        try: self.geometry_sync_var.set("Geometry: GUI changed / not applied")
        except Exception: pass

    def _mark_enabled_dirty(self, *_args):
        try: self.enabled_sync_var.set("Enabled positions: GUI changed / not applied")
        except Exception: pass

    def _adaptive_selected_pos_index(self):
        try:
            idx = int(str(self.adapt_selected_pos_var.get()).strip())
        except Exception:
            idx = 0
        return max(0, min(5, idx))

    def _sync_adaptive_position_from_global(self, idx):
        if not (0 <= idx < 6):
            return
        self.adapt_pos_enabled_vars[idx].set(bool(self.adaptive_enabled_var.get()))
        self.adapt_pos_hits_vars[idx].set(self.adapt_hits_var.get())
        self.adapt_pos_misses_vars[idx].set(self.adapt_misses_var.get())
        self.adapt_pos_step_vars[idx].set(self.adapt_step_var.get())
        self.adapt_pos_step_down_vars[idx].set(self.adapt_step_down_var.get())
        self.adapt_pos_min_vars[idx].set(self.adapt_min_var.get())
        self.adapt_pos_max_vars[idx].set(self.adapt_max_var.get())

    def _sync_all_adaptive_positions_from_global(self):
        for idx in range(6):
            self._sync_adaptive_position_from_global(idx)

    def _load_selected_adaptive_position_into_editor(self, *_args):
        idx = self._adaptive_selected_pos_index()
        self._adapt_editor_syncing = True
        try:
            self.adapt_edit_enabled_var.set(self.adapt_pos_enabled_vars[idx].get())
            self.adapt_edit_hits_var.set(self.adapt_pos_hits_vars[idx].get())
            self.adapt_edit_misses_var.set(self.adapt_pos_misses_vars[idx].get())
            self.adapt_edit_step_var.set(self.adapt_pos_step_vars[idx].get())
            self.adapt_edit_step_down_var.set(self.adapt_pos_step_down_vars[idx].get())
            self.adapt_edit_min_var.set(self.adapt_pos_min_vars[idx].get())
            self.adapt_edit_max_var.set(self.adapt_pos_max_vars[idx].get())
        finally:
            self._adapt_editor_syncing = False

    def _store_adaptive_editor_to_selected_position(self, *_args):
        if self._adapt_editor_syncing:
            return
        idx = self._adaptive_selected_pos_index()
        self.adapt_pos_enabled_vars[idx].set(bool(self.adapt_edit_enabled_var.get()))
        self.adapt_pos_hits_vars[idx].set(self.adapt_edit_hits_var.get())
        self.adapt_pos_misses_vars[idx].set(self.adapt_edit_misses_var.get())
        self.adapt_pos_step_vars[idx].set(self.adapt_edit_step_var.get())
        self.adapt_pos_step_down_vars[idx].set(self.adapt_edit_step_down_var.get())
        self.adapt_pos_min_vars[idx].set(self.adapt_edit_min_var.get())
        self.adapt_pos_max_vars[idx].set(self.adapt_edit_max_var.get())
        self._refresh_position_stats_tree()

    def _on_adaptive_scope_changed(self, *_args):
        if not self.adapt_use_per_position_var.get():
            self._sync_all_adaptive_positions_from_global()
            self._load_selected_adaptive_position_into_editor()
        self._refresh_adaptive_ui_state()
        self._refresh_position_stats_tree()

    def _refresh_adaptive_ui_state(self):
        use_per_position = bool(self.adapt_use_per_position_var.get())
        if hasattr(self, "adapt_all_frame"):
            self._set_children_state(self.adapt_all_frame, enabled=not use_per_position)
        if hasattr(self, "adapt_per_pos_frame"):
            self._set_children_state(self.adapt_per_pos_frame, enabled=use_per_position)

    def _on_global_adaptive_value_changed(self, *_args):
        if not self.adapt_use_per_position_var.get():
            self._sync_all_adaptive_positions_from_global()
            self._load_selected_adaptive_position_into_editor()
        self._refresh_position_stats_tree()

    def _required_apply_categories(self):
        required = {'mouth', 'dock', 'safez', 'geometry', 'enabled', 'cue', 'reward', 'timing', 'logic', 'adaptive'}
        if getattr(self, "backend_var", None) is not None and self.backend_var.get() != "mega_zaber":
            required.add('lick')
        return required

    def _mark_categories_applied(self, *cats):
        self._applied_categories_since_connect.update(cats)
        missing = self._required_apply_categories() - self._applied_categories_since_connect
        if missing:
            self.device_apply_state_var.set("Pending device apply: " + ", ".join(sorted(missing)))
        else:
            self.device_apply_state_var.set("All required session/task settings pushed since connect")

    def _reset_apply_tracking(self):
        self._applied_categories_since_connect = set()
        self.device_apply_state_var.set("Device settings not yet pushed since connect")

    # ---------------- UI build ----------------
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=8)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(9, weight=1)

        ttk.Label(top, text="Port:").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=34, state="readonly")
        self.port_combo.grid(row=0, column=1, columnspan=2, padx=4, sticky="ew")
        ttk.Button(top, text="Refresh", command=self.refresh_ports).grid(row=0, column=3, padx=4)
        ttk.Button(top, text="Probe", command=self.probe_selected_port).grid(row=0, column=4, padx=4)

        ttk.Label(top, text="Baud:").grid(row=0, column=5, sticky="w")
        ttk.Entry(top, textvariable=self.baud_var, width=8).grid(row=0, column=6, padx=4)
        ttk.Button(top, text="Connect", command=self.connect_serial).grid(row=0, column=7, padx=4)
        ttk.Button(top, text="Disconnect", command=self.disconnect_serial).grid(row=0, column=8, padx=4)

        ttk.Checkbutton(top, text="Auto STATUS poll", variable=self.autopoll_var).grid(row=0, column=9, padx=(8,2))
        ttk.Label(top, text="every (s)").grid(row=0, column=10, sticky="e")
        ttk.Entry(top, textvariable=self.autopoll_interval_var, width=6).grid(row=0, column=11, padx=(2,8), sticky="w")
        ttk.Button(top, text="Save Config", command=self.save_config).grid(row=0, column=12, padx=4)
        ttk.Button(top, text="Save Config As…", command=self.save_config_as).grid(row=0, column=13, padx=4)
        ttk.Button(top, text="Load Config", command=self.load_config_dialog).grid(row=0, column=14, padx=4, sticky="w")

        ttk.Label(top, textvariable=self.port_info_var, foreground="#444").grid(row=1, column=0, columnspan=12, sticky="w", pady=(4, 0))
        ttk.Label(top, textvariable=self.status_line_var, foreground="#055").grid(row=2, column=0, columnspan=12, sticky="w", pady=(6, 0))
        ttk.Label(top, text="Save dir:").grid(row=3, column=0, sticky="w", pady=(6,0))
        ttk.Entry(top, textvariable=self.save_dir_var, width=55).grid(row=3, column=1, columnspan=4, sticky="ew", padx=4, pady=(6,0))
        ttk.Button(top, text="Browse…", command=self.browse_save_dir).grid(row=3, column=5, padx=4, pady=(6,0))
        ttk.Label(top, text="Prefix:").grid(row=3, column=6, sticky="e", pady=(6,0))
        ttk.Entry(top, textvariable=self.session_prefix_var, width=14).grid(row=3, column=7, padx=4, pady=(6,0))
        ttk.Checkbutton(top, text="Auto-log on START", variable=self.auto_log_on_start_var).grid(row=3, column=8, sticky="w", padx=4, pady=(6,0))
        ttk.Label(top, textvariable=self.logging_state_var, foreground="#550").grid(row=3, column=9, sticky="w", padx=4, pady=(6,0))

        nb = ttk.Notebook(self)
        nb.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)

        self.tab_session = ttk.Frame(nb, padding=8)
        self.tab_visual = ttk.Frame(nb, padding=8)
        self.tab_motion = ttk.Frame(nb, padding=8)
        self.tab_task = ttk.Frame(nb, padding=8)
        self.tab_geometry = ttk.Frame(nb, padding=8)
        self.tab_smc = ttk.Frame(nb, padding=8)
        self.tab_console = ttk.Frame(nb, padding=8)

        nb.add(self.tab_session, text="1. Session")
        nb.add(self.tab_visual, text="2. Visualization")
        nb.add(self.tab_motion, text="3. Motion & Calibration")
        nb.add(self.tab_task, text="4. Task Structure")
        nb.add(self.tab_geometry, text="5. Geometry / Adaptive")
        nb.add(self.tab_console, text="6. Console")

        self._build_session_tab()
        self._build_visual_tab()
        self._build_motion_tab()
        self._build_task_tab()
        self._build_geometry_tab()
        self._build_console_tab()

    def _build_session_tab(self):
        tab = self.tab_session
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        outer, f = self._make_scrollable_container(tab)
        outer.grid(row=0, column=0, sticky="nsew")
        for i in range(4):
            f.columnconfigure(i, weight=1)

        top_pane = ttk.Panedwindow(f, orient="horizontal")
        top_pane.grid(row=0, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)

        current = ttk.LabelFrame(top_pane, text="Current status", padding=8)
        current.columnconfigure(1, weight=1)
        ttk.Label(current, text="X").grid(row=0, column=0, sticky="w")
        ttk.Label(current, textvariable=self.curr_x_var).grid(row=0, column=1, sticky="w")
        ttk.Label(current, text="Y").grid(row=1, column=0, sticky="w")
        ttk.Label(current, textvariable=self.curr_y_var).grid(row=1, column=1, sticky="w")
        ttk.Label(current, text="Z").grid(row=2, column=0, sticky="w")
        ttk.Label(current, textvariable=self.curr_z_var).grid(row=2, column=1, sticky="w")
        ttk.Label(current, textvariable=self.status_line_var, wraplength=520, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(8,4))
        ttk.Label(current, textvariable=self.current_pos_label_var, justify="left", wraplength=520).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(current, textvariable=self.block_summary_var, justify="left", wraplength=520).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(current, textvariable=self.reward_summary_var, justify="left", wraplength=520).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(current, textvariable=self.scheduler_summary_var, justify="left", wraplength=520).grid(row=7, column=0, columnspan=2, sticky="w")
        ttk.Label(current, textvariable=self.remaining_summary_var, justify="left", wraplength=520).grid(row=8, column=0, columnspan=2, sticky="w")
        ttk.Label(current, textvariable=self.visual_elapsed_var, justify="left", wraplength=520).grid(row=9, column=0, columnspan=2, sticky="w", pady=(4,0))
        ttk.Label(current, textvariable=self.last_event_var, wraplength=520, justify="left").grid(row=10, column=0, columnspan=2, sticky="w", pady=(6,0))

        current_btns = ttk.Frame(current)
        current_btns.grid(row=11, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(current_btns, text="GET status", command=lambda: self.send("GET kind=status")).grid(row=0, column=0, padx=(0,6), pady=(0,4), sticky="w")
        ttk.Button(current_btns, text="GET positions", command=lambda: self.send("GET kind=positions")).grid(row=0, column=1, padx=(0,6), pady=(0,4), sticky="w")
        ttk.Button(current_btns, text="GET stats", command=lambda: self.send("GET kind=stats")).grid(row=1, column=0, padx=(0,6), sticky="w")
        ttk.Button(current_btns, text="Fetch device state", command=self.fetch_device_state).grid(row=1, column=1, padx=(0,6), sticky="w")

        right_pane = ttk.Panedwindow(top_pane, orient="horizontal")

        session = ttk.LabelFrame(right_pane, text="Session control", padding=8)
        session.grid_columnconfigure(0, weight=1)
        self.home_ref_button = ttk.Button(session, text="HOME", command=self.home_or_reference_action)
        self.home_ref_button.grid(row=0, column=0, sticky="ew", pady=2)
        ttk.Button(session, text="Apply ALL session/task settings", command=self.apply_all_session_task_settings).grid(row=1, column=0, sticky="ew", pady=2)
        ttk.Label(session, textvariable=self.device_apply_state_var, wraplength=320, justify="left", foreground="#6a4").grid(row=2, column=0, sticky="w", pady=(2,6))
        ttk.Button(session, text="START TASK", command=self.start_task).grid(row=3, column=0, sticky="ew", pady=2)
        ttk.Button(session, text="STOP", command=self.stop_task).grid(row=4, column=0, sticky="ew", pady=2)
        ttk.Button(session, text="RESET SESSION", command=self.reset_session).grid(row=5, column=0, sticky="ew", pady=2)
        ttk.Button(session, text="Manual reward", command=self.send_manual_reward).grid(row=6, column=0, sticky="ew", pady=2)
        ttk.Button(session, text="Cue only", command=lambda: self.send("CUE")).grid(row=7, column=0, sticky="ew", pady=2)
        ttk.Button(session, text="Cue + reward", command=self.send_cue_reward).grid(row=8, column=0, sticky="ew", pady=2)

        summary = ttk.LabelFrame(right_pane, text="Notes", padding=8)
        summary.grid_columnconfigure(0, weight=1)
        self.session_notes_var = tk.StringVar(value="")
        ttk.Label(summary, textvariable=self.session_notes_var, justify="left", wraplength=360).grid(row=0, column=0, sticky="nw")

        top_pane.add(current, weight=4)
        right_pane.add(session, weight=3)
        right_pane.add(summary, weight=1)
        top_pane.add(right_pane, weight=3)

        logging = ttk.LabelFrame(f, text="Session data logging", padding=8)
        logging.grid(row=1, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)
        logging.columnconfigure(1, weight=1)
        ttk.Label(logging, text="Save directory").grid(row=0, column=0, sticky="w")
        ttk.Entry(logging, textvariable=self.save_dir_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(logging, text="Browse…", command=self.browse_save_dir).grid(row=0, column=2, padx=4)
        ttk.Label(logging, text="Prefix").grid(row=1, column=0, sticky="w")
        ttk.Entry(logging, textvariable=self.session_prefix_var, width=20).grid(row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Checkbutton(logging, text="Auto-start logging when START is sent", variable=self.auto_log_on_start_var).grid(row=1, column=2, sticky="w")
        ttk.Checkbutton(logging, text="Raw protocol log (debug)", variable=self.raw_protocol_log_var).grid(row=2, column=0, sticky="w", pady=(2,0))
        ttk.Checkbutton(logging, text="Timeseries CSV", variable=self.timeseries_enabled_var).grid(row=2, column=1, sticky="w", pady=(2,0))
        ttk.Frame(logging).grid(row=2, column=2, sticky="w")
        ttk.Label(logging, text="Timeseries interval (ms)").grid(row=3, column=0, sticky="w", pady=(2,0))
        ttk.Entry(logging, textvariable=self.timeseries_interval_var, width=8).grid(row=3, column=1, sticky="w", padx=4, pady=(2,0))
        ttk.Button(logging, text="Start logging now", command=self.start_session_logging).grid(row=4, column=0, sticky="ew", pady=4)
        ttk.Button(logging, text="Stop logging", command=self.stop_session_logging).grid(row=4, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(logging, textvariable=self.logging_state_var).grid(row=4, column=2, sticky="w")
        ttk.Label(logging, text="Saved files: events.csv, trials.csv, config_changes.csv, gui_config.json, gui_config_end.json, device_snapshot_start.json, device_snapshot_end.json, session_manifest.json, summary_end.json, position_stats_end.csv, optional timeseries.csv, optional raw_protocol.log", justify="left", wraplength=1000).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4,0))

        cumf = ttk.LabelFrame(f, text="Cumulative task raster", padding=8)
        cumf.grid(row=2, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)
        cumf.columnconfigure(0, weight=1)
        cumf.rowconfigure(1, weight=1)
        ttk.Label(
            cumf,
            text="Green dot = hit / earned reward   |   Red dot = miss   |   Teal ring = free, auto-delay, or manual reward",
            justify="left",
            wraplength=1100,
        ).grid(row=0, column=0, sticky="w", pady=(0,6))
        self.cumulative_timeline_canvas = tk.Canvas(cumf, bg="white", height=220, highlightthickness=1, highlightbackground="#cccccc")
        self.cumulative_timeline_canvas.grid(row=1, column=0, sticky="nsew")

        statsf = ttk.LabelFrame(f, text="Per-position stats", padding=8)
        statsf.grid(row=3, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)
        cols = ("name", "enabled", "dist_mm", "trials", "hits", "misses", "free_rewards", "adaptive")
        self.pos_stats_tree = ttk.Treeview(statsf, columns=cols, show="headings", height=6)
        self.pos_stats_tree.grid(row=0, column=0, sticky="nsew")
        statsf.columnconfigure(0, weight=1)
        statsf.rowconfigure(0, weight=1)
        headings = {"name":"Name", "enabled":"On", "dist_mm":"Dist (mm)", "trials":"Trials", "hits":"Hits", "misses":"Misses", "free_rewards":"Free", "adaptive":"Adaptive"}
        for c in cols:
            self.pos_stats_tree.heading(c, text=headings[c])
            self.pos_stats_tree.column(c, width=80, anchor="center")
        self.pos_stats_tree.column("name", width=120, anchor="w")
        self.pos_stats_tree.column("dist_mm", width=90, anchor="center")
        for i in range(6):
            self.pos_stats_tree.insert("", "end", iid=f"pos{i}", values=(self.position_name_vars[i].get(),"1","",0,0,0,0,0))

    def _build_visual_tab(self):
        f = self.tab_visual
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)

        outer = ttk.Panedwindow(f, orient="horizontal")
        outer.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        controls_host, controls = self._build_scrolled_labelframe(outer, "Visualization controls / summary", padding=8)
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Use this tab before START to verify lick detection, cue-only, reward, and position moves.", wraplength=280, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(controls, text="Auto STATUS poll", variable=self.autopoll_var).grid(row=1, column=0, sticky="w", pady=(6,2))
        pollf = ttk.Frame(controls)
        pollf.grid(row=1, column=1, sticky="e")
        ttk.Label(pollf, text="every").grid(row=0, column=0, sticky="e")
        ttk.Entry(pollf, textvariable=self.autopoll_interval_var, width=6).grid(row=0, column=1, padx=(4,2))
        ttk.Label(pollf, text="s").grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(controls, text="Lick debug streaming", variable=self.lick_debug_var).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Button(controls, text="Apply lick debug setting", command=self.apply_lick_settings).grid(row=3, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(controls, text="Enable lick debug + apply", command=self.enable_live_lick_monitor).grid(row=4, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(controls, text="Test position row").grid(row=5, column=0, sticky="w", pady=(8,2))
        self.monitor_pos_combo = ttk.Combobox(controls, textvariable=self.monitor_pos_var, state="readonly", values=[str(i) for i in range(6)], width=8)
        self.monitor_pos_combo.grid(row=5, column=1, sticky="w", pady=(8,2))
        ttk.Button(controls, text="Move to test position", command=self.move_to_monitor_position).grid(row=6, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(controls, text="6-position test").grid(row=7, column=0, sticky="w", pady=(8,2))
        ttk.Frame(controls).grid(row=7, column=1, sticky="ew")
        ttk.Label(controls, text="Dwell ms").grid(row=8, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.sequence_dwell_var, width=10).grid(row=8, column=1, sticky="w")
        ttk.Label(controls, text="Cycles").grid(row=9, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.sequence_cycles_var, width=10).grid(row=9, column=1, sticky="w")
        ttk.Checkbutton(controls, text="Cue at each position", variable=self.sequence_with_cue_var).grid(row=10, column=0, columnspan=2, sticky="w")
        ttk.Button(controls, text="Run 6-position test", command=self.start_position_sequence).grid(row=11, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(controls, text="Stop test sequence", command=self.stop_position_sequence).grid(row=12, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(controls, textvariable=self.sequence_status_var, wraplength=280, justify="left").grid(row=13, column=0, columnspan=2, sticky="w")
        ttk.Button(controls, text="Cue only", command=lambda: self.send("CUE")).grid(row=14, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(controls, text="Manual reward", command=self.send_manual_reward).grid(row=15, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(controls, text="Cue + reward", command=self.send_cue_reward).grid(row=16, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(controls, textvariable=self.reward_hold_button_var, command=self.toggle_reward_hold).grid(row=17, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(controls, text="Clear visualization", command=self.clear_visuals).grid(row=18, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Separator(controls, orient="horizontal").grid(row=19, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(controls, textvariable=self.visual_pos_var, justify="left", wraplength=280).grid(row=20, column=0, columnspan=2, sticky="w")
        ttk.Label(controls, textvariable=self.visual_block_var, justify="left", wraplength=280).grid(row=21, column=0, columnspan=2, sticky="w")
        ttk.Label(controls, textvariable=self.visual_trial_var, justify="left", wraplength=280).grid(row=22, column=0, columnspan=2, sticky="w")
        ttk.Label(controls, textvariable=self.visual_elapsed_var, justify="left", wraplength=280).grid(row=23, column=0, columnspan=2, sticky="w")
        ttk.Label(controls, textvariable=self.last_event_var, wraplength=280, justify="left").grid(row=24, column=0, columnspan=2, sticky="w", pady=(6,0))
        ttk.Label(controls, text=(
            "Raster rows: one per spout position.\n"
            "Green = earned reward, red = miss or pre-cue reset, teal ring = free/auto/manual reward.\n"
            "Top rows show Sync, Cue, and thresholded Licks. Use the 6-position test to verify row labeling before START."
        ), wraplength=280, justify="left").grid(row=25, column=0, columnspan=2, sticky="w", pady=(8,0))

        vis = ttk.Panedwindow(outer, orient="vertical")
        outer.add(controls_host, weight=1)
        outer.add(vis, weight=4)

        lickf = ttk.LabelFrame(vis, text="Live lick trace (20 s)", padding=4)
        lickf.columnconfigure(0, weight=1)
        lickf.rowconfigure(0, weight=1)
        self.lick_canvas = tk.Canvas(lickf, bg="white", height=220, highlightthickness=1, highlightbackground="#cccccc")
        self.lick_canvas.grid(row=0, column=0, sticky="nsew")

        rastf = ttk.LabelFrame(vis, text="Task raster / recent events (20 s)", padding=4)
        rastf.columnconfigure(0, weight=1)
        rastf.rowconfigure(0, weight=1)
        self.timeline_canvas = tk.Canvas(rastf, bg="white", height=300, highlightthickness=1, highlightbackground="#cccccc")
        self.timeline_canvas.grid(row=0, column=0, sticky="nsew")

        pos_pane = ttk.Panedwindow(vis, orient="horizontal")
        xyf = ttk.LabelFrame(pos_pane, text="Current position layout: XY relative to mouth (camera view)", padding=4)
        xyf.columnconfigure(0, weight=1)
        xyf.rowconfigure(0, weight=1)
        self.xy_canvas = tk.Canvas(xyf, bg="white", height=240, highlightthickness=1, highlightbackground="#cccccc")
        self.xy_canvas.grid(row=0, column=0, sticky="nsew")
        yzf = ttk.LabelFrame(pos_pane, text="Current position layout: YZ relative to mouth (camera view)", padding=4)
        yzf.columnconfigure(0, weight=1)
        yzf.rowconfigure(0, weight=1)
        self.yz_canvas = tk.Canvas(yzf, bg="white", height=240, highlightthickness=1, highlightbackground="#cccccc")
        self.yz_canvas.grid(row=0, column=0, sticky="nsew")
        pos_pane.add(xyf, weight=1)
        pos_pane.add(yzf, weight=1)

        vis.add(lickf, weight=3)
        vis.add(rastf, weight=4)
        vis.add(pos_pane, weight=3)

    def _build_motion_tab(self):

        f = self.tab_motion
        for i in range(3):
            f.columnconfigure(i, weight=1)
        f.rowconfigure(0, weight=1)

        left_col = ttk.Frame(f)
        left_col.grid(row=0, column=0, sticky="nsew")
        left_col.columnconfigure(0, weight=1)

        jog = ttk.LabelFrame(left_col, text="Jog", padding=8)
        jog.grid(row=0, column=0, sticky="nsew", padx=4, pady=(4,0))
        ttk.Label(jog, text="Jog step (mm)").grid(row=0, column=0, sticky="w")
        ttk.Entry(jog, textvariable=self.jog_step_var, width=10).grid(row=0, column=1, sticky="w")

        row = 1
        for axis in ("X", "Y", "Z"):
            ttk.Label(jog, text=axis).grid(row=row, column=0, sticky="w")
            ttk.Button(jog, text=f"{axis} -", command=lambda a=axis: self.jog_axis(a, -1)).grid(row=row, column=1, padx=2, pady=2)
            ttk.Button(jog, text=f"{axis} +", command=lambda a=axis: self.jog_axis(a, +1)).grid(row=row, column=2, padx=2, pady=2)
            row += 1

        mouse_profile = ttk.LabelFrame(left_col, text="Mouse profile", padding=8)
        mouse_profile.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0,4))
        self.motion_mouse_profile_frame = mouse_profile
        ttk.Label(mouse_profile, text="Current profile").grid(row=0, column=0, sticky="w")
        ttk.Label(mouse_profile, textvariable=self.current_mouse_profile_var, foreground="#1f4e79").grid(row=0, column=1, sticky="w", padx=(8,0))
        ttk.Button(mouse_profile, text="Save As…", command=self.save_mouse_profile).grid(row=2, column=0, sticky="ew", pady=2)
        ttk.Button(mouse_profile, text="Load…", command=self.load_mouse_profile).grid(row=2, column=1, sticky="ew", padx=(6,0), pady=2)
        ttk.Button(mouse_profile, text="Load coords from config…", command=self.load_motion_coords_from_config_dialog).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4,2))
        ttk.Label(mouse_profile, text="Profiles are saved as JSON files in the mouse-profiles folder and can restore mouth and related settings.", wraplength=240, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(8,0))

        for child in mouse_profile.grid_slaves():
            try:
                txt = child.cget("text")
            except Exception:
                txt = ""
            try:
                txtvar = child.cget("textvariable")
            except Exception:
                txtvar = ""
            if txt == "Current profile":
                child.grid_configure(row=0, column=0, sticky="w")
            elif txtvar == str(self.current_mouse_profile_var):
                child.grid_configure(row=0, column=1, sticky="w", padx=(8,0))
            elif txt.startswith("Save As"):
                child.grid_configure(row=1, column=0, sticky="ew", pady=(6,2))
            elif txt.startswith("Load"):
                child.grid_configure(row=1, column=1, sticky="ew", padx=(6,0), pady=(6,2))
            elif txt.startswith("Load coords from config"):
                child.grid_configure(row=2, column=0, columnspan=2, sticky="ew", pady=(4,2))
            elif txt.startswith("Profiles are saved"):
                child.configure(wraplength=280, justify="left")
                child.grid_configure(row=3, column=0, columnspan=2, sticky="w", pady=(6,0))

        move_abs = ttk.LabelFrame(f, text="Coordinates", padding=8)
        move_abs.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        ttk.Label(move_abs, text="Mouth X/Y/Z").grid(row=0, column=0, sticky="w")
        ttk.Entry(move_abs, textvariable=self.mouth_x_var, width=9).grid(row=1, column=0)
        ttk.Entry(move_abs, textvariable=self.mouth_y_var, width=9).grid(row=1, column=1)
        ttk.Entry(move_abs, textvariable=self.mouth_z_var, width=9).grid(row=1, column=2)
        ttk.Button(move_abs, text="Set MOUTH", command=self.apply_mouth).grid(row=2, column=0, pady=4, sticky="ew")
        ttk.Button(move_abs, text="Use current XYZ", command=self.set_mouth_from_current).grid(row=2, column=1, columnspan=2, pady=4, sticky="ew")
        ttk.Label(move_abs, textvariable=self.mouth_sync_var, foreground="#555").grid(row=3, column=0, columnspan=3, sticky="w")

        ref_frame = ttk.LabelFrame(move_abs, text="Reference XYZ (Teensy only)", padding=6)
        ref_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8,0))
        self.motion_reference_frame = ref_frame
        ttk.Entry(ref_frame, textvariable=self.ref_x_var, width=9).grid(row=0, column=0)
        ttk.Entry(ref_frame, textvariable=self.ref_y_var, width=9).grid(row=0, column=1)
        ttk.Entry(ref_frame, textvariable=self.ref_z_var, width=9).grid(row=0, column=2)
        ttk.Button(ref_frame, text="Set REFERENCE", command=self.apply_current_reference).grid(row=1, column=0, pady=4, sticky="ew")
        ttk.Button(ref_frame, text="Use current XYZ", command=self.set_ref_from_current).grid(row=1, column=1, columnspan=2, pady=4, sticky="ew")

        ttk.Label(move_abs, text="Dock X/Y/Z").grid(row=5, column=0, sticky="w", pady=(10,0))
        ttk.Entry(move_abs, textvariable=self.dock_x_var, width=9).grid(row=6, column=0)
        ttk.Entry(move_abs, textvariable=self.dock_y_var, width=9).grid(row=6, column=1)
        ttk.Entry(move_abs, textvariable=self.dock_z_var, width=9).grid(row=6, column=2)
        ttk.Button(move_abs, text="Set DOCK", command=self.apply_dock).grid(row=7, column=0, pady=4, sticky="ew")
        ttk.Button(move_abs, text="Use current XYZ", command=self.set_dock_from_current).grid(row=7, column=1, columnspan=2, pady=4, sticky="ew")
        ttk.Label(move_abs, textvariable=self.dock_sync_var, foreground="#555").grid(row=8, column=0, columnspan=3, sticky="w")

        ttk.Label(move_abs, text="Safe Z").grid(row=9, column=0, sticky="w", pady=(10,0))
        ttk.Entry(move_abs, textvariable=self.safe_z_var, width=9).grid(row=9, column=1, sticky="w")
        ttk.Button(move_abs, text="Set SAFEZ", command=self.apply_safez).grid(row=9, column=2, sticky="ew")
        ttk.Button(move_abs, text="Use current Z", command=self.set_safez_from_current).grid(row=10, column=1, columnspan=2, pady=4, sticky="ew")
        ttk.Label(move_abs, textvariable=self.safez_sync_var, foreground="#555").grid(row=11, column=0, columnspan=3, sticky="w")

        ttk.Button(move_abs, text="Push all origins + geometry", command=self.push_all_origins_geometry).grid(row=12, column=0, columnspan=3, pady=(10, 2), sticky="ew")
        ttk.Button(move_abs, text="MOVE to current mouth", command=self.move_to_mouth).grid(row=13, column=0, pady=(6, 2), sticky="ew")
        ttk.Button(move_abs, text="MOVE to dock", command=self.move_to_dock).grid(row=13, column=1, pady=(6, 2), sticky="ew")
        self.move_to_ref_button = ttk.Button(move_abs, text="MOVE to ref XYZ", command=self.move_to_xyz_fields)
        self.move_to_ref_button.grid(row=13, column=2, pady=(6, 2), sticky="ew")

        axiscal = ttk.LabelFrame(f, text="Axis timed-motion calibration (ms/mm)", padding=8)
        axiscal.grid(row=0, column=2, sticky="nsew", padx=4, pady=4)
        self.motion_axiscal_frame = axiscal
        headers = ["Axis", "ms/mm +", "ms/mm -", "overhead ms", "CW is +"]
        for j, h in enumerate(headers):
            ttk.Label(axiscal, text=h).grid(row=0, column=j, padx=3, pady=2)
        for i, axis in enumerate(("X", "Y", "Z"), start=1):
            ttk.Label(axiscal, text=axis).grid(row=i, column=0, sticky="w")
            ttk.Entry(axiscal, textvariable=self.axis_cal[axis]["ms_pos"], width=8).grid(row=i, column=1)
            ttk.Entry(axiscal, textvariable=self.axis_cal[axis]["ms_neg"], width=8).grid(row=i, column=2)
            ttk.Entry(axiscal, textvariable=self.axis_cal[axis]["overhead"], width=8).grid(row=i, column=3)
            ttk.Checkbutton(axiscal, variable=self.axis_cal[axis]["cw_pos"]).grid(row=i, column=4)
            ttk.Button(axiscal, text=f"Apply {axis} scale", command=lambda a=axis: self.apply_axis_cal(a)).grid(row=i, column=5, padx=4)
        ttk.Button(axiscal, text="Apply all axes", command=self.apply_all_axis_cal).grid(row=4, column=0, columnspan=6, sticky="ew", pady=(8,0))
        ttk.Button(axiscal, text="Use theoretical ms/mm from calculator", command=self.populate_axis_cal_from_calculator).grid(row=5, column=0, columnspan=6, sticky="ew", pady=(4,0))

        zaber_note = ttk.LabelFrame(f, text="Zaber note", padding=8)
        zaber_note.grid(row=2, column=0, columnspan=3, sticky="nsew", padx=4, pady=4)
        self.motion_zaber_note_frame = zaber_note
        ttk.Label(zaber_note, text=(
            "For the Mega/Zaber backend, timed-motion calibration does not apply. "
            "Use the jog controls and XYZ origin/dock controls here, but set device IDs, units/mm, and raw-device testing in the Backend tab."
        ), wraplength=900, justify="left").grid(row=0, column=0, sticky="w")

    def _build_task_tab(self):
        tab = self.tab_task
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        outer, f = self._make_scrollable_container(tab)
        outer.grid(row=0, column=0, sticky="nsew")
        f.columnconfigure(0, weight=1)

        top = ttk.Panedwindow(f, orient="horizontal")
        top.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        bottom = ttk.Panedwindow(f, orient="horizontal")
        bottom.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        timing = ttk.LabelFrame(top, text="Timing", padding=8)
        self._labeled_entry(timing, "Target settle ms", self.settle_ms_var, 0)
        self._labeled_entry(timing, "Post reward hold ms", self.posthold_ms_var, 1)
        self._labeled_entry(timing, "Pre-cue min ms", self.precue_min_var, 2)
        self._labeled_entry(timing, "Pre-cue max ms", self.precue_max_var, 3)
        self._labeled_entry(timing, "Response window ms", self.response_window_var, 4)
        self._labeled_entry(timing, "ITI min ms", self.iti_min_var, 5)
        self._labeled_entry(timing, "ITI jitter ms", self.iti_jitter_var, 6)
        self._labeled_entry(timing, "Sync pulse min ms", self.sync_pulse_min_var, 7)
        self._labeled_entry(timing, "Sync pulse max ms", self.sync_pulse_max_var, 8)
        self._labeled_entry(timing, "Sync interval min ms", self.sync_interval_min_var, 9)
        self._labeled_entry(timing, "Sync interval max ms", self.sync_interval_max_var, 10)
        ttk.Button(timing, text="Apply timing", command=self.apply_timing_settings).grid(row=11, column=0, columnspan=2, sticky="ew", pady=(8,0))

        logic = ttk.LabelFrame(top, text="Task logic / reward mode", padding=8)
        ttk.Label(logic, text="Reward mode").grid(row=0, column=0, sticky="w")
        self.reward_mode_combo = ttk.Combobox(logic, textvariable=self.reward_mode_var, state="readonly", values=[
            "contingent",
            "auto_after_delay",
            "contingent_or_auto_after_delay",
        ], width=32)
        self.reward_mode_combo.grid(row=1, column=0, sticky="w")
        self._labeled_entry(logic, "Auto reward delay ms", self.auto_reward_delay_var, 2)
        ttk.Checkbutton(logic, text="Auto-hold rewards after misses", variable=self.auto_hold_after_miss_enabled_var).grid(row=3, column=0, sticky="w")
        self._labeled_entry(logic, "Miss threshold", self.auto_hold_after_miss_threshold_var, 4)
        ttk.Checkbutton(logic, text="Enforce no-lick pre-cue period", variable=self.enforce_var).grid(row=5, column=0, sticky="w")
        ttk.Label(logic, text=(
            "contingent: reward only after a post-cue lick\n"
            "auto_after_delay: reward automatically after the delay\n"
            "contingent_or_auto_after_delay: reward on lick, or if no lick occurs, reward automatically after the delay\n"
            "Auto-hold after misses: in auto reward modes, hold further rewards after the threshold of consecutive missed rewarded trials; resume immediately on any detected lick, including between trials if contact is already present."
        ), wraplength=480, justify="left").grid(row=6, column=0, columnspan=2, sticky="w", pady=(6,0))
        ttk.Button(logic, text="Apply task logic", command=self.apply_logic_settings).grid(row=7, column=0, sticky="ew", pady=(8,0))

        top.add(timing, weight=1)
        top.add(logic, weight=1)

        cuef = ttk.LabelFrame(bottom, text="Cue / audio", padding=8)
        self.cue_freq_label = ttk.Label(cuef, text="Cue frequency (Hz)")
        self.cue_freq_label.grid(row=0, column=0, sticky="w", padx=2, pady=2)
        self.cue_freq_entry = ttk.Entry(cuef, textvariable=self.cue_hz_var, width=14)
        self.cue_freq_entry.grid(row=0, column=1, sticky="w", padx=2, pady=2)
        self.cue_dur_label = ttk.Label(cuef, text="Cue duration (ms)")
        self.cue_dur_label.grid(row=1, column=0, sticky="w", padx=2, pady=2)
        self.cue_dur_entry = ttk.Entry(cuef, textvariable=self.cue_duration_var, width=14)
        self.cue_dur_entry.grid(row=1, column=1, sticky="w", padx=2, pady=2)
        self.cue_vol_label = ttk.Label(cuef, text="Cue volume (%)")
        self.cue_vol_label.grid(row=2, column=0, sticky="w", padx=2, pady=2)
        self.cue_vol_entry = ttk.Entry(cuef, textvariable=self.cue_volume_var, width=14)
        self.cue_vol_entry.grid(row=2, column=1, sticky="w", padx=2, pady=2)
        ttk.Button(cuef, text="Apply cue/audio", command=self.apply_cue_settings).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8,0))
        self.cue_note_var = tk.StringVar(value=(
            "Use Cue only from the Session tab to test cue delivery before starting the task.\n"
            "Volume is implemented as PWM duty cycle on the speaker pin, so perceived loudness depends on your amplifier/speaker."
        ))
        self.cue_note_label = ttk.Label(cuef, textvariable=self.cue_note_var, wraplength=420, justify="left")
        self.cue_note_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6,0))

        reward = ttk.LabelFrame(bottom, text="Reward / session", padding=8)
        self._labeled_entry(reward, "Reward open ms", self.reward_ms_var, 0)
        self._labeled_entry(reward, "Reward size (uL)", self.reward_ul_var, 1)
        self._labeled_entry(reward, "Session water limit (uL)", self.water_limit_ul_var, 2)
        ttk.Button(reward, text="Apply reward/session", command=self.apply_reward_settings).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8,0))
        ttk.Separator(reward, orient="horizontal").grid(row=4, column=0, columnspan=2, sticky="ew", pady=6)
        self._labeled_entry(reward, "Calibration pulses", self.reward_cal_pulses_var, 5)
        ttk.Button(reward, text="Run reward calibration pulses", command=self.run_reward_calibration).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6,0))
        ttk.Label(reward, text="Opens the solenoid repeatedly (e.g. 100 pulses). Measure total volume, then adjust reward open ms until per-reward volume is correct.", wraplength=420, justify="left").grid(row=7, column=0, columnspan=2, sticky="w", pady=(6,0))

        bottom.add(cuef, weight=1)
        bottom.add(reward, weight=1)

        lower = ttk.Panedwindow(f, orient="horizontal")
        lower.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)

        freef = ttk.LabelFrame(lower, text="Free reward", padding=8)
        ttk.Checkbutton(freef, text="Free reward enabled", variable=self.free_reward_enabled_var).grid(row=0, column=0, columnspan=2, sticky="w")
        self._labeled_entry(freef, "After consecutive misses", self.free_after_misses_var, 1)
        self._labeled_entry(freef, "Free reward delay after cue (ms)", self.free_delay_var, 2)
        self.free_reward_note_var = tk.StringVar()
        ttk.Label(freef, textvariable=self.free_reward_note_var, wraplength=360, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6,0))
        ttk.Button(freef, text="Apply free reward", command=self.apply_free_reward_settings).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8,0))

        schedule = ttk.LabelFrame(lower, text="Session limits / block scheduling", padding=8)
        ttk.Checkbutton(schedule, text="Enable target trials per position", variable=self.target_trials_enabled_var, command=self._update_scheduler_summary).grid(row=0, column=0, columnspan=2, sticky="w")
        self._labeled_entry(schedule, "Target trials / position", self.target_trials_per_pos_var, 1)
        ttk.Checkbutton(schedule, text="Enable max duration", variable=self.max_duration_enabled_var, command=self._update_scheduler_summary).grid(row=2, column=0, columnspan=2, sticky="w")
        self._labeled_entry(schedule, "Max duration (min)", self.max_duration_min_var, 3)
        ttk.Label(schedule, text="Block size min").grid(row=0, column=2, sticky="w", padx=(18,2))
        ttk.Entry(schedule, textvariable=self.block_size_min_var, width=10).grid(row=0, column=3, sticky="w")
        ttk.Label(schedule, text="Block size max").grid(row=1, column=2, sticky="w", padx=(18,2))
        ttk.Entry(schedule, textvariable=self.block_size_max_var, width=10).grid(row=1, column=3, sticky="w")
        ttk.Label(schedule, text="Scheduling mode").grid(row=2, column=2, sticky="w", padx=(18,2))
        self.scheduling_mode_combo = ttk.Combobox(schedule, textvariable=self.scheduling_mode_var, state="readonly", values=["balanced_block_cycles", "random_blocks"], width=26)
        self.scheduling_mode_combo.grid(row=2, column=3, sticky="w")
        ttk.Label(schedule, text="Stop when limit reached").grid(row=3, column=2, sticky="w", padx=(18,2))
        self.stop_mode_combo = ttk.Combobox(schedule, textvariable=self.stop_mode_var, state="readonly", values=["end_of_current_block", "end_of_balanced_cycle"], width=26)
        self.stop_mode_combo.grid(row=3, column=3, sticky="w")
        ttk.Label(schedule, textvariable=self.scheduler_summary_var, wraplength=880, justify="left").grid(row=4, column=0, columnspan=4, sticky="w", pady=(8,0))
        ttk.Button(schedule, text="Apply scheduling / limits", command=self.apply_timing_settings).grid(row=5, column=0, columnspan=4, sticky="ew", pady=(8,0))

        lower.add(freef, weight=1)
        lower.add(schedule, weight=2)

        for _v in (
            self.target_trials_enabled_var, self.target_trials_per_pos_var,
            self.max_duration_enabled_var, self.max_duration_min_var,
            self.block_size_min_var, self.block_size_max_var,
            self.scheduling_mode_var, self.stop_mode_var,
            *self.pos_enabled_vars,
        ):
            try:
                _v.trace_add("write", lambda *_args: self._update_scheduler_summary())
            except Exception:
                pass
        try:
            self.reward_mode_var.trace_add("write", lambda *_args: self._update_free_reward_note())
        except Exception:
            pass
        self._update_scheduler_summary()
        self._update_free_reward_note()

    def _update_scheduler_summary(self):

        try:
            enabled = sum(1 for v in self.pos_enabled_vars if v.get())
        except Exception:
            enabled = 0
        block_min = max(1, self.block_size_min_var.get_int(5))
        block_max = max(block_min, self.block_size_max_var.get_int(block_min))
        mode = self.scheduling_mode_var.get().strip() or "balanced_block_cycles"
        stop_mode = self.stop_mode_var.get().strip() or "end_of_current_block"
        parts = [f"Enabled positions: {enabled}", f"Block size range: {block_min}–{block_max}", f"Scheduling: {mode}", f"Stop mode: {stop_mode}"]
        if self.target_trials_enabled_var.get() and enabled > 0:
            target = max(1, self.target_trials_per_pos_var.get_int(50))
            parts.append(f"Target trials / position: {target}")
            parts.append(f"Derived target total trials: {target * enabled}")
        else:
            parts.append("Target trials / position: disabled")
        if self.max_duration_enabled_var.get():
            parts.append(f"Max duration: {max(1, self.max_duration_min_var.get_int(60))} min")
        else:
            parts.append("Max duration: disabled")
        if mode == "balanced_block_cycles":
            parts.append("Balanced cycles use one block per remaining enabled position, shuffled each cycle.")
        self.scheduler_summary_var.set("  |  ".join(parts))

    def _update_free_reward_note(self):

        mode = (self.reward_mode_var.get().strip() if getattr(self, "reward_mode_var", None) is not None else "")
        if mode == "contingent":
            msg = (
                "Counts consecutive misses globally across positions. Example: 4 misses at A + 1 miss at B -> next trial becomes a free-reward trial. "
                "This is the mode where free reward is usually most meaningful."
            )
        elif mode == "auto_after_delay":
            msg = (
                "Counts consecutive misses globally across positions, but auto_after_delay produces automatic rewards instead of misses, "
                "so free reward will normally never trigger in this mode."
            )
        else:
            msg = (
                "Counts consecutive misses globally across positions. In contingent_or_auto_after_delay, free reward only matters if misses can actually accumulate before automatic reward occurs."
            )
        if getattr(self, "free_reward_note_var", None) is not None:
            self.free_reward_note_var.set(msg)

    def _build_geometry_tab(self):
        f = self.tab_geometry
        for i in range(3):
            f.columnconfigure(i, weight=1)

        geomf = ttk.LabelFrame(f, text="Geometry", padding=8)
        geomf.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self._labeled_entry(geomf, "Close distance (mm)", self.dist_close_var, 0)
        self._labeled_entry(geomf, "Far distance (mm)", self.dist_far_var, 1)
        self._labeled_entry(geomf, "Az center (deg)", self.az_center_var, 2)
        self._labeled_entry(geomf, "Az left (deg)", self.az_left_var, 3)
        self._labeled_entry(geomf, "Az right (deg)", self.az_right_var, 4)
        self._labeled_entry(geomf, "Downward angle (deg)", self.down_angle_var, 5)
        self._labeled_entry(geomf, "Head roll (deg)", self.head_roll_var, 6)
        ttk.Button(geomf, text="Recompute preview", command=self._recompute_geometry_preview).grid(row=7, column=0, sticky="ew", pady=(8,0))
        ttk.Button(geomf, text="Apply geometry", command=self.apply_geometry_settings).grid(row=7, column=1, sticky="ew", pady=(8,0))
        ttk.Label(geomf, textvariable=self.geometry_sync_var, foreground="#555").grid(row=8, column=0, columnspan=2, sticky="w")

        posf = ttk.LabelFrame(f, text="Enabled positions + preview", padding=8)
        posf.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        headers = ["On", "Name", "XYZ", "Dist", "Down"]
        for j, h in enumerate(headers):
            ttk.Label(posf, text=h).grid(row=0, column=j, sticky="w", padx=2)
        pos_names = [
            "0 close center",
            "1 close left",
            "2 close right",
            "3 far center",
            "4 far left",
            "5 far right",
        ]
        for i in range(6):
            ttk.Checkbutton(posf, variable=self.pos_enabled_vars[i]).grid(row=i+1, column=0, sticky="w")
            ttk.Label(posf, textvariable=tk.StringVar(value=pos_names[i])).grid(row=i+1, column=1, sticky="w")
            ttk.Label(posf, textvariable=self.position_preview_vars[i]["xyz"], width=32).grid(row=i+1, column=2, sticky="w")
            ttk.Label(posf, textvariable=self.position_preview_vars[i]["dist"], width=10).grid(row=i+1, column=3, sticky="w")
            ttk.Label(posf, textvariable=self.position_preview_vars[i]["down"], width=10).grid(row=i+1, column=4, sticky="w")
        ttk.Button(posf, text="Apply enabled positions", command=self.apply_enabled_positions).grid(row=7, column=0, columnspan=5, sticky="ew", pady=(8,0))

        adaptf = ttk.LabelFrame(f, text="Adaptive difficulty / free reward", padding=8)
        adaptf.grid(row=0, column=2, sticky="nsew", padx=4, pady=4)
        self.adapt_frame = adaptf
        ttk.Checkbutton(adaptf, text="Adaptive difficulty enabled", variable=self.adaptive_enabled_var).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(adaptf, text="Set independently by position", variable=self.adapt_use_per_position_var).grid(row=1, column=0, columnspan=3, sticky="w")

        self.adapt_all_frame = ttk.LabelFrame(adaptf, text="All positions", padding=6)
        self.adapt_all_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6,4))
        self._labeled_entry(self.adapt_all_frame, "Successful trials before increasing", self.adapt_hits_var, 0)
        self._labeled_entry(self.adapt_all_frame, "Failed trials before decreasing", self.adapt_misses_var, 1)
        self._labeled_entry(self.adapt_all_frame, "Increase step (mm)", self.adapt_step_var, 2)
        self._labeled_entry(self.adapt_all_frame, "Decrease step (mm)", self.adapt_step_down_var, 3)
        self._labeled_entry(self.adapt_all_frame, "Min distance (mm)", self.adapt_min_var, 4)
        self._labeled_entry(self.adapt_all_frame, "Max distance (mm)", self.adapt_max_var, 5)

        self.adapt_per_pos_frame = ttk.LabelFrame(adaptf, text="Per-position adaptive settings", padding=6)
        self.adapt_per_pos_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4,4))
        ttk.Label(self.adapt_per_pos_frame, text="Adapt on").grid(row=0, column=0, sticky="w")
        for i in range(6):
            ttk.Checkbutton(self.adapt_per_pos_frame, text=str(i), variable=self.adapt_pos_enabled_vars[i]).grid(row=0, column=i+1, sticky="w", padx=(0,4))
        ttk.Label(self.adapt_per_pos_frame, text="Edit position").grid(row=1, column=0, sticky="w", pady=(6,2))
        ttk.Combobox(self.adapt_per_pos_frame, textvariable=self.adapt_selected_pos_var, state="readonly", width=8, values=[str(i) for i in range(6)]).grid(row=1, column=1, sticky="w", pady=(6,2))
        ttk.Checkbutton(self.adapt_per_pos_frame, text="Selected position adapts", variable=self.adapt_edit_enabled_var).grid(row=1, column=2, columnspan=3, sticky="w", pady=(6,2))
        self._labeled_entry(self.adapt_per_pos_frame, "Successful trials before increasing", self.adapt_edit_hits_var, 2)
        self._labeled_entry(self.adapt_per_pos_frame, "Failed trials before decreasing", self.adapt_edit_misses_var, 3)
        self._labeled_entry(self.adapt_per_pos_frame, "Increase step (mm)", self.adapt_edit_step_var, 4)
        self._labeled_entry(self.adapt_per_pos_frame, "Decrease step (mm)", self.adapt_edit_step_down_var, 5)
        self._labeled_entry(self.adapt_per_pos_frame, "Min distance (mm)", self.adapt_edit_min_var, 6)
        self._labeled_entry(self.adapt_per_pos_frame, "Max distance (mm)", self.adapt_edit_max_var, 7)

        ttk.Button(adaptf, text="Apply adaptive difficulty", command=self.apply_adaptive_settings).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8,0))
        self._refresh_adaptive_ui_state()

    def _build_lick_tab(self):
        f = self.tab_lick
        for i in range(2):
            f.columnconfigure(i, weight=1)

        lick = ttk.LabelFrame(f, text="Lick debug / tuning", padding=8)
        lick.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self._labeled_entry(lick, "Threshold (ADC counts)", self.lick_thresh_var, 0)
        ttk.Label(lick, textvariable=self.lick_thresh_volts_var, width=12, foreground="#555555").grid(row=0, column=2, sticky="w", padx=(6,2), pady=2)
        self._labeled_entry(lick, "Hysteresis (ADC counts)", self.lick_hyst_var, 1)
        ttk.Label(lick, textvariable=self.lick_hyst_volts_var, width=12, foreground="#555555").grid(row=1, column=2, sticky="w", padx=(6,2), pady=2)
        self._labeled_entry(lick, "Polarity (1 or -1)", self.lick_polarity_var, 2)
        self._labeled_entry(lick, "Baseline alpha", self.lick_alpha_var, 3)
        self._labeled_entry(lick, "Refractory ms", self.lick_refract_var, 4)
        ttk.Checkbutton(lick, text="Lick debug streaming", variable=self.lick_debug_var).grid(row=5, column=0, columnspan=3, sticky="w")
        ttk.Button(lick, text="Apply lick settings", command=self.apply_lick_settings).grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8,0))

        self.refresh_lick_voltage_labels()

        notes = ttk.LabelFrame(f, text="Notes", padding=8)
        notes.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        txt = (
            "This GUI assumes your lick board baseline is high and a lick deflects downward toward 0 V.\n\n"
            "Suggested starting values:\n"
            "- polarity = -1\n"
            "- threshold = 500 ADC counts\n"
            "- hysteresis = 150 ADC counts\n"
            "- refractory = 20 ms\n\n"
            "Use STATUS and optional debug output to tune thresholds."
        )
        ttk.Label(notes, text=txt, justify="left").grid(row=0, column=0, sticky="nw")

    def _build_smc_tab(self):
        # Legacy placeholder; SMC02 content now lives in the unified Backend tab.
        return

    def _build_console_tab(self):
        f = self.tab_console
        f.rowconfigure(1, weight=1)
        f.columnconfigure(0, weight=1)

        cmdf = ttk.Frame(f)
        cmdf.grid(row=0, column=0, sticky="ew", pady=(0,8))
        cmdf.columnconfigure(0, weight=1)
        self.manual_cmd_var = tk.StringVar()
        self.manual_cmd_entry = ttk.Entry(cmdf, textvariable=self.manual_cmd_var)
        self.manual_cmd_entry.grid(row=0, column=0, sticky="ew", padx=(0,4))
        self.manual_cmd_entry.bind("<Return>", self.send_manual_command_event)
        ttk.Button(cmdf, text="Send", command=self.send_manual_command).grid(row=0, column=1)

        self.console = ScrolledText(f, wrap="word", height=30)
        self.console.grid(row=1, column=0, sticky="nsew")
        self.console.insert("end", "Console ready.\n")
        self.console.configure(state="disabled")

    # ---------------- widget helpers ----------------
    def _labeled_entry(self, parent, label, var, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=2, pady=2)
        ttk.Entry(parent, textvariable=var, width=14).grid(row=row, column=1, sticky="w", padx=2, pady=2)

    def _readonly_labeled(self, parent, label, var, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=2, pady=2)
        ttk.Label(parent, textvariable=var, width=18).grid(row=row, column=1, sticky="w", padx=2, pady=2)

    def _make_scrollable_container(self, parent):
        outer = ttk.Frame(parent)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        hsb = ttk.Scrollbar(outer, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        inner = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_scroll(_event=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
            except Exception:
                pass

        def _resize_inner(event):
            try:
                req_w = inner.winfo_reqwidth()
                canvas.itemconfigure(window_id, width=max(event.width, req_w))
            except Exception:
                pass

        def _scroll_vertical(units):
            try:
                canvas.yview_scroll(int(units), "units")
            except Exception:
                pass

        def _event_is_inside(event):
            widget = getattr(event, "widget", None)
            while widget is not None:
                if widget is outer:
                    return True
                widget = getattr(widget, "master", None)
            return False

        def _on_mousewheel(event):
            if not _event_is_inside(event):
                return None
            delta = getattr(event, "delta", 0)
            if delta == 0:
                return "break"
            step = -1 if delta > 0 else 1
            _scroll_vertical(step)
            return "break"

        def _on_button4(event):
            if not _event_is_inside(event):
                return None
            _scroll_vertical(-1)
            return "break"

        def _on_button5(event):
            if not _event_is_inside(event):
                return None
            _scroll_vertical(1)
            return "break"

        inner.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _resize_inner)
        canvas.bind_all("<MouseWheel>", _on_mousewheel, add="+")
        canvas.bind_all("<Button-4>", _on_button4, add="+")
        canvas.bind_all("<Button-5>", _on_button5, add="+")
        return outer, inner

    def _build_scrolled_labelframe(self, parent, text, padding=8):
        host, inner = self._make_scrollable_container(parent)
        lf = ttk.LabelFrame(inner, text=text, padding=padding)
        lf.grid(row=0, column=0, sticky="nsew")
        inner.columnconfigure(0, weight=1)
        inner.rowconfigure(0, weight=1)
        return host, lf

    def adc_counts_to_volts(self, counts):
        try:
            return float(counts) * 3.3 / 4095.0
        except Exception:
            return 0.0

    def refresh_lick_voltage_labels(self):
        self.lick_thresh_volts_var.set(f"≈ {self.adc_counts_to_volts(self.lick_thresh_var.get_float()):.3f} V")
        self.lick_hyst_volts_var.set(f"≈ {self.adc_counts_to_volts(self.lick_hyst_var.get_float()):.3f} V")

    # ---------------- serial / console ----------------
    def refresh_ports(self):
        ports = self.client.available_ports()
        self.port_combo["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])
        elif not ports:
            self.port_var.set("")


    def probe_selected_port(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror(APP_TITLE, "Choose a serial port first.")
            return
        baud = self.baud_var.get_int(DEFAULT_BAUD)
        if serial is None:
            messagebox.showerror(APP_TITLE, "pyserial is not installed. Install with: pip install pyserial")
            return
        try:
            # Opening a serial port often auto-resets Arduino-class boards.
            # Give the device time to reboot and announce itself.
            backend = self.backend_var.get() if hasattr(self, "backend_var") else ""
            ser = serial.Serial(port=port, baudrate=baud, timeout=0.3, write_timeout=0.5)
            lines = []
            try:
                time.sleep(2.0)

                # First, collect any startup/banner lines (e.g. INFO kind=ready ...)
                t0 = time.time()
                while time.time() - t0 < 0.8:
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line:
                        lines.append(line)
                        if line.startswith("OK ") or line.startswith("INFO ") or line.startswith("STAT "):
                            break

                # Then explicitly probe with PING and wait a bit longer for response.
                try:
                    ser.write(b"PING\n")
                    ser.flush()
                except Exception:
                    pass

                t1 = time.time()
                while time.time() - t1 < 2.0:
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line:
                        lines.append(line)
                        if line.startswith("OK ") or line.startswith("INFO ") or line.startswith("STAT "):
                            # Keep going briefly in case the next line carries backend info.
                            if "backend=" in line.lower():
                                break
            finally:
                try:
                    ser.close()
                except Exception:
                    pass

            if lines:
                first = lines[0]
                joined = " | ".join(lines[:5])
                self.status_line_var.set(f"Probe OK on {port}: {first[:120]}")
                self._log_local(f"[PROBE {port}] " + joined)

                low = " ".join(lines).lower()
                self._last_probe_port = port
                self._last_probe_time = time.time()
                if "protocol=2" in low:
                    self.protocol_version = 2
                if "backend=smc02" in low or "backend=teensy" in low:
                    self.backend_var.set("teensy_smc02")
                    self.protocol_version = 2
                elif "backend=mega" in low or "backend=zaber" in low:
                    self.backend_var.set("mega_zaber")
            else:
                self.status_line_var.set(
                    f"Probe opened {port}, no response. "
                    f"Check baud (115200), close Arduino Serial Monitor, and note that Mega boards may reset on connect."
                )
                self._log_local(f"[PROBE {port}] no response")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Probe failed:\n{e}")

    def connect_serial(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror(APP_TITLE, "Choose a serial port first.")
            return
        try:
            backend = self.backend_var.get() if hasattr(self, "backend_var") else ""
            self.client.connect(port, self.baud_var.get_int(DEFAULT_BAUD), backend=backend)
            if backend in ("teensy_smc02", "mega_zaber"):
                self.protocol_version = 2
            self._connect_waiting = True
            self._connect_attempt = 0
            try:
                if self._connect_after_id is not None:
                    self.after_cancel(self._connect_after_id)
            except Exception:
                pass
            self._connect_after_id = None
            self.status_line_var.set(f"Connected to {port}; waiting for device ready...")
            self._log_local(f"[GUI] Connected to {port}")
            self._reset_apply_tracking()
            recently_probed = (self._last_probe_port == port and (time.time() - self._last_probe_time) < 10.0)
            if recently_probed:
                # Trust a very recent successful probe on the same port instead of forcing a second fragile handshake.
                self._connect_waiting = False
                self.status_line_var.set(f"Connected to {port}; recent probe succeeded")
                self._log_local("[GUI] Recent probe succeeded on this port; skipping blocking handshake.")
                self.after(1200, lambda: self.send("GET kind=status"))
            else:
                # Arduino Mega often resets on serial-open. Retry handshake until any non-empty line arrives.
                self._connect_after_id = self.after(1800, self._post_connect_handshake)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Failed to connect:\n{e}")

    def _post_connect_handshake(self):
        if not self.client.is_connected() or not self._connect_waiting:
            return
        self._connect_attempt += 1
        try:
            self._log_local(f"[GUI] Post-connect handshake attempt {self._connect_attempt}...")
            # Keep handshake light; on the Mega path, any non-empty reply is enough.
            self.client.send("PING")
            self.after(180, lambda: self.client.send("GET kind=status"))
        except Exception as e:
            self._log_local(f"[GUI] Handshake failed: {e}")

        # Retry a few times instead of waiting forever on one shot.
        if self._connect_waiting:
            if self._connect_attempt < 8:
                self._connect_after_id = self.after(900, self._post_connect_handshake)
            else:
                self._connect_waiting = False
                self._connect_after_id = None
                port = self.client.connected_port or self.port_var.get().strip()
                self.status_line_var.set(
                    f"Connected to {port}; no ready reply seen yet. You can try commands or press reset once."
                )
                self._log_local("[GUI] Handshake timed out waiting for a reply.")

    def disconnect_serial(self):
        self._connect_waiting = False
        try:
            if self._connect_after_id is not None:
                self.after_cancel(self._connect_after_id)
        except Exception:
            pass
        self._connect_after_id = None
        self.client.disconnect()
        self._reset_apply_tracking()
        self.status_line_var.set("Disconnected")
        self._log_local("[GUI] Disconnected")

    def _ensure_connected_for_command(self):
        if self.client.is_connected():
            return True
        try:
            self.status_line_var.set("Not connected")
        except Exception:
            pass
        self._log_local("[GUI] Send aborted: not connected to device")
        try:
            messagebox.showerror(APP_TITLE, "Not connected to device.")
        except Exception:
            pass
        return False

    def send(self, cmd: str):
        if not self.client.is_connected():
            return self._ensure_connected_for_command()
        try:
            self.client.send(cmd)
            self._log_local(f"> {cmd}")
            if self.session_logger.active:
                self.session_logger.log_outbound_command(cmd, latest_status=self.latest_status if isinstance(self.latest_status, dict) else {})
            return True
        except Exception as e:
            try:
                messagebox.showerror(APP_TITLE, f"Send failed:\n{e}")
            except Exception:
                pass
            return False

    def send_manual_command(self):
        cmd = self.manual_cmd_var.get().strip()
        if cmd:
            self.send(cmd)
            self.manual_cmd_var.set("")

    def send_manual_command_event(self, event=None):
        self.send_manual_command()
        return "break"

    def _append_console(self, line: str):
        self.console.configure(state="normal")
        self.console.insert("end", line + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def _log_local(self, text: str):
        self._append_console(text)

    def _process_serial(self):
        try:
            while True:
                line = self.client.rx_queue.get_nowait()
                # Avoid flooding console with repeated auto-polled status lines while still parsing them.
                show_line = True
                if line.startswith("STAT kind=status") and self.autopoll_var.get():
                    try:
                        if getattr(self, "_last_status_console_line", "") == line:
                            show_line = False
                        self._last_status_console_line = line
                    except Exception:
                        pass
                if show_line:
                    self._append_console(line)
                if self.session_logger.active:
                    self.session_logger.log_raw(line)
                try:
                    if line and not line.startswith("[ERROR]"):
                        self._mark_device_ready(line)
                    self._parse_serial_line(line)
                except Exception as e:
                    self._append_console(f"[GUI ERROR parsing serial] {e} :: {line}")
        except queue.Empty:
            pass
        except Exception as e:
            self._append_console(f"[GUI ERROR serial loop] {e}")
        self.after(100, self._process_serial)

    def _poll_status_loop(self):
        now = time.time()
        try:
            interval_s = max(0.2, float(self.autopoll_interval_var.get()))
        except Exception:
            interval_s = 1.0
        if self.autopoll_var.get() and self.client.is_connected() and now - self._last_status_poll > interval_s:
            try:
                if self.protocol_version >= 2:
                    self.client.send("GET kind=status")
                    run_now = str(getattr(self, "latest_status", {}).get("run", "0")).strip().lower() in ("1", "true", "on")
                    if run_now:
                        self.client.send("GET kind=stats")
                else:
                    self.client.send("STATUS")
                self._last_status_poll = now
            except Exception:
                pass
        self.after(250, self._poll_status_loop)

    def _mark_device_ready(self, line: str):
        if not getattr(self, "_connect_waiting", False):
            return
        s = (line or "").strip()
        backend = self.backend_var.get() if hasattr(self, "backend_var") else ""
        # On some Mega/USB-serial opens, a brief line of garbage can arrive during reset.
        # Do not treat an unknown-command error with replacement characters as a successful ready handshake.
        if backend == "mega_zaber" and s.startswith("ERR cmd=unknown code=bad_cmd") and ("�" in s or "detail=" in s and s.endswith("detail=")):
            return
        if not (s.startswith("INFO ") or s.startswith("OK ") or s.startswith("STAT ")):
            return
        self._connect_waiting = False
        try:
            if self._connect_after_id is not None:
                self.after_cancel(self._connect_after_id)
        except Exception:
            pass
        self._connect_after_id = None
        port = self.client.connected_port or self.port_var.get().strip()
        self.status_line_var.set(f"Connected to {port}; device ready")
        self._log_local(f"[GUI] Device ready: {line}")

    def _parse_serial_line(self, line: str):
        def kv_from_tokens(tokens):
            data = {}
            for p in tokens:
                if "=" in p:
                    k, v = p.split("=", 1)
                    data[k] = v
            return data

        if line.startswith("INFO "):
            self._mark_device_ready(line)
            if "protocol=2" in line:
                self.protocol_version = 2
            if "backend=mega_zaber" in line or "backend=mega" in line:
                if hasattr(self, "backend_var"):
                    self.backend_var.set("mega_zaber")
                    self._refresh_backend_note()
            elif "backend=smc02" in line or "backend=teensy_smc02" in line or "backend=teensy" in line:
                if hasattr(self, "backend_var"):
                    self.backend_var.set("teensy_smc02")
                    self._refresh_backend_note()
            self.port_info_var.set(f"Device says: {line}")
            return

        if line.startswith("OK ") or line.startswith("ERR "):
            self._mark_device_ready(line)
            if line.startswith("OK "):
                data = kv_from_tokens(line.split()[1:])
                if data.get("cmd", "") == "set":
                    key = data.get("key", "")
                    val = data.get("value", "")
                    if key:
                        self.device_config_cache[key] = val
                        if self.session_logger.active:
                            self.session_logger.log_config_change(
                                change_type="set_ack",
                                direction="inbound",
                                source="device",
                                key=key,
                                value=val,
                                raw_line=line,
                                latest_status=self.latest_status if isinstance(self.latest_status, dict) else {},
                                acknowledged="1",
                            )
                            self.session_logger.update_device_config_snapshot({
                                "latest_status": dict(self.latest_status) if isinstance(self.latest_status, dict) else {},
                                "config_cache": dict(self.device_config_cache),
                            })
                    if key in ("task.rewards_held", "task.reward_hold"):
                        self._set_reward_hold_state(val in ("1", "true", "True", "on"), source="device")
            return

        if line.startswith("STAT kind=lick"):
            self._mark_device_ready(line)
            data = kv_from_tokens(line.split()[2:])
            raw = float(data.get("raw", data.get("lick_raw", 0)) or 0)
            baseline = float(data.get("baseline", data.get("lick_baseline", 0)) or 0)
            lick = int(float(data.get("lick", 0) or 0))
            try:
                thresh = float(self.lick_thresh_var.get())
            except Exception:
                thresh = 0.0
            pol = self.lick_polarity_var.get().strip()
            threshold_line = baseline - thresh if pol == "-1" else baseline + thresh
            self.lick_samples.append((time.time(), raw, baseline, threshold_line, lick))
            if lick and not self._last_lick_state:
                self._record_timeline_event("lick", data)
            self._last_lick_state = lick
            return

        if line.startswith("STAT kind=status"):
            self._mark_device_ready(line)
            data = kv_from_tokens(line.split()[2:])
            prev_status = dict(self.latest_status) if isinstance(self.latest_status, dict) else {}
            self.latest_status = data
            self._log_hold_state_transitions(prev_status, data, raw_line=line)
            held_now = str(data.get("rewards_held", data.get("task_rewards_held", "0"))).strip().lower() in ("1", "true", "on")
            self._set_reward_hold_state(held_now, source="device")
            try:
                self._lick_state_current = int(float(data.get("lick", self._lick_state_current) or 0))
            except Exception:
                pass
            if "x_mm" in data:
                self.curr_x_var.set(data["x_mm"])
            if "y_mm" in data:
                self.curr_y_var.set(data["y_mm"])
            if "z_mm" in data:
                self.curr_z_var.set(data["z_mm"])
            pos_idx = data.get("current_pos", "-1")
            try:
                if str(pos_idx).lstrip("-").isdigit():
                    i = int(pos_idx)
                    self.current_active_pos_idx = i if 0 <= i < 6 else None
            except Exception:
                pass
            labels = self.position_labels if hasattr(self, "position_labels") else [f"Pos {i}" for i in range(6)]
            pos_label = labels[int(pos_idx)] if pos_idx.lstrip('-').isdigit() and 0 <= int(pos_idx) < len(labels) else "--"
            self.current_pos_label_var.set(f"Pos: {pos_idx} {pos_label}  Dist: {data.get('current_pos_dist_mm','--')} mm")
            block_size_disp = data.get('current_block_size', data.get('block_size', '--'))
            self.block_summary_var.set(
                f"Block: pos={data.get('block_pos','--')}  trial={data.get('block_trial','--')}/{block_size_disp}  block#={data.get('block_number','--')}"
            )
            self._refresh_reward_summary(data)
            block_min_disp = data.get('block_size_min', self.block_size_min_var.get())
            block_max_disp = data.get('block_size_max', self.block_size_max_var.get())
            self.scheduler_summary_var.set(
                f"Schedule: block size min/max={block_min_disp}/{block_max_disp}  current block size={block_size_disp}"
            )
            remaining_text = '--'
            target_enabled = str(data.get('target_trials_per_position_enabled', '0')).strip().lower() in ('1', 'true', 'on')
            if target_enabled:
                try:
                    target_total = int(float(data.get('target_trials_per_position', self.target_trials_per_pos_var.get()) or 0))
                except Exception:
                    target_total = 0
                try:
                    pos_i = int(pos_idx)
                except Exception:
                    pos_i = -1
                pos_trials = None
                raw_pos_trials = data.get('current_pos_trials', None)
                if raw_pos_trials not in (None, ''):
                    try:
                        pos_trials = int(float(raw_pos_trials))
                    except Exception:
                        pos_trials = None
                if pos_trials is None and 0 <= pos_i < len(getattr(self, 'position_labels', [])):
                    pos_stat = getattr(self, 'position_stats', {}).get(pos_i, {}) if hasattr(self, 'position_stats') else {}
                    raw_trials = pos_stat.get('trials', None) if isinstance(pos_stat, dict) else None
                    if raw_trials not in (None, ''):
                        try:
                            pos_trials = int(float(raw_trials))
                        except Exception:
                            pos_trials = None
                raw_remaining = data.get('current_pos_target_remaining', None)
                pos_remaining = None
                if raw_remaining not in (None, ''):
                    try:
                        pos_remaining = int(float(raw_remaining))
                    except Exception:
                        pos_remaining = None
                elif pos_trials is not None and target_total >= 0:
                    pos_remaining = max(0, target_total - pos_trials)
                if pos_remaining is not None and pos_i >= 0:
                    remaining_text = f"{pos_remaining} (target={target_total}, pos_trials={pos_trials if pos_trials is not None else '--'})"
                elif target_total > 0:
                    remaining_text = f"target={target_total}"
            self.remaining_summary_var.set(f"Target remaining for current pos: {remaining_text}")
            run_str = str(data.get("run", "0")).strip().lower()
            run_now = run_str in ("1", "true", "on")
            if run_now and not self._last_status_run:
                start_wall = self._start_requested_wall_unix if self._start_requested_wall_unix is not None else time.time()
                self._task_start_wall_unix = start_wall
                if self._session_timeline_start_unix is None:
                    self._session_timeline_start_unix = start_wall
                self._session_timeline_end_unix = None
                self._awaiting_run_confirmation = False
                self._start_requested_wall_unix = None
            elif not run_now:
                if self._last_status_run:
                    self._task_start_wall_unix = None
                    self._session_timeline_end_unix = time.time()
                    if self.session_logger.active:
                        self.after(0, self.stop_session_logging)
                elif self._awaiting_run_confirmation:
                    # Ignore stale idle statuses immediately after START.
                    # Only a confirmed run=1 transition should begin or end an auto-started log.
                    pass
            self._last_status_run = run_now
            elapsed_text = "--"
            if run_now and self._task_start_wall_unix is not None:
                elapsed_text = f"{(time.time() - self._task_start_wall_unix) / 60.0:.2f}"
            at_dock = getattr(self, "current_at_dock", False)
            visual_pos = "dock" if at_dock else (f"{pos_idx} {pos_label}" if pos_label != "--" else "--")
            dist_text = data.get('current_pos_dist_mm', '--')
            if at_dock:
                self.visual_pos_var.set(f"Current spout pos: {visual_pos}")
            else:
                try:
                    adapt_enabled = bool(self.adaptive_enabled_var.get())
                except Exception:
                    adapt_enabled = False
                if adapt_enabled and dist_text not in ('', None, '--'):
                    self.visual_pos_var.set(f"Current spout pos: {visual_pos}  |  Current adapted dist: {dist_text} mm")
                elif dist_text not in ('', None, '--'):
                    self.visual_pos_var.set(f"Current spout pos: {visual_pos}  |  Dist: {dist_text} mm")
                else:
                    self.visual_pos_var.set(f"Current spout pos: {visual_pos}")
            self.visual_block_var.set(f"Current block #: {data.get('block_number','--')}")
            self.visual_trial_var.set(
                f"Current trial #: {data.get('total_trials','--')}  |  Block trial: {data.get('block_trial','--')}/{block_size_disp}"
            )
            self.visual_elapsed_var.set(f"Time since task start: {elapsed_text} min")
            self.status_line_var.set(
                f"Run={data.get('run','?')}  State={data.get('state','?')}  Mode={data.get('reward_mode','?')}  "
                f"Trials={data.get('total_trials','?')}  Hits={data.get('hits','?')}  Misses={data.get('misses','?')}  "
                f"Water_uL={data.get('water_ul','?')}/{data.get('water_limit_ul','?')}  Sync={data.get('sync_count','?')}"
            )
            if self.session_logger.active:
                self.session_logger.update_latest_status(data)
            return

        if line.startswith("STAT kind=summary"):
            self._mark_device_ready(line)
            self.summary_stats = kv_from_tokens(line.split()[2:])
            return

        if line.startswith("STAT kind=pos"):
            self._mark_device_ready(line)
            data = kv_from_tokens(line.split()[2:])
            idx = data.get("idx", "")
            if idx.isdigit() and hasattr(self, 'pos_stats_tree'):
                self.position_stats[int(idx)] = data
                self._refresh_position_stats_tree()
            return

        if line.startswith("CFG "):
            self._mark_device_ready(line)
            data = kv_from_tokens(line.split()[1:])
            key = data.get("key", "")
            val = data.get("value", "")
            if key:
                self.device_config_cache[key] = val
                if self.session_logger.active:
                    self.session_logger.log_config_change(
                        change_type="cfg",
                        direction="inbound",
                        source="device",
                        key=key,
                        value=val,
                        raw_line=line,
                        latest_status=self.latest_status if isinstance(self.latest_status, dict) else {},
                    )
            if key == "task.reward_mode":
                self.reward_mode_var.set(val)
            elif key == "task.auto_reward_delay_ms":
                self.auto_reward_delay_var.set(val)
            elif key == "task.auto_hold_after_miss_enabled":
                self.auto_hold_after_miss_enabled_var.set(val in ("1", "true", "True", "on"))
            elif key == "task.auto_hold_after_miss_threshold":
                self.auto_hold_after_miss_threshold_var.set(val)
            elif key in ("task.rewards_held", "task.reward_hold"):
                self._set_reward_hold_state(val in ("1", "true", "True", "on"), source="device")
            elif key == "task.enforce_no_lick":
                self.enforce_var.set(val in ("1", "true", "True", "on"))
            elif key == "task.reward_ms":
                self.reward_ms_var.set(val)
            elif key == "task.reward_ul":
                self.reward_ul_var.set(val)
            elif key == "task.water_limit_ul":
                self.water_limit_ul_var.set(val)
            elif key == "task.settle_ms":
                self.settle_ms_var.set(val)
            elif key == "task.post_reward_hold_ms":
                self.posthold_ms_var.set(val)
            elif key == "task.pre_cue_min_ms":
                self.precue_min_var.set(val)
            elif key == "task.pre_cue_max_ms":
                self.precue_max_var.set(val)
            elif key == "task.response_window_ms":
                self.response_window_var.set(val)
            elif key == "task.iti_min_ms":
                self.iti_min_var.set(val)
            elif key == "task.iti_jitter_ms":
                self.iti_jitter_var.set(val)
            elif key == "task.block_size":
                self.block_size_var.set(val)
            elif key == "task.block_size_min":
                self.block_size_min_var.set(val)
            elif key == "task.block_size_max":
                self.block_size_max_var.set(val)
            elif key == "task.target_trials_per_position_enabled":
                self.target_trials_enabled_var.set(val in ("1", "true", "True", "on"))
            elif key == "task.target_trials_per_position":
                self.target_trials_per_pos_var.set(val)
            elif key == "task.max_duration_enabled":
                self.max_duration_enabled_var.set(val in ("1", "true", "True", "on"))
            elif key == "task.max_duration_min":
                self.max_duration_min_var.set(val)
            elif key == "task.scheduling_mode":
                self.scheduling_mode_var.set(val)
            elif key == "task.stop_mode":
                self.stop_mode_var.set(val)
            elif key == "cue.frequency_hz":
                self.cue_hz_var.set(val)
            elif key == "cue.duration_ms":
                self.cue_duration_var.set(val)
            elif key == "cue.volume_pct":
                self.cue_volume_var.set(val)
            elif key == "lick.debug":
                self.lick_debug_var.set(val in ("1", "true", "True", "on"))
            elif key == "motion.mouth_origin.x_mm":
                self.mouth_x_var.set(val)
            elif key == "motion.mouth_origin.y_mm":
                self.mouth_y_var.set(val)
            elif key == "motion.mouth_origin.z_mm":
                self.mouth_z_var.set(val)
            elif key == "motion.dock.x_mm":
                self.dock_x_var.set(val)
            elif key == "motion.dock.y_mm":
                self.dock_y_var.set(val)
            elif key == "motion.dock.z_mm":
                self.dock_z_var.set(val)
            elif key == "adapt.enabled":
                self.adaptive_enabled_var.set(val in ("1", "true", "True", "on"))
            elif key == "adapt.use_per_position":
                self.adapt_use_per_position_var.set(val in ("1", "true", "True", "on"))
            elif key == "adapt.hits_to_advance":
                self.adapt_hits_var.set(val)
            elif key == "adapt.misses_to_decrease":
                self.adapt_misses_var.set(val)
            elif key == "adapt.step_mm":
                self.adapt_step_var.set(val)
            elif key == "adapt.decrease_step_mm":
                self.adapt_step_down_var.set(val)
            elif key == "adapt.min_distance_mm":
                self.adapt_min_var.set(val)
            elif key == "adapt.max_distance_mm":
                self.adapt_max_var.set(val)
            elif key.startswith("adapt.pos"):
                try:
                    rest = key[len("adapt.pos"):]
                    pos_str, subkey = rest.split(".", 1)
                    pos_idx = int(pos_str)
                except Exception:
                    pos_idx = -1
                    subkey = ""
                if 0 <= pos_idx < 6:
                    if subkey == "enabled":
                        self.adapt_pos_enabled_vars[pos_idx].set(val in ("1", "true", "True", "on"))
                    elif subkey == "hits_to_advance":
                        self.adapt_pos_hits_vars[pos_idx].set(val)
                    elif subkey == "misses_to_decrease":
                        self.adapt_pos_misses_vars[pos_idx].set(val)
                    elif subkey == "step_mm":
                        self.adapt_pos_step_vars[pos_idx].set(val)
                    elif subkey == "decrease_step_mm":
                        self.adapt_pos_step_down_vars[pos_idx].set(val)
                    elif subkey == "min_distance_mm":
                        self.adapt_pos_min_vars[pos_idx].set(val)
                    elif subkey == "max_distance_mm":
                        self.adapt_pos_max_vars[pos_idx].set(val)
                    if pos_idx == self._adaptive_selected_pos_index():
                        self._load_selected_adaptive_position_into_editor()
            if self.session_logger.active:
                self.session_logger.update_device_config_snapshot({"latest_status": dict(self.latest_status) if isinstance(self.latest_status, dict) else {}, "config_cache": dict(self.device_config_cache)})
            return

        if line.startswith("POS "):
            self._mark_device_ready(line)
            data = kv_from_tokens(line.split()[1:])
            idx = data.get("idx", "")
            if idx.isdigit():
                i = int(idx)
                try:
                    self.position_preview_vars[i]["xyz"].set(f"({float(data.get('x_mm',0)):.3f}, {float(data.get('y_mm',0)):.3f}, {float(data.get('z_mm',0)):.3f})")
                    self.position_preview_vars[i]["dist"].set(f"{float(data.get('dist_mm',0)):.3f} mm")
                    self.position_preview_vars[i]["down"].set(f"{float(data.get('down_deg',0)):.2f}°")
                    self.position_preview_vars[i]["label"].set((self.position_labels[i] if hasattr(self, "position_labels") and i < len(self.position_labels) else f"Pos {i}"))
                    self.pos_enabled_vars[i].set(data.get('enabled','1') in ('1','true','True','on'))
                except Exception:
                    pass
                if self.session_logger.active:
                    self.session_logger.update_positions_snapshot(self._current_positions_snapshot())
            return

        if line.startswith("EVT "):
            self._mark_device_ready(line)
            data = kv_from_tokens(line.split()[1:])
            name = data.get("name", "")
            event_now = time.time()
            latest_status = self.latest_status if isinstance(self.latest_status, dict) else {}
            if name == "sync":
                try:
                    ttl_ms = float(data.get("ttl_ms", 0) or 0)
                except Exception:
                    ttl_ms = 0.0
                self._sync_active_until = max(self._sync_active_until, event_now + ttl_ms/1000.0)
            elif name in ("lick", "lick_on"):
                self._lick_state_current = 1
            elif name == "lick_off":
                self._lick_state_current = 0
            elif name == "manual_reward_hold_on":
                latest_status["manual_reward_hold_active"] = "1"
                latest_status["rewards_held"] = "1"
                self._set_reward_hold_state(True, source="device")
                self._refresh_reward_summary(latest_status)
            elif name == "manual_reward_hold_off":
                latest_status["manual_reward_hold_active"] = "0"
                auto_held = self._is_truthy_flag(latest_status.get("auto_reward_hold_active", "0"))
                latest_status["rewards_held"] = "1" if auto_held else "0"
                self._set_reward_hold_state(auto_held, source="device")
                self._refresh_reward_summary(latest_status)
            elif name == "auto_reward_hold_on":
                latest_status["auto_reward_hold_active"] = "1"
                latest_status["rewards_held"] = "1"
                self._set_reward_hold_state(True, source="device")
                self._refresh_reward_summary(latest_status)
            elif name == "auto_reward_hold_off":
                latest_status["auto_reward_hold_active"] = "0"
                manual_held = self._is_truthy_flag(latest_status.get("manual_reward_hold_active", "0"))
                latest_status["rewards_held"] = "1" if manual_held else "0"
                self._set_reward_hold_state(manual_held, source="device")
                self._refresh_reward_summary(latest_status)

            # Track active/target position from either pos= or idx= so diagrams follow the real task.
            pos_token = data.get("pos", data.get("idx", ""))
            if pos_token in ("", None):
                status_pos = latest_status.get("current_pos", "")
                if str(status_pos).lstrip("-").isdigit():
                    pos_token = status_pos
                    data["pos"] = str(status_pos)
            try:
                if str(pos_token).lstrip("-").isdigit():
                    pidx = int(pos_token)
                    if 0 <= pidx < 6:
                        self.current_active_pos_idx = pidx
                        self.current_at_dock = False
                if name == "dock":
                    self.current_active_pos_idx = None
                    self.current_at_dock = True
                elif name == "position":
                    self.current_at_dock = False
            except Exception:
                pass

            if name == "free_reward":
                try:
                    free_pos = int(pos_token) if str(pos_token).lstrip("-").isdigit() else None
                except Exception:
                    free_pos = None
                self._pending_free_reward_marker = {"ts": event_now, "pos": free_pos}
            elif name in ("adapt_advance", "adapt_decrease"):
                try:
                    pending = getattr(self, "_adapt_refresh_after_id", None)
                    if pending:
                        self.after_cancel(pending)
                except Exception:
                    pass
                self._adapt_refresh_after_id = self.after(120, lambda: self.send("GET kind=positions"))
            elif name == "reward":
                marker = getattr(self, "_pending_free_reward_marker", None)
                try:
                    reward_pos = int(pos_token) if str(pos_token).lstrip("-").isdigit() else None
                except Exception:
                    reward_pos = None
                if marker and (event_now - marker.get("ts", 0.0) <= 1.0):
                    marker_pos = marker.get("pos", None)
                    if marker_pos is None or reward_pos is None or marker_pos == reward_pos:
                        data["_skip_reward_dot"] = "1"
                    self._pending_free_reward_marker = None
            elif name not in ("free_reward_trial",):
                marker = getattr(self, "_pending_free_reward_marker", None)
                if marker and (event_now - marker.get("ts", 0.0) > 1.0):
                    self._pending_free_reward_marker = None

            trial_id_explicit_for_logger = (
                "trial" in data and self.session_logger._normalize_trial_id(data.get("trial", "")) != ""
            )
            if "trial" not in data and latest_status.get("total_trials", "") not in ("", None):
                data["trial"] = str(latest_status.get("total_trials", ""))
            data["_trial_id_explicit_for_logger"] = "1" if trial_id_explicit_for_logger else "0"
            if "state" not in data and latest_status.get("state", "") not in ("", None):
                data["state"] = str(latest_status.get("state", ""))
            if "reward_mode" not in data and latest_status.get("reward_mode", "") not in ("", None):
                data["reward_mode"] = str(latest_status.get("reward_mode", ""))
            data["event_source"] = self._infer_event_source_for_raster(name, data)
            data["reward_type"] = self._infer_reward_type_for_raster(name, data)

            pos_text = data.get('pos', data.get('idx', '?'))
            self.last_event_var.set(f"Last event: {name} @ t_ms={data.get('t_ms','?')} pos={pos_text}")
            self._record_timeline_event(name, data)
            if self.session_logger.active:
                event_context = {
                    "latest_status": dict(latest_status) if isinstance(latest_status, dict) else {},
                    "x_mm": self.curr_x_var.get(),
                    "y_mm": self.curr_y_var.get(),
                    "z_mm": self.curr_z_var.get(),
                    "lick_state": self._lick_state_current,
                }
                self.session_logger.log_event(data, self._current_positions_snapshot(), line, event_context)
            return

        if line.startswith("STATUS "):
            # legacy sketch compatibility
            data = kv_from_tokens(line.split()[1:])
            self.latest_status = data
            return

    def _is_truthy_flag(self, value):
        return str(value).strip().lower() in ("1", "true", "on", "yes")

    def _log_hold_state_transitions(self, prev_status: dict, new_status: dict, raw_line: str = ""):
        if not self.session_logger.active:
            return
        if not isinstance(prev_status, dict) or not isinstance(new_status, dict):
            return
        fields = (
            ("rewards_held", "task.rewards_held"),
            ("manual_reward_hold_active", "task.manual_reward_hold_active"),
            ("auto_reward_hold_active", "task.auto_reward_hold_active"),
        )
        for src_key, log_key in fields:
            prev_raw = prev_status.get(src_key, None)
            new_raw = new_status.get(src_key, None)
            if prev_raw is None or new_raw is None:
                continue
            prev_val = self._is_truthy_flag(prev_raw)
            new_val = self._is_truthy_flag(new_raw)
            if prev_val == new_val:
                continue
            self.session_logger.log_config_change(
                change_type="hold_transition",
                direction="inbound",
                source="status_poll",
                key=log_key,
                value="1" if new_val else "0",
                raw_line=raw_line,
                latest_status=new_status,
            )

    def _normalize_raster_trial_id(self, value):
        s = str(value).strip()
        if not s or s in ("?", "None"):
            return ""
        return s

    def _infer_event_source_for_raster(self, name: str, data: dict):
        source = str(data.get("event_source", "") or "").strip()
        if source:
            return source
        if name in ("cue_only", "manual_reward", "manual_reference_set"):
            return "manual"
        if name == "reward_cal_pulse":
            return "calibration"
        if name == "sync":
            return "sync"
        if name.startswith("button_"):
            return "button"
        return "task"

    def _infer_reward_type_for_raster(self, name: str, data: dict):
        reward_type = str(data.get("reward_type", "") or "").strip().lower()
        if reward_type:
            return reward_type
        if name in ("free_reward_trial", "free_reward"):
            return "free"
        if name == "manual_reward":
            return "manual"
        if name == "reward_cal_pulse":
            return "calibration"
        if name == "hit":
            return "contingent"
        if name == "reward":
            if self._is_truthy_flag(data.get("free_reward_delivered", "")):
                return "free"
            if self._infer_event_source_for_raster(name, data) == "manual":
                return "manual"
            reward_mode = str(data.get("reward_mode", "") or "").strip().lower()
            if reward_mode == "contingent":
                return "contingent"
        return ""

    def _raster_reward_marker_kind(self, name: str, data: dict):
        if name == "reward" and self._is_truthy_flag(data.get("_skip_reward_dot", "0")):
            return ""
        reward_type = self._infer_reward_type_for_raster(name, data)
        if reward_type in ("free", "manual"):
            return "free"
        if reward_type == "contingent":
            return "earned"
        if name in ("free_reward", "manual_reward"):
            return "free"
        if name == "reward":
            reward_mode = str(data.get("reward_mode", "") or "").strip().lower()
            if reward_mode == "auto_after_delay":
                return "free"
            return "earned"
        return ""


    def _record_timeline_event(self, name: str, data=None):
        ts = time.time()
        payload = data or {}
        self.timeline_events.append((ts, name, payload))
        if self._session_timeline_start_unix is None:
            self._session_timeline_start_unix = ts
        self.session_timeline_events.append((ts, name, payload))

    def _clear_session_visual_history(self):
        self.timeline_events.clear()
        self.session_timeline_events.clear()
        self._session_timeline_start_unix = None
        self._session_timeline_end_unix = None

    def _timeseries_log_loop(self):
        try:
            self._timeseries_sample_ms = max(1, self.timeseries_interval_var.get_int(self._timeseries_sample_ms or 20))
            if self.session_logger.active and self.timeseries_enabled_var.get():
                now = time.time()
                sync_state = 1 if now < getattr(self, "_sync_active_until", 0.0) else 0
                pos_idx = self.latest_status.get("current_pos", "") if isinstance(self.latest_status, dict) else ""
                pos_name = ""
                try:
                    if str(pos_idx).lstrip("-").isdigit():
                        i = int(pos_idx)
                        if 0 <= i < len(self.position_labels):
                            pos_name = self.position_labels[i]
                except Exception:
                    pass
                row = {
                    "gui_timestamp_iso": datetime.now().isoformat(),
                    "gui_timestamp_unix": f"{now:.6f}",
                    "sample_interval_ms": self._timeseries_sample_ms,
                    "run": self.latest_status.get("run", "") if isinstance(self.latest_status, dict) else "",
                    "state": self.latest_status.get("state", "") if isinstance(self.latest_status, dict) else "",
                    "current_pos": pos_idx,
                    "pos_name": pos_name,
                    "x_mm": self.curr_x_var.get(),
                    "y_mm": self.curr_y_var.get(),
                    "z_mm": self.curr_z_var.get(),
                    "sync_state": sync_state,
                    "lick_state": self._lick_state_current,
                }
                self.session_logger.log_timeseries(row)
        except Exception:
            pass
        self.after(self._timeseries_sample_ms, self._timeseries_log_loop)

    def _redraw_visuals_loop(self):
        try:
            if self.winfo_exists():
                self._draw_task_raster()
                self._draw_cumulative_task_raster()
                self._draw_lick_trace()
                self._draw_position_diagrams()
        except Exception:
            pass
        self.after(120, self._redraw_visuals_loop)

    def _draw_raster_on_canvas(self, canvas, events, *, window_s, title_text, recent_mode):
        if not canvas:
            return
        c = canvas
        c.delete('all')
        try:
            w = c.winfo_width()
            h = c.winfo_height()
        except Exception:
            w, h = 700, 300
        if w <= 2:
            try:
                w = int(float(c.cget('width')))
            except Exception:
                w = 700
        if h <= 2:
            try:
                h = int(float(c.cget('height')))
            except Exception:
                h = 300
        w = max(w, 360)
        h = max(h, 120)
        left = 120
        top = 10
        bottom_margin = 18
        labels = self.position_labels if hasattr(self, 'position_labels') and len(self.position_labels) >= 6 else [f'Pos {i}' for i in range(6)]
        rows = [('sync','Sync'), ('cue','Cue'), ('lick','Lick')] + [(f'pos{i}', labels[i]) for i in range(6)]
        aliases = {'manual_cue': 'cue', 'lick_on': 'lick'}
        dot_colors = {
            'earned':'#2e7d32',
            'free':'#00897b',
            'pre_cue_reset_by_lick':'#d32f2f',
            'miss':'#d32f2f',
        }
        line_event_styles = {
            'trial_start': {'fill': '#9e9e9e', 'dash': (2, 2), 'width': 1},
            'dock_start': {'fill': '#9e9e9e', 'dash': (2, 2), 'width': 1},
            'dock': {'fill': '#1565c0', 'dash': (2, 2), 'width': 1},
            'position': {'fill': '#ef6c00', 'dash': (2, 2), 'width': 1},
        }
        event_row_h = max(8.0, (h - top - bottom_margin) / len(rows))
        usable_w = max(40.0, w - left - 10)
        now = time.time()
        label_font = ('TkDefaultFont', max(7, min(10, int(event_row_h * 0.6))))
        legend_font = ('TkDefaultFont', max(7, min(9, int(event_row_h * 0.5))))
        event_radius = max(1.0, min(4.0, usable_w / max(120.0, window_s * 2.5)))
        pos_ring_radius = max(1.5, min(4.5, event_radius + 0.5))
        c.create_rectangle(0, 0, w, h, fill='white', outline='')
        for i,(_,label) in enumerate(rows):
            y = top + i*event_row_h + event_row_h/2
            c.create_text(6, y, text=label, anchor='w', fill='#333333', font=label_font)
            c.create_line(left, y, w-4, y, fill='#e7e7e7')
        c.create_line(left, top, left, h-bottom_margin, fill='#999999')

        if recent_mode:
            for sec in range(0, int(window_s) + 1, 5):
                x = left + usable_w * (1 - sec/window_s)
                c.create_line(x, top, x, h-bottom_margin, fill='#f0f0f0')
                if sec < window_s:
                    c.create_text(x, h-2, text=f'-{sec}s', anchor='s', fill='#666666', font=legend_font)
        else:
            tick_count = 6
            for i in range(tick_count + 1):
                frac = i / tick_count
                x = left + usable_w * frac
                c.create_line(x, top, x, h-bottom_margin, fill='#f0f0f0')
                sec = window_s * frac
                if sec >= 60:
                    label = f'{sec/60.0:.0f}m'
                else:
                    label = f'{sec:.0f}s'
                c.create_text(x, h-2, text=label, anchor='s', fill='#666666', font=legend_font)

        row_lookup = {name: idx for idx,(name,_) in enumerate(rows)}
        for ts, name, data in list(events):
            if recent_mode:
                age = now - ts
                if age > window_s:
                    continue
                frac = 1.0 - age/window_s
            else:
                start_ts = self._session_timeline_start_unix if self._session_timeline_start_unix is not None else ts
                frac = 0.0 if window_s <= 0 else max(0.0, min(1.0, (ts - start_ts) / window_s))
            x = left + usable_w * frac
            key = aliases.get(name, name)

            if key == 'sync':
                row = row_lookup['sync']
                y = top + row*event_row_h + event_row_h/2
                c.create_line(x, y-event_row_h*0.3, x, y+event_row_h*0.3, fill='#9e9e9e', width=1)
                continue
            if key == 'cue':
                row = row_lookup['cue']
                y = top + row*event_row_h + event_row_h/2
                c.create_line(x, y-event_row_h*0.3, x, y+event_row_h*0.3, fill='#1565c0', width=2 if recent_mode else 1)
                continue
            if key == 'lick':
                row = row_lookup['lick']
                y = top + row*event_row_h + event_row_h/2
                c.create_line(x, y-event_row_h*0.3, x, y+event_row_h*0.3, fill='#111111', width=1)
                continue

            pos = data.get('pos', data.get('idx', '')) if data else ''
            try:
                pos_idx = int(pos)
            except Exception:
                pos_idx = getattr(self, 'current_active_pos_idx', None)
            if pos_idx is None or pos_idx < 0 or pos_idx > 5:
                if key == 'dock':
                    sty = line_event_styles['dock']
                    c.create_line(x, top, x, h-bottom_margin, fill=sty['fill'], dash=sty['dash'], width=sty['width'])
                continue
            row = row_lookup[f'pos{pos_idx}']
            y = top + row*event_row_h + event_row_h/2
            if key in line_event_styles:
                sty = line_event_styles[key]
                c.create_line(x, top, x, h-bottom_margin, fill=sty['fill'], dash=sty['dash'], width=sty['width'])
                if key == 'position':
                    c.create_oval(x-pos_ring_radius, y-pos_ring_radius, x+pos_ring_radius, y+pos_ring_radius, outline=sty['fill'], width=1 if not recent_mode else 2)
                continue
            if key == 'miss':
                color = dot_colors['miss']
                c.create_oval(x-event_radius, y-event_radius, x+event_radius, y+event_radius, fill=color, outline='')
                continue
            if key == 'pre_cue_reset_by_lick':
                color = dot_colors['pre_cue_reset_by_lick']
                c.create_oval(x-event_radius, y-event_radius, x+event_radius, y+event_radius, fill=color, outline='')
                continue
            if key in ('reward', 'free_reward', 'manual_reward'):
                marker_kind = self._raster_reward_marker_kind(key, data or {})
                if not marker_kind:
                    continue
                color = dot_colors[marker_kind]
                if marker_kind == 'free':
                    c.create_oval(x-event_radius, y-event_radius, x+event_radius, y+event_radius, fill='', outline=color, width=1 if not recent_mode else 2)
                else:
                    c.create_oval(x-event_radius, y-event_radius, x+event_radius, y+event_radius, fill=color, outline='')
        c.create_text(w-6, 4, text=title_text, anchor='ne', fill='#666666', font=legend_font)

    def _draw_task_raster(self):
        if not hasattr(self, 'timeline_canvas'):
            return
        self._draw_raster_on_canvas(
            self.timeline_canvas,
            self.timeline_events,
            window_s=20.0,
            title_text='20 s raster | gray dotted = move start to target or dock | orange dotted = target arrival | blue dotted = dock arrival | earned reward=green dot | free/auto/manual reward=teal ring | miss=red dot',
            recent_mode=True,
        )

    def _build_cumulative_trial_markers(self):
        markers = []
        labels = self.position_labels if hasattr(self, 'position_labels') and len(self.position_labels) >= 6 else [f'Pos {i}' for i in range(6)]
        trials = {}
        active_key = None
        seq_counter = 0
        end_ts = self._session_timeline_end_unix if self._session_timeline_end_unix is not None else None

        def ensure_trial(trial_key, ts, data):
            nonlocal trials
            trial = trials.get(trial_key)
            if trial is None:
                pos_idx = None
                pos_token = data.get('pos', data.get('idx', '')) if data else ''
                if str(pos_token).lstrip('-').isdigit():
                    pos_idx = int(pos_token)
                trial = {
                    "trial_id": trial_key,
                    "ts": ts,
                    "pos_idx": pos_idx,
                    "hit_ts": None,
                    "miss_ts": None,
                    "free_ts": None,
                    "free_pos_idx": None,
                    "hit_pos_idx": None,
                    "miss_pos_idx": None,
                }
                trials[trial_key] = trial
            return trial

        for ts, name, data in list(self.session_timeline_events):
            if end_ts is not None and ts > end_ts:
                continue
            data = data or {}
            trial_key = self._normalize_raster_trial_id(data.get("trial", ""))
            if not trial_key and name == "trial_start":
                seq_counter += 1
                trial_key = f"seq:{seq_counter}"
            if trial_key:
                active_key = trial_key
            elif active_key:
                trial_key = active_key
            else:
                continue

            trial = ensure_trial(trial_key, ts, data)
            pos_token = data.get('pos', data.get('idx', ''))
            if str(pos_token).lstrip('-').isdigit():
                pos_idx = int(pos_token)
                if 0 <= pos_idx < len(labels):
                    trial["pos_idx"] = pos_idx
            marker_kind = self._raster_reward_marker_kind(name, data)
            if marker_kind == "free" and trial["free_ts"] is None:
                trial["free_ts"] = ts
                trial["free_pos_idx"] = trial["pos_idx"]
            if name == "hit" and trial["hit_ts"] is None:
                trial["hit_ts"] = ts
                trial["hit_pos_idx"] = trial["pos_idx"]
            elif name == "miss" and trial["miss_ts"] is None:
                trial["miss_ts"] = ts
                trial["miss_pos_idx"] = trial["pos_idx"]

        for trial in trials.values():
            if trial["hit_ts"] is not None:
                pos_idx = trial["hit_pos_idx"] if trial["hit_pos_idx"] is not None else trial["pos_idx"]
                markers.append((trial["hit_ts"], "earned", pos_idx))
            elif trial["miss_ts"] is not None:
                pos_idx = trial["miss_pos_idx"] if trial["miss_pos_idx"] is not None else trial["pos_idx"]
                markers.append((trial["miss_ts"], "miss", pos_idx))
            elif trial["free_ts"] is not None:
                pos_idx = trial["free_pos_idx"] if trial["free_pos_idx"] is not None else trial["pos_idx"]
                markers.append((trial["free_ts"], "free", pos_idx))
        markers.sort(key=lambda item: item[0])
        return markers

    def _draw_cumulative_task_raster(self):
        if not hasattr(self, 'cumulative_timeline_canvas'):
            return
        c = self.cumulative_timeline_canvas
        c.delete('all')
        try:
            w = c.winfo_width()
            h = c.winfo_height()
        except Exception:
            w, h = 700, 220
        if w <= 2:
            try:
                w = int(float(c.cget('width')))
            except Exception:
                w = 700
        if h <= 2:
            try:
                h = int(float(c.cget('height')))
            except Exception:
                h = 220
        w = max(w, 360)
        h = max(h, 140)
        left = 120
        top = 10
        bottom_margin = 18
        labels = self.position_labels if hasattr(self, 'position_labels') and len(self.position_labels) >= 6 else [f'Pos {i}' for i in range(6)]
        event_row_h = max(10.0, (h - top - bottom_margin) / len(labels))
        usable_w = max(40.0, w - left - 10)
        label_font = ('TkDefaultFont', max(7, min(10, int(event_row_h * 0.6))))
        legend_font = ('TkDefaultFont', max(7, min(9, int(event_row_h * 0.5))))
        end_ts = self._session_timeline_end_unix if self._session_timeline_end_unix is not None else time.time()
        start_ts = self._session_timeline_start_unix if self._session_timeline_start_unix is not None else None
        if start_ts is not None:
            window_s = max(20.0, end_ts - start_ts)
        else:
            window_s = 20.0
        c.create_rectangle(0, 0, w, h, fill='white', outline='')
        for i, label in enumerate(labels):
            y = top + i * event_row_h + event_row_h / 2
            c.create_text(6, y, text=label, anchor='w', fill='#333333', font=label_font)
            c.create_line(left, y, w - 4, y, fill='#e7e7e7')
        c.create_line(left, top, left, h - bottom_margin, fill='#999999')
        tick_count = 6
        for i in range(tick_count + 1):
            frac = i / tick_count
            x = left + usable_w * frac
            c.create_line(x, top, x, h - bottom_margin, fill='#f0f0f0')
            sec = window_s * frac
            label = f'{sec/60.0:.0f}m' if sec >= 60 else f'{sec:.0f}s'
            c.create_text(x, h - 2, text=label, anchor='s', fill='#666666', font=legend_font)

        markers = self._build_cumulative_trial_markers()
        marker_radius = max(1.5, min(4.0, usable_w / max(140.0, len(markers) * 1.4 if markers else 140.0)))
        for ts, marker_kind, pos_idx in markers:
            if start_ts is None or pos_idx is None or not (0 <= pos_idx < len(labels)):
                continue
            frac = 0.0 if window_s <= 0 else max(0.0, min(1.0, (ts - start_ts) / window_s))
            x = left + usable_w * frac
            y = top + pos_idx * event_row_h + event_row_h / 2
            if marker_kind == 'free':
                c.create_oval(x-marker_radius, y-marker_radius, x+marker_radius, y+marker_radius, fill='', outline='#00897b', width=2)
            elif marker_kind == 'earned':
                c.create_oval(x-marker_radius, y-marker_radius, x+marker_radius, y+marker_radius, fill='#2e7d32', outline='')
            elif marker_kind == 'miss':
                c.create_oval(x-marker_radius, y-marker_radius, x+marker_radius, y+marker_radius, fill='#d32f2f', outline='')
        c.create_text(w-6, 4, text='Cumulative session performance', anchor='ne', fill='#666666', font=legend_font)

    def _draw_lick_trace(self):
        if not hasattr(self, 'lick_canvas'):
            return
        c = self.lick_canvas
        c.delete('all')
        w = max(c.winfo_width(), 400)
        h = max(c.winfo_height(), 120)
        left = 50
        top = 8
        bottom = h - 20
        right = w - 8
        c.create_rectangle(0,0,w,h, fill='white', outline='')
        c.create_line(left, top, left, bottom, fill='#999999')
        c.create_line(left, bottom, right, bottom, fill='#999999')
        for val in (0, 1024, 2048, 3072, 4095):
            y = top + (1 - val/4095.0)*(bottom-top)
            c.create_line(left, y, right, y, fill='#f0f0f0')
            c.create_text(4, y, text=str(val), anchor='w', fill='#666666')
        pts = [p for p in self.lick_samples if time.time() - p[0] <= 20.0]
        if not pts:
            c.create_text((left+right)/2, (top+bottom)/2, text='Enable lick debug streaming to view live trace', fill='#777777')
            return
        def x_of(ts):
            return left + (right-left) * (1 - ((time.time()-ts)/20.0))
        def y_of(v):
            return top + (1 - max(0,min(4095,v))/4095.0)*(bottom-top)
        raw_xy=[]; base_xy=[]; thr_xy=[]
        for ts, raw, base, thr, lick in pts:
            x=x_of(ts)
            raw_xy.extend((x,y_of(raw)))
            base_xy.extend((x,y_of(base)))
            thr_xy.extend((x,y_of(thr)))
            if lick:
                c.create_line(x, top, x, bottom, fill='#ffebee')
        if len(thr_xy) >= 4: c.create_line(*thr_xy, fill='#ef5350', width=1)
        if len(base_xy) >= 4: c.create_line(*base_xy, fill='#1e88e5', width=2)
        if len(raw_xy) >= 4: c.create_line(*raw_xy, fill='#2e7d32', width=2)
        c.create_text(right, top, anchor='ne', text='raw=green  baseline=blue  threshold=red', fill='#555555')


    def _iter_preview_positions_relative(self):
        out = []
        try:
            mx, my, mz = self.mouth_x_var.get_float(), self.mouth_y_var.get_float(), self.mouth_z_var.get_float()
            for i, pv in enumerate(self.position_preview_vars):
                xyz_text = pv["xyz"].get().strip().strip("()")
                if not xyz_text:
                    continue
                try:
                    x_str, y_str, z_str = [s.strip() for s in xyz_text.split(",")]
                    x, y, z = float(x_str), float(y_str), float(z_str)
                    out.append({
                        "idx": i,
                        "name": self.position_labels[i] if i < len(self.position_labels) else f"pos{i}",
                        "x": x - mx,
                        "y": y - my,
                        "z": z - mz,
                    })
                except Exception:
                    pass
        except Exception:
            pass
        return out

    def _draw_position_diagrams(self):
        if not hasattr(self, "xy_canvas") or not hasattr(self, "yz_canvas"):
            return
        pts = self._iter_preview_positions_relative()
        try:
            az_center = self.az_center_var.get_float()
            az_left = self.az_left_var.get_float()
            az_right = self.az_right_var.get_float()
            down_angle = self.down_angle_var.get_float()
            head_roll = self.head_roll_var.get_float()
        except Exception:
            az_center = az_left = az_right = down_angle = head_roll = 0.0
        lateral_left = abs(az_left - az_center)
        lateral_right = abs(az_right - az_center)
        xy_angle_text = f"L lat {lateral_left:.1f} deg   R lat {lateral_right:.1f} deg   Roll {head_roll:.1f} deg"
        yz_angle_text = f"Downward {down_angle:.1f} deg"

        def _bounds_with_pad(values, *, include_zero=False, pad_frac=0.12, min_span=1.0, floor=None, ceil=None):
            vals = [float(v) for v in values]
            if include_zero:
                vals.append(0.0)
            if not vals:
                vals = [0.0]
            lo = min(vals)
            hi = max(vals)
            if floor is not None:
                lo = max(lo, floor)
            if ceil is not None:
                hi = min(hi, ceil)
            span = hi - lo
            if span < min_span:
                extra = (min_span - span) / 2.0
                lo -= extra
                hi += extra
                span = hi - lo
            pad = max(span * pad_frac, min_span * 0.08)
            lo -= pad
            hi += pad
            if floor is not None:
                lo = max(lo, floor)
            if ceil is not None:
                hi = min(hi, ceil)
            if hi - lo < min_span:
                if floor is not None and lo <= floor + 1e-9:
                    hi = lo + min_span
                elif ceil is not None and hi >= ceil - 1e-9:
                    lo = hi - min_span
                else:
                    mid = (lo + hi) / 2.0
                    lo = mid - min_span / 2.0
                    hi = mid + min_span / 2.0
            return lo, hi

        def place_label(c, text, sx, sy, *, fill, mode, cx, cy, w, h, occupied):
            offset = 8
            if mode == "dock":
                tx, ty, anchor = sx, sy - max(offset, 10), "s"
            elif mode == "mouth":
                tx, ty, anchor = sx + offset, sy - offset, "sw"
            else:
                if sy > cy + 12:
                    tx, ty, anchor = sx, sy + offset, "n"
                elif sx < cx - 6:
                    tx, ty, anchor = sx - offset, sy, "e"
                else:
                    tx, ty, anchor = sx + offset, sy, "w"
                bucket = "bottom" if anchor == "n" else ("left" if anchor == "e" else "right")
                key = (bucket, int(round(tx / 20.0)))
                prev_y = occupied.get(key)
                if prev_y is not None and abs(ty - prev_y) < 14:
                    ty = prev_y + 14 if ty >= prev_y else prev_y - 14
                occupied[key] = ty
            tx = min(max(tx, 12), w - 12)
            ty = min(max(ty, 12), h - 12)
            c.create_text(tx, ty, text=text, anchor=anchor, fill=fill)

        for canvas, plane in ((self.xy_canvas, "xy"), (self.yz_canvas, "yz")):
            c = canvas
            c.delete("all")
            try:
                w = max(c.winfo_width(), 320)
                h = max(c.winfo_height(), 220)
            except Exception:
                w, h = 320, 220
            pad_x = 28
            pad_y = 34 if plane == "xy" else 28
            plot_w = max(1.0, w - 2 * pad_x)
            plot_h = max(1.0, h - 2 * pad_y)

            if plane == "xy":
                plotted_pts = [{"idx": p["idx"], "name": p["name"], "a": p["x"], "b": p["y"]} for p in pts]
                a_vals = [0.0] + [p["a"] for p in plotted_pts]
                b_vals = [0.0] + [p["b"] for p in plotted_pts]
                a_lo, a_hi = _bounds_with_pad(a_vals, include_zero=True, min_span=0.8, pad_frac=0.05)
                if max(b_vals) <= 0.0:
                    b_lo, b_hi = _bounds_with_pad(b_vals, include_zero=True, min_span=1.0, pad_frac=0.06)
                else:
                    b_lo, b_hi = _bounds_with_pad(b_vals, include_zero=True, min_span=1.0, pad_frac=0.06)
                title = "XY (camera view)"
                angle_text = xy_angle_text
            else:
                plotted_pts = [{"idx": p["idx"], "name": p["name"], "a": max(0.0, -p["y"]), "b": p["z"]} for p in pts]
                a_vals = [0.0] + [p["a"] for p in plotted_pts]
                b_vals = [0.0] + [p["b"] for p in plotted_pts]
                a_lo, a_hi = 0.0, max(max(a_vals) * 1.08, 1.0)
                if max(b_vals) <= 0.0:
                    b_lo, b_hi = _bounds_with_pad(b_vals, include_zero=True, min_span=1.0, pad_frac=0.06)
                else:
                    b_lo, b_hi = _bounds_with_pad(b_vals, include_zero=True, min_span=1.0, pad_frac=0.06)
                title = "YZ (camera view)"
                angle_text = yz_angle_text

            def sx_for(a):
                if abs(a_hi - a_lo) < 1e-9:
                    return pad_x + plot_w / 2.0
                if plane == "yz":
                    return pad_x + (a - a_lo) / (a_hi - a_lo) * plot_w
                return pad_x + (a_hi - a) / (a_hi - a_lo) * plot_w

            def sy_for(b):
                if abs(b_hi - b_lo) < 1e-9:
                    return pad_y + plot_h / 2.0
                return pad_y + (b_hi - b) / (b_hi - b_lo) * plot_h

            cx = sx_for(0.0)
            cy = sy_for(0.0)
            if b_lo <= 0.0 <= b_hi:
                c.create_line(pad_x, cy, w - pad_x, cy, fill="#cccccc")
            if a_lo <= 0.0 <= a_hi:
                c.create_line(cx, pad_y, cx, h - pad_y, fill="#cccccc")
            if plane == "xy":
                c.create_text(pad_x, cy - 8, text="mouse R", anchor="nw", fill="#666")
                c.create_text(w - pad_x, cy - 8, text="mouse L", anchor="ne", fill="#666")
                c.create_text(cx + 6, pad_y, text="closer", anchor="nw", fill="#666")
                # Display-only dock marker: fixed at positive Y, centered on mouth X.
                dock_sx = cx
                dock_sy = pad_y + 0.82 * plot_h
            else:
                c.create_text(w - pad_x, cy - 8, text="spout side", anchor="ne", fill="#666")
                c.create_text(cx + 6, h - pad_y, text="down", anchor="sw", fill="#666")
                # Display-only dock marker: fixed at positive Y, aligned with mouth Z.
                dock_sx = pad_x + 0.82 * plot_w
                dock_sy = cy
            c.create_text(6, 6, text=title, anchor="nw", fill="#444")
            c.create_text(6, 22, text=angle_text, anchor="nw", fill="#555")

            occupied = {}
            c.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill="black")
            place_label(c, "mouth", cx, cy, fill="black", mode="mouth", cx=cx, cy=cy, w=w, h=h, occupied=occupied)

            dock_fill = "#1565c0" if getattr(self, "current_at_dock", False) else ""
            c.create_oval(dock_sx - 4, dock_sy - 4, dock_sx + 4, dock_sy + 4, outline="#1565c0", fill=dock_fill, width=2)
            place_label(c, "dock", dock_sx, dock_sy, fill="#1565c0", mode="dock", cx=cx, cy=cy, w=w, h=h, occupied=occupied)

            sel = getattr(self, "current_active_pos_idx", None)
            if sel is None and not getattr(self, "current_at_dock", False):
                try:
                    s = self.latest_status.get("current_pos", "") if isinstance(self.latest_status, dict) else ""
                    if str(s).lstrip("-").isdigit():
                        i = int(s)
                        sel = i if 0 <= i < 6 else None
                except Exception:
                    sel = None
            if sel is None and not getattr(self, "current_at_dock", False):
                try:
                    sel = int(self.monitor_pos_var.get())
                except Exception:
                    sel = None
            for p in plotted_pts:
                a, b = p["a"], p["b"]
                sx = sx_for(a)
                sy = sy_for(b)
                r = 5 if p["idx"] == sel else 4
                fill = "#d62728" if p["idx"] == sel else "#1f77b4"
                c.create_oval(sx - r, sy - r, sx + r, sy + r, fill=fill, outline="")
                place_label(c, f'{p["idx"]}:{p["name"]}', sx, sy, fill=fill, mode="position", cx=cx, cy=cy, w=w, h=h, occupied=occupied)

    def clear_visuals(self):
        self._clear_session_visual_history()
        self.lick_samples.clear()
        self._last_lick_state = 0
        self.current_active_pos_idx = None
        self.current_at_dock = False
        self._pending_free_reward_marker = None
        if not hasattr(self, "position_labels") or not self.position_labels:
            self.position_labels = [f"Pos {i}" for i in range(6)]
        if hasattr(self, "current_pos_label_var"):
            self.current_pos_label_var.set("Pos: --  Dist: -- mm")
        if hasattr(self, "last_event_var"):
            self.last_event_var.set("Last event: --")
        self._draw_task_raster()
        self._draw_cumulative_task_raster()
        self._draw_lick_trace()
        self._draw_position_diagrams()

    def _set_reward_hold_state(self, held: bool, source: str = "gui"):
        held = bool(held)
        prev = bool(self.reward_hold_var.get())
        self.reward_hold_var.set(held)
        self.reward_hold_button_var.set("Resume rewards" if held else "Hold rewards")
        if source == "device" and held != prev:
            self._log_local(f"[GUI] Device reward hold is now {self._reward_hold_label()}.")

    def _reward_hold_label(self, data=None):
        data = data if isinstance(data, dict) else (self.latest_status if isinstance(self.latest_status, dict) else {})
        held = self._is_truthy_flag(data.get("rewards_held", data.get("task_rewards_held", "0")))
        manual = self._is_truthy_flag(data.get("manual_reward_hold_active", "0"))
        auto = self._is_truthy_flag(data.get("auto_reward_hold_active", "0"))
        if manual and auto:
            return "MANUAL+AUTO"
        if auto:
            return "AUTO"
        if manual:
            return "MANUAL"
        return "ON" if held else "OFF"

    def _refresh_reward_summary(self, data=None):
        data = data if isinstance(data, dict) else (self.latest_status if isinstance(self.latest_status, dict) else {})
        self.reward_summary_var.set(
            f"Rewards: total={data.get('total_rewards','--')}  free={data.get('free_rewards','--')}  "
            f"auto={data.get('auto_rewards','--')}  held={self._reward_hold_label(data)}"
        )

    def send_cue_reward(self):
        self.send("CUEREWARD")

    def send_manual_reward(self):
        if self.send("REWARD"):
            self.status_line_var.set("Requested manual reward")

    def toggle_reward_hold(self):
        cmd = "SET task.rewards_held=0" if self.reward_hold_var.get() else "SET task.rewards_held=1"
        if self.send(cmd):
            self.status_line_var.set("Waiting for reward-hold update from device...")

    def reset_session(self):
        self._clear_session_visual_history()
        self.send("RESETSESSION")

    def stop_task(self):
        if self.send("STOP") and self._last_status_run and self._session_timeline_end_unix is None:
            self._session_timeline_end_unix = time.time()

    def move_to_monitor_position(self):
        idx = self.monitor_pos_var.get().strip() or "0"
        try:
            mx = abs(self.mouth_x_var.get_float())
            my = abs(self.mouth_y_var.get_float())
            mz = abs(self.mouth_z_var.get_float())
            if mx < 1e-6 and my < 1e-6 and mz < 1e-6:
                self._append_console("[GUI NOTE] MOUTH origin is still 0,0,0. If the stages were homed elsewhere, generated test positions may be invalid. Use Motion tab -> Use current XYZ for MOUTH first.")
        except Exception:
            pass
        cmds = self._position_test_prep_cmds() + [f"MOVE mode=pos idx={idx}"]
        self._queue_commands(cmds, delay_ms=120, final_status=f"Moved to test position {idx}")
        self.safez_sync_var.set("SAFE Z: applied to device")
        self.mouth_sync_var.set("MOUTH: applied to device")
        self.geometry_sync_var.set("Geometry: applied to device")
        self.enabled_sync_var.set("Enabled positions: applied to device")
        self._mark_categories_applied('mouth', 'safez', 'geometry', 'enabled')
        return True

    def _position_test_prep_cmds(self):
        cmds = [
            f"SET motion.mouth_origin.x_mm={self.mouth_x_var.get()}",
            f"SET motion.mouth_origin.y_mm={self.mouth_y_var.get()}",
            f"SET motion.mouth_origin.z_mm={self.mouth_z_var.get()}",
            f"SET motion.safe_z_mm={self.safe_z_var.get()}",
            f"SET geom.dist_close_mm={self.dist_close_var.get_float()}",
            f"SET geom.dist_far_mm={self.dist_far_var.get_float()}",
            f"SET geom.az_center_deg={self.az_center_var.get_float()}",
            f"SET geom.az_left_deg={self.az_left_var.get_float()}",
            f"SET geom.az_right_deg={self.az_right_var.get_float()}",
            f"SET geom.down_angle_deg={self.down_angle_var.get_float()}",
            f"SET geom.head_roll_deg={self.head_roll_var.get_float()}",
        ]
        cmds.extend([f"SET task.enable_pos{i}={1 if v.get() else 0}" for i, v in enumerate(self.pos_enabled_vars)])
        return cmds

    def _move_to_named_position_idx(self, idx, final_status=None):
        cmds = [
            f"SET motion.safe_z_mm={self.safe_z_var.get()}",
            f"MOVE mode=pos idx={idx}",
        ]
        self._queue_commands(cmds, delay_ms=120, final_status=final_status)
        self.safez_sync_var.set("SAFE Z: applied to device")
        self._mark_categories_applied('safez')
        return True

    def _send_sequence_move_to_position_idx(self, idx, token):
        if not self._ensure_connected_for_command():
            self._sequence_fail(f"Sequence: could not connect for pos {idx}", token=token)
            return
        if not self.send(f"SET motion.safe_z_mm={self.safe_z_var.get()}"):
            self._sequence_fail(f"Sequence: failed to push SAFE Z before pos {idx}", token=token)
            return
        self.safez_sync_var.set("SAFE Z: applied to device")
        self._mark_categories_applied('safez')

        def _send_move():
            if not self._sequence_running or token != self._sequence_token:
                return
            ok = self.send(f"MOVE mode=pos idx={idx}")
            if not ok:
                self._sequence_fail(f"Sequence: failed to send move for pos {idx}", token=token)
                return
            self._sequence_waiting_idx = idx
            self._sequence_move_inflight = True
            self.sequence_status_var.set(
                f"Sequence: moving to step {self._sequence_completed_steps + 1} / {self._sequence_total_steps} (pos {idx})"
            )

        self.after(120, _send_move)

    def start_position_sequence(self):
        self.stop_position_sequence(silent=True)
        if getattr(self, "_command_batch_active", False):
            messagebox.showinfo(APP_TITLE, "A command batch is still being sent to the device. Please wait for it to finish before starting the 6-position test.")
            return
        try:
            dwell_ms = max(200, int(self.sequence_dwell_var.get_float(1200)))
        except Exception:
            dwell_ms = 1200
        try:
            cycles = max(1, int(self.sequence_cycles_var.get_float(1)))
        except Exception:
            cycles = 1
        self._sequence_token += 1
        self._sequence_queue = list(range(6)) * cycles
        self._sequence_step_delay_ms = dwell_ms
        self._sequence_total_steps = len(self._sequence_queue)
        self._sequence_completed_steps = 0
        self._sequence_waiting_idx = None
        self._sequence_move_inflight = False
        self._sequence_running = True
        self.sequence_status_var.set(f"Sequence: preparing ({cycles} cycle{'s' if cycles != 1 else ''}, {dwell_ms} ms dwell)")

        def _start_after_prep(expected_token=self._sequence_token, expected_cycles=cycles, expected_dwell_ms=dwell_ms):
            if not self._sequence_running or expected_token != self._sequence_token:
                return
            self.safez_sync_var.set("SAFE Z: applied to device")
            self.mouth_sync_var.set("MOUTH: applied to device")
            self.geometry_sync_var.set("Geometry: applied to device")
            self.enabled_sync_var.set("Enabled positions: applied to device")
            self._mark_categories_applied('mouth', 'safez', 'geometry', 'enabled')
            self.sequence_status_var.set(f"Sequence: running ({expected_cycles} cycle{'s' if expected_cycles != 1 else ''}, {expected_dwell_ms} ms dwell)")
            self._run_next_sequence_step()

        self._queue_commands(self._position_test_prep_cmds(), delay_ms=120, final_status="Prepared 6-position test geometry", on_complete=_start_after_prep)

    def _run_next_sequence_step(self):
        if not self._sequence_running:
            return
        if not self._sequence_queue:
            self._sequence_running = False
            self._sequence_after_id = None
            self.sequence_status_var.set("Sequence: complete")
            return

        idx = self._sequence_queue.pop(0)
        self.monitor_pos_var.set(str(idx))
        self._sequence_after_id = None
        self._send_sequence_move_to_position_idx(idx, self._sequence_token)

    def _sequence_on_position_arrival(self, idx):
        if not self._sequence_running:
            return
        if not self._sequence_move_inflight:
            return
        if self._sequence_waiting_idx is None or idx != self._sequence_waiting_idx:
            return
        self._sequence_move_inflight = False
        self._sequence_waiting_idx = None
        self._sequence_completed_steps += 1
        current_step = self._sequence_completed_steps
        self.sequence_status_var.set(f"Sequence: step {current_step} / {self._sequence_total_steps} (pos {idx})")

        if self.sequence_with_cue_var.get():
            cue_delay = min(300, max(80, self._sequence_step_delay_ms // 4))
            token = self._sequence_token
            def _cue_if_running(expected_idx=idx, expected_step=current_step):
                if not self._sequence_running or token != self._sequence_token:
                    return
                if self._sequence_completed_steps != expected_step:
                    return
                if self.monitor_pos_var.get() == str(expected_idx):
                    self.send("CUE")
            self._sequence_cue_after_id = self.after(cue_delay, _cue_if_running)

        self._sequence_after_id = self.after(self._sequence_step_delay_ms, self._run_next_sequence_step)

    def _sequence_fail(self, message, token=None):
        if token is not None and token != self._sequence_token:
            return
        self.stop_position_sequence(silent=True)
        self.sequence_status_var.set(message)

    def stop_position_sequence(self, silent=False):
        self._sequence_token += 1
        self._sequence_running = False
        self._sequence_queue = []
        self._sequence_waiting_idx = None
        self._sequence_move_inflight = False
        if self._sequence_after_id is not None:
            try:
                self.after_cancel(self._sequence_after_id)
            except Exception:
                pass
            self._sequence_after_id = None
        if self._sequence_cue_after_id is not None:
            try:
                self.after_cancel(self._sequence_cue_after_id)
            except Exception:
                pass
            self._sequence_cue_after_id = None
        if not silent:
            self.sequence_status_var.set("Sequence: stopped")

    def enable_live_lick_monitor(self):
        self.lick_debug_var.set(True)
        self.apply_lick_settings()
        if getattr(self, "backend_var", None) is not None and self.backend_var.get() == "mega_zaber":
            return
        self.fetch_device_state()

    # ---------------- motion commands ----------------
    def jog_axis(self, axis: str, sign: int):
        step = self.jog_step_var.get_float(1.0)
        self.send(f"MOVE mode=jog axis={axis.lower()} mm={sign * step}")

    def set_mouth_from_current(self):
        self.mouth_x_var.set(self.curr_x_var.get())
        self.mouth_y_var.set(self.curr_y_var.get())
        self.mouth_z_var.set(self.curr_z_var.get())
        self._recompute_geometry_preview()

    def set_dock_from_current(self):
        self.dock_x_var.set(self.curr_x_var.get())
        self.dock_y_var.set(self.curr_y_var.get())
        self.dock_z_var.set(self.curr_z_var.get())

    def set_ref_from_current(self):
        self.ref_x_var.set(self.curr_x_var.get())
        self.ref_y_var.set(self.curr_y_var.get())
        self.ref_z_var.set(self.curr_z_var.get())

    def set_safez_from_current(self):
        self.safe_z_var.set(self.curr_z_var.get())

    def apply_current_reference(self):
        self._queue_commands([
            f"SETCURRENT x={self.ref_x_var.get()} y={self.ref_y_var.get()} z={self.ref_z_var.get()}",
            "GET kind=status",
        ], delay_ms=220)

    def home_or_reference_action(self):
        backend = self.backend_var.get() if hasattr(self, "backend_var") else "teensy_smc02"
        if backend == "mega_zaber":
            self.send("HOME")
        else:
            self.apply_current_reference()

    def apply_mouth(self):
        self._queue_commands([
            f"SET motion.mouth_origin.x_mm={self.mouth_x_var.get()}",
            f"SET motion.mouth_origin.y_mm={self.mouth_y_var.get()}",
            f"SET motion.mouth_origin.z_mm={self.mouth_z_var.get()}",
        ], delay_ms=220, final_status="Applied MOUTH origin")
        self.mouth_sync_var.set("MOUTH: applied to device")
        self._mark_categories_applied('mouth')
        self._recompute_geometry_preview()

    def apply_dock(self):
        self._queue_commands([
            f"SET motion.dock.x_mm={self.dock_x_var.get()}",
            f"SET motion.dock.y_mm={self.dock_y_var.get()}",
            f"SET motion.dock.z_mm={self.dock_z_var.get()}",
        ], delay_ms=220, final_status="Applied DOCK position")
        self.dock_sync_var.set("DOCK: applied to device")
        self._mark_categories_applied('dock')

    def apply_safez(self):
        self._queue_commands([f"SET motion.safe_z_mm={self.safe_z_var.get()}"], delay_ms=180, final_status="Applied SAFE Z")
        self.safez_sync_var.set("SAFE Z: applied to device")
        self._mark_categories_applied('safez')

    def push_all_origins_geometry(self):
        if self._validate_geometry_settings(show_dialog=True):
            return
        cmds = [
            f"SET motion.mouth_origin.x_mm={self.mouth_x_var.get()}",
            f"SET motion.mouth_origin.y_mm={self.mouth_y_var.get()}",
            f"SET motion.mouth_origin.z_mm={self.mouth_z_var.get()}",
            f"SET motion.dock.x_mm={self.dock_x_var.get()}",
            f"SET motion.dock.y_mm={self.dock_y_var.get()}",
            f"SET motion.dock.z_mm={self.dock_z_var.get()}",
            f"SET motion.safe_z_mm={self.safe_z_var.get()}",
            f"SET geom.dist_close_mm={self.dist_close_var.get_float()}",
            f"SET geom.dist_far_mm={self.dist_far_var.get_float()}",
            f"SET geom.az_center_deg={self.az_center_var.get_float()}",
            f"SET geom.az_left_deg={self.az_left_var.get_float()}",
            f"SET geom.az_right_deg={self.az_right_var.get_float()}",
            f"SET geom.down_angle_deg={self.down_angle_var.get_float()}",
            f"SET geom.head_roll_deg={self.head_roll_var.get_float()}",
        ]
        cmds.extend([f"SET task.enable_pos{i}={1 if v.get() else 0}" for i, v in enumerate(self.pos_enabled_vars)])
        self._queue_commands(cmds, delay_ms=200, final_status="Applied MOUTH + DOCK + SAFE Z + geometry + enabled positions")
        self.mouth_sync_var.set("MOUTH: applied to device")
        self.dock_sync_var.set("DOCK: applied to device")
        self.safez_sync_var.set("SAFE Z: applied to device")
        self.geometry_sync_var.set("Geometry: applied to device")
        self.enabled_sync_var.set("Enabled positions: applied to device")
        self._mark_categories_applied('enabled')
        self._recompute_geometry_preview()

    def _move_with_current_safez(self, x_text: str, y_text: str, z_text: str, status_text: str):
        cmds = [
            f"SET motion.safe_z_mm={self.safe_z_var.get()}",
            f"MOVE mode=xyz x={x_text} y={y_text} z={z_text}",
        ]
        self._queue_commands(cmds, delay_ms=120, final_status=status_text)
        self.safez_sync_var.set("SAFE Z: applied to device")
        self._mark_categories_applied('safez')

    def move_to_mouth(self):
        self._move_with_current_safez(
            self.mouth_x_var.get(),
            self.mouth_y_var.get(),
            self.mouth_z_var.get(),
            "Moved to current mouth",
        )

    def move_to_dock(self):
        self._move_with_current_safez(
            self.dock_x_var.get(),
            self.dock_y_var.get(),
            self.dock_z_var.get(),
            "Moved to dock",
        )

    def move_to_xyz_fields(self):
        self._move_with_current_safez(
            self.ref_x_var.get(),
            self.ref_y_var.get(),
            self.ref_z_var.get(),
            "Moved to ref XYZ",
        )

    def apply_axis_cal(self, axis: str):
        d = self.axis_cal[axis]
        ax = axis.lower()
        self._queue_commands([
            f"SET motion.axis.{ax}.ms_per_mm_pos={d['ms_pos'].get()}",
            f"SET motion.axis.{ax}.ms_per_mm_neg={d['ms_neg'].get()}",
            f"SET motion.axis.{ax}.overhead_ms={d['overhead'].get()}",
            f"SET motion.axis.{ax}.cw_is_positive={1 if d['cw_pos'].get() else 0}",
        ], delay_ms=180, final_status=f"Applied {axis} axis calibration")

    def apply_all_axis_cal(self):
        cmds = []
        for axis in ("X", "Y", "Z"):
            d = self.axis_cal[axis]
            ax = axis.lower()
            cmds.extend([
                f"SET motion.axis.{ax}.ms_per_mm_pos={d['ms_pos'].get()}",
                f"SET motion.axis.{ax}.ms_per_mm_neg={d['ms_neg'].get()}",
                f"SET motion.axis.{ax}.overhead_ms={d['overhead'].get()}",
                f"SET motion.axis.{ax}.cw_is_positive={1 if d['cw_pos'].get() else 0}",
            ])
        self._queue_commands(cmds, delay_ms=180, final_status="Applied all axis calibration")

    # ---------------- task commands ----------------
    def _cue_settings_cmds(self):
        cmds = [
            f"SET cue.frequency_hz={self.cue_hz_var.get_int()}",
            f"SET cue.duration_ms={self.cue_duration_var.get_int()}",
        ]
        if self.backend_var.get() != "mega_zaber":
            cmds.append(f"SET cue.volume_pct={self.cue_volume_var.get_int()}")
        return cmds

    def _reward_settings_cmds(self):
        if self.protocol_version >= 2:
            return [
                f"SET task.reward_ms={self.reward_ms_var.get_int()}",
                f"SET task.reward_ul={self.reward_ul_var.get_float()}",
                f"SET task.water_limit_ul={self.water_limit_ul_var.get_float()}",
            ]
        return [
            f"SET REWARD_MS {self.reward_ms_var.get_int()}",
            f"SET REWARD_UL {self.reward_ul_var.get_float()}",
            f"SET WATER_LIMIT_UL {self.water_limit_ul_var.get_float()}",
        ]

    def _timing_settings_cmds(self):
        self._update_scheduler_summary()
        return [
            f"SET task.settle_ms={self.settle_ms_var.get_int()}",
            f"SET task.post_reward_hold_ms={self.posthold_ms_var.get_int()}",
            f"SET task.pre_cue_min_ms={self.precue_min_var.get_int()}",
            f"SET task.pre_cue_max_ms={self.precue_max_var.get_int()}",
            f"SET task.response_window_ms={self.response_window_var.get_int()}",
            f"SET task.iti_min_ms={self.iti_min_var.get_int()}",
            f"SET task.iti_jitter_ms={self.iti_jitter_var.get_int()}",
            f"SET sync.min_pulse_ms={self.sync_pulse_min_var.get_int()}",
            f"SET sync.max_pulse_ms={self.sync_pulse_max_var.get_int()}",
            f"SET sync.min_interval_ms={self.sync_interval_min_var.get_int()}",
            f"SET sync.max_interval_ms={self.sync_interval_max_var.get_int()}",
        ]

    def _scheduler_settings_cmds(self):
        block_min = max(1, self.block_size_min_var.get_int(5))
        block_max = max(block_min, self.block_size_max_var.get_int(block_min))
        self.block_size_var.set(str(block_min))
        self._update_scheduler_summary()
        return [
            f"SET task.block_size={block_min}",
            f"SET task.block_size_min={block_min}",
            f"SET task.block_size_max={block_max}",
            f"SET task.target_trials_per_position_enabled={1 if self.target_trials_enabled_var.get() else 0}",
            f"SET task.target_trials_per_position={max(1, self.target_trials_per_pos_var.get_int(50))}",
            f"SET task.max_duration_enabled={1 if self.max_duration_enabled_var.get() else 0}",
            f"SET task.max_duration_min={max(1, self.max_duration_min_var.get_int(60))}",
            f"SET task.scheduling_mode={(self.scheduling_mode_var.get().strip() or 'balanced_block_cycles')}",
            f"SET task.stop_mode={(self.stop_mode_var.get().strip() or 'end_of_current_block')}",
        ]

    def _logic_settings_cmds(self):
        reward_mode = self.reward_mode_var.get().strip() or "contingent"
        if self.protocol_version >= 2:
            return [
                f"SET task.reward_mode={reward_mode}",
                f"SET task.enforce_no_lick={1 if self.enforce_var.get() else 0}",
                f"SET task.auto_reward_delay_ms={self.auto_reward_delay_var.get_int()}",
                f"SET task.auto_hold_after_miss_enabled={1 if self.auto_hold_after_miss_enabled_var.get() else 0}",
                f"SET task.auto_hold_after_miss_threshold={max(1, self.auto_hold_after_miss_threshold_var.get_int(3))}",
            ]
        cmds = []
        if reward_mode == "contingent":
            cmds = ["SET CONTINGENT 1", "SET AUTOREWARD 0"]
        elif reward_mode == "auto_after_delay":
            cmds = ["SET CONTINGENT 0", "SET AUTOREWARD 1"]
        else:
            cmds = ["SET CONTINGENT 1", "SET AUTOREWARD 1"]
        cmds.extend([
            f"SET ENFORCE {1 if self.enforce_var.get() else 0}",
            f"SET AUTOREWARD_DELAY {self.auto_reward_delay_var.get_int()}",
        ])
        return cmds

    def _adaptive_settings_cmds(self):
        cmds = [
              f"SET adapt.enabled={1 if self.adaptive_enabled_var.get() else 0}",
              f"SET adapt.use_per_position={1 if self.adapt_use_per_position_var.get() else 0}",
              f"SET adapt.hits_to_advance={self.adapt_hits_var.get_int()}",
              f"SET adapt.misses_to_decrease={self.adapt_misses_var.get_int()}",
              f"SET adapt.step_mm={self.adapt_step_var.get_float()}",
              f"SET adapt.decrease_step_mm={self.adapt_step_down_var.get_float()}",
              f"SET adapt.min_distance_mm={self.adapt_min_var.get_float()}",
              f"SET adapt.max_distance_mm={self.adapt_max_var.get_float()}",
          ]
        if self.adapt_use_per_position_var.get():
            for i in range(6):
                cmds.extend([
                    f"SET adapt.pos{i}.enabled={1 if self.adapt_pos_enabled_vars[i].get() else 0}",
                    f"SET adapt.pos{i}.hits_to_advance={self.adapt_pos_hits_vars[i].get_int()}",
                    f"SET adapt.pos{i}.misses_to_decrease={self.adapt_pos_misses_vars[i].get_int()}",
                    f"SET adapt.pos{i}.step_mm={self.adapt_pos_step_vars[i].get_float()}",
                    f"SET adapt.pos{i}.decrease_step_mm={self.adapt_pos_step_down_vars[i].get_float()}",
                    f"SET adapt.pos{i}.min_distance_mm={self.adapt_pos_min_vars[i].get_float()}",
                    f"SET adapt.pos{i}.max_distance_mm={self.adapt_pos_max_vars[i].get_float()}",
                ])
        return cmds

    def _verify_adaptive_scope_after_apply(self, expected_use_per_position, success_status=None, on_success=None):
        expected_value = "1" if expected_use_per_position else "0"
        expected_label = "per-position" if expected_use_per_position else "shared/global"

        if self._current_backend_name() == "mega_zaber":
            if success_status:
                self.zaber_status_var.set(success_status)
            if on_success:
                try:
                    on_success()
                except Exception:
                    pass
            return

        def _check_scope():
            self._adaptive_verify_after_id = None
            actual_raw = str(self.device_config_cache.get("adapt.use_per_position", "")).strip().lower()
            if actual_raw == expected_value:
                if success_status:
                    self.zaber_status_var.set(success_status)
                if on_success:
                    try:
                        on_success()
                    except Exception:
                        pass
                return

            if actual_raw in ("1", "true", "on", "yes"):
                actual_label = "per-position"
            elif actual_raw in ("0", "false", "off", "no"):
                actual_label = "shared/global"
            else:
                actual_label = "unknown"
            self.zaber_status_var.set("Adaptive settings may be partially applied")
            self.device_apply_state_var.set("Adaptive settings need verification on device")
            messagebox.showwarning(
                APP_TITLE,
                "Adaptive settings were sent, but the device did not confirm the expected adaptive scope.\n\n"
                f"Expected: {expected_label}\n"
                f"Device reports: {actual_label}\n\n"
                "Per-position adaptive enable flags may have been ignored. Please apply adaptive settings again and confirm the device state before continuing.",
            )

        def _request_config_refresh():
            self._queue_commands(["GET kind=config"], delay_ms=220, on_complete=lambda: self._schedule_adaptive_scope_check(_check_scope))

        _request_config_refresh()

    def _schedule_adaptive_scope_check(self, callback, delay_ms=300):
        try:
            pending = getattr(self, "_adaptive_verify_after_id", None)
            if pending:
                self.after_cancel(pending)
        except Exception:
            pass
        self._adaptive_verify_after_id = self.after(delay_ms, callback)

    def _queue_commands_with_adaptive_scope_verification(self, commands, delay_ms=180, final_status=None, pending_status=None, on_success=None):
        # Mega/Zaber firmware can spend long enough streaming config state that a
        # follow-up START collides with verification. Skip device-side verification
        # there and trust the applied settings, while still guarding START below.
        if self._current_backend_name() == "mega_zaber":
            self._queue_commands(commands, delay_ms=delay_ms, final_status=final_status, on_complete=on_success)
            return

        # When adaptive is disabled entirely there is no scope distinction to verify.
        if not bool(self.adaptive_enabled_var.get()):
            self._queue_commands(commands, delay_ms=delay_ms, final_status=final_status, on_complete=on_success)
            return

        expected_use_per_position = bool(self.adapt_use_per_position_var.get())
        verify_status = pending_status
        if verify_status is None and final_status:
            verify_status = f"{final_status} Verifying adaptive scope..."

        def _after_apply():
            self._verify_adaptive_scope_after_apply(
                expected_use_per_position,
                success_status=final_status,
                on_success=on_success,
            )

        self._queue_commands(commands, delay_ms=delay_ms, final_status=verify_status, on_complete=_after_apply)

    def _free_reward_settings_cmds(self):
        return [
            f"SET free_reward.enabled={1 if self.free_reward_enabled_var.get() else 0}",
            f"SET free_reward.after_misses={self.free_after_misses_var.get_int()}",
            f"SET free_reward.delay_ms={self.free_delay_var.get_int()}",
        ]

    def _lick_settings_cmds(self):
        if getattr(self, "backend_var", None) is not None and self.backend_var.get() == "mega_zaber":
            return [f"SET lick.debug={1 if self.lick_debug_var.get() else 0}"]
        return [
            f"SET lick.threshold_counts={self.lick_thresh_var.get_int()}",
            f"SET lick.hysteresis_counts={self.lick_hyst_var.get_int()}",
            f"SET lick.polarity={self.lick_polarity_var.get().strip()}",
            f"SET lick.baseline_alpha={self.lick_alpha_var.get_float()}",
            f"SET lick.refractory_ms={self.lick_refract_var.get_int()}",
            f"SET lick.debug={1 if self.lick_debug_var.get() else 0}",
        ]

    def apply_all_session_task_settings(self):
        if self._validate_geometry_settings(show_dialog=True):
            return
        cmds = [
            f"SET motion.mouth_origin.x_mm={self.mouth_x_var.get()}",
            f"SET motion.mouth_origin.y_mm={self.mouth_y_var.get()}",
            f"SET motion.mouth_origin.z_mm={self.mouth_z_var.get()}",
            f"SET motion.dock.x_mm={self.dock_x_var.get()}",
            f"SET motion.dock.y_mm={self.dock_y_var.get()}",
            f"SET motion.dock.z_mm={self.dock_z_var.get()}",
            f"SET motion.safe_z_mm={self.safe_z_var.get()}",
            f"SET geom.dist_close_mm={self.dist_close_var.get_float()}",
            f"SET geom.dist_far_mm={self.dist_far_var.get_float()}",
            f"SET geom.az_center_deg={self.az_center_var.get_float()}",
            f"SET geom.az_left_deg={self.az_left_var.get_float()}",
            f"SET geom.az_right_deg={self.az_right_var.get_float()}",
            f"SET geom.down_angle_deg={self.down_angle_var.get_float()}",
            f"SET geom.head_roll_deg={self.head_roll_var.get_float()}",
        ]
        cmds.extend([f"SET task.enable_pos{i}={1 if v.get() else 0}" for i, v in enumerate(self.pos_enabled_vars)])
        if self.backend_var.get() == "mega_zaber":
            cmds.extend([
                f"SET zaber.axis.x.units_per_mm={self.zaber_x_upm_var.get_float()}",
                f"SET zaber.axis.y.units_per_mm={self.zaber_y_upm_var.get_float()}",
                f"SET zaber.axis.z.units_per_mm={self.zaber_z_upm_var.get_float()}",
            ])
        cmds.extend(self._cue_settings_cmds())
        cmds.extend(self._reward_settings_cmds())
        cmds.extend(self._timing_settings_cmds())
        cmds.extend(self._scheduler_settings_cmds())
        cmds.extend(self._logic_settings_cmds())
        cmds.extend(self._adaptive_settings_cmds())
        cmds.extend(self._free_reward_settings_cmds())
        cmds.extend(self._lick_settings_cmds())
        self.device_apply_state_var.set("Applying session/task settings to device...")
        def _on_complete():
            self.mouth_sync_var.set("MOUTH: applied to device")
            self.dock_sync_var.set("DOCK: applied to device")
            self.safez_sync_var.set("SAFE Z: applied to device")
            self.geometry_sync_var.set("Geometry: applied to device")
            self.enabled_sync_var.set("Enabled positions: applied to device")
            self._mark_categories_applied('mouth','dock','safez','geometry','enabled','cue','reward','timing','logic','adaptive')
            if self.backend_var.get() != "mega_zaber":
                self._mark_categories_applied('lick')
            self.device_apply_state_var.set("All required session/task settings pushed since connect")
            self._recompute_geometry_preview()
        self._queue_commands_with_adaptive_scope_verification(
            cmds,
            delay_ms=180,
            final_status="Applied all session/task settings to device",
            pending_status="Applied all session/task settings to device. Verifying adaptive scope...",
            on_success=_on_complete,
        )

    def apply_cue_settings(self):
        self._queue_commands(self._cue_settings_cmds(), delay_ms=180, final_status="Applied cue settings")
        self._mark_categories_applied('cue')

    def apply_reward_settings(self):
        self._queue_commands(self._reward_settings_cmds(), delay_ms=180, final_status="Applied reward settings")
        self._mark_categories_applied('reward')

    def apply_timing_settings(self):
        cmds = []
        cmds.extend(self._timing_settings_cmds())
        cmds.extend(self._scheduler_settings_cmds())
        self._queue_commands(cmds, delay_ms=180, final_status="Applied timing + scheduling / limits settings")
        self._mark_categories_applied('timing')

    def apply_logic_settings(self):
        self._queue_commands(self._logic_settings_cmds(), delay_ms=180, final_status="Applied task logic settings")
        self._mark_categories_applied('logic')

    # ---------------- geometry / adaptive ----------------
    def apply_geometry_settings(self):
        if self._validate_geometry_settings(show_dialog=True):
            return
        cmds = [
            f"SET geom.dist_close_mm={self.dist_close_var.get_float()}",
            f"SET geom.dist_far_mm={self.dist_far_var.get_float()}",
            f"SET geom.az_center_deg={self.az_center_var.get_float()}",
            f"SET geom.az_left_deg={self.az_left_var.get_float()}",
            f"SET geom.az_right_deg={self.az_right_var.get_float()}",
            f"SET geom.down_angle_deg={self.down_angle_var.get_float()}",
            f"SET geom.head_roll_deg={self.head_roll_var.get_float()}",
        ]
        self._queue_commands(cmds, delay_ms=220, final_status="Applied geometry settings")
        self.geometry_sync_var.set("Geometry: applied to device")
        self._mark_categories_applied('geometry')
        self._recompute_geometry_preview()

    def apply_enabled_positions(self):
        cmds = [f"SET task.enable_pos{i}={1 if v.get() else 0}" for i, v in enumerate(self.pos_enabled_vars)]
        self._queue_commands(cmds, delay_ms=180, final_status="Applied enabled positions")
        self.enabled_sync_var.set("Enabled positions: applied to device")

    def apply_adaptive_settings(self):
        self._queue_commands_with_adaptive_scope_verification(
            self._adaptive_settings_cmds(),
            delay_ms=180,
            final_status="Applied adaptive difficulty settings",
            pending_status="Applied adaptive difficulty settings. Verifying adaptive scope...",
            on_success=lambda: self._mark_categories_applied('adaptive'),
        )

    def apply_free_reward_settings(self):
        self._queue_commands(self._free_reward_settings_cmds(), delay_ms=180, final_status="Applied free reward settings")
        self._mark_categories_applied('adaptive')

    # ---------------- lick ----------------
    def apply_lick_settings(self):
        self._queue_commands(self._lick_settings_cmds(), delay_ms=150 if self.backend_var.get() != "mega_zaber" else 120, final_status="Applied lick settings" if self.backend_var.get() != "mega_zaber" else "Applied lick debug setting")
        if getattr(self, "backend_var", None) is not None and self.backend_var.get() == "mega_zaber":
            self._log_local("[GUI] Mega backend selected: analog lick threshold fields are informational only.")
            return
        self._mark_categories_applied('lick')

    def run_reward_calibration(self):
        pulses = max(1, self.reward_cal_pulses_var.get_int(100))
        if not messagebox.askyesno(APP_TITLE, f"Open the reward solenoid {pulses} times for calibration?"):
            return
        backend = self.backend_var.get() if hasattr(self, "backend_var") else ""
        if backend in ("teensy_smc02", "mega_zaber") or self.protocol_version >= 2:
            self.send(f"CAL kind=reward pulses={pulses}")
        else:
            self.send(f"CALREWARD {pulses}")

    def _compute_geometry_positions(self):
        mouth = (
            self.mouth_x_var.get_float(),
            self.mouth_y_var.get_float(),
            self.mouth_z_var.get_float(),
        )
        dist_close = self.dist_close_var.get_float()
        dist_far = self.dist_far_var.get_float()
        az = [
            self.az_center_var.get_float(),
            self.az_left_var.get_float(),
            self.az_right_var.get_float(),
        ]
        down = self.down_angle_var.get_float()
        head_roll = self.head_roll_var.get_float()
        position_map = [
            (dist_close, az[0], self.position_name_vars[0].get()),
            (dist_close, az[1], self.position_name_vars[1].get()),
            (dist_close, az[2], self.position_name_vars[2].get()),
            (dist_far, az[0], self.position_name_vars[3].get()),
            (dist_far, az[1], self.position_name_vars[4].get()),
            (dist_far, az[2], self.position_name_vars[5].get()),
        ]
        roll_rad = math.radians(head_roll)
        roll_cos = math.cos(roll_rad)
        roll_sin = math.sin(roll_rad)
        out = []
        for i, (r, az_deg, label) in enumerate(position_map):
            theta = math.radians(down)
            phi = math.radians(az_deg)
            x0 = r * math.cos(theta) * math.sin(phi)
            y0 = -r * math.cos(theta) * math.cos(phi)
            z0 = -r * math.sin(theta)
            # Rotate the full mouth-relative spout vector around the fore-aft
            # axis. Positive roll lowers mouse-right.
            x1 = x0 * roll_cos + z0 * roll_sin
            z1 = -x0 * roll_sin + z0 * roll_cos
            x = mouth[0] + x1
            y = mouth[1] + y0
            z = mouth[2] + z1
            out.append({"index": i, "label": label, "dist_mm": r, "az_deg": az_deg, "down_deg": down, "x": x, "y": y, "z": z})
        return mouth, out

    def _validate_geometry_settings(self, show_dialog=True):
        errors = []
        try:
            mouth, positions = self._compute_geometry_positions()
            dock = (
                self.dock_x_var.get_float(),
                self.dock_y_var.get_float(),
                self.dock_z_var.get_float(),
            )
            dist_close = self.dist_close_var.get_float()
            dist_far = self.dist_far_var.get_float()
            if dist_close <= 0 or dist_far <= 0:
                errors.append("Close and far distances must both be positive.")
            elif dist_close >= dist_far:
                errors.append("dist_close_mm should be less than dist_far_mm.")
            if self.backend_var.get() == "mega_zaber":
                if len(positions) >= 6:
                    if not (positions[1]["x"] < positions[2]["x"]):
                        errors.append("Left spout positions should have lower X than right spout positions (close pair).")
                    if not (positions[4]["x"] < positions[5]["x"]):
                        errors.append("Left spout positions should have lower X than right spout positions (far pair).")
                    if not (positions[1]["x"] <= positions[0]["x"] <= positions[2]["x"]):
                        errors.append("Close center X should fall between close left and close right X.")
                    if not (positions[4]["x"] <= positions[3]["x"] <= positions[5]["x"]):
                        errors.append("Far center X should fall between far left and far right X.")
                def _check_triplet(name, triplet):
                    for axis_name, value in zip(("X", "Y", "Z"), triplet):
                        if axis_name == "Z":
                            lo, hi = -101.60, 0.0
                        else:
                            lo, hi = 0.0, 101.60
                        if value < lo or value > hi:
                            errors.append(f"{name} {axis_name}={value:.3f} mm is outside the allowed Zaber range of {lo:.2f}-{hi:.2f} mm.")
                _check_triplet("MOUTH", mouth)
                _check_triplet("DOCK", dock)
                for pos in positions:
                    _check_triplet(pos["label"] or f"Pos {pos['index']}", (pos["x"], pos["y"], pos["z"]))
        except Exception as e:
            errors.append(f"Could not validate geometry: {e}")
        if errors and show_dialog:
            try:
                messagebox.showerror(APP_TITLE, "Geometry validation failed:\n\n- " + "\n- ".join(errors))
            except Exception:
                pass
        return errors

    # ---------------- local calculators ----------------
    def _recompute_geometry_preview(self):
        mouth, position_map = self._compute_geometry_positions()
        for i, pos in enumerate(position_map):
            self.position_preview_vars[i]["label"].set(pos["label"])
            self.position_preview_vars[i]["xyz"].set(f"({pos['x']:.3f}, {pos['y']:.3f}, {pos['z']:.3f})")
            self.position_preview_vars[i]["dist"].set(f"{pos['dist_mm']:.3f} mm")
            self.position_preview_vars[i]["down"].set(f"{pos['down_deg']:.2f}°")

    def _recompute_stepper_calculator(self):

        try:
            step_angle = self.step_angle_var.get_float()
            microstep = self.microstep_var.get_float()
            screw_lead = self.screw_lead_var.get_float()
            if step_angle <= 0 or microstep <= 0 or screw_lead <= 0:
                raise ValueError
            pulses_rev = (360.0 / step_angle) * microstep
            steps_per_mm = pulses_rev / screw_lead
            self.pulses_rev_var.set(f"{pulses_rev:.1f}")
            self.f09_display_var.set(f"{pulses_rev / 10.0:.1f}")
            self.steps_per_mm_var.set(f"{steps_per_mm:.1f}")

            for rpm_var, mm_s_var, ms_mm_var in (
                (self.rpm_x_var, self.mm_per_s_x_var, self.ms_per_mm_x_var),
                (self.rpm_y_var, self.mm_per_s_y_var, self.ms_per_mm_y_var),
                (self.rpm_z_var, self.mm_per_s_z_var, self.ms_per_mm_z_var),
            ):
                rpm = rpm_var.get_float()
                mm_per_s = (rpm / 60.0) * screw_lead
                ms_per_mm = (1000.0 / mm_per_s) if mm_per_s > 1e-9 else float("inf")
                mm_s_var.set(f"{mm_per_s:.4f}")
                ms_mm_var.set(f"{ms_per_mm:.2f}")
        except Exception:
            for v in (
                self.pulses_rev_var,
                self.f09_display_var,
                self.steps_per_mm_var,
                self.mm_per_s_x_var,
                self.mm_per_s_y_var,
                self.mm_per_s_z_var,
                self.ms_per_mm_x_var,
                self.ms_per_mm_y_var,
                self.ms_per_mm_z_var,
            ):
                v.set("")

    def populate_axis_cal_from_calculator(self):
        try:
            mx = float(self.ms_per_mm_x_var.get())
            my = float(self.ms_per_mm_y_var.get())
            mz = float(self.ms_per_mm_z_var.get())
        except Exception:
            messagebox.showerror(APP_TITLE, "Calculator outputs are not valid yet.")
            return
        for axis, val in (("X", mx), ("Y", my), ("Z", mz)):
            self.axis_cal[axis]["ms_pos"].set(f"{val:.2f}")
            self.axis_cal[axis]["ms_neg"].set(f"{val:.2f}")

    # ---------------- config save/load ----------------
    def _config_dict(self):
        return {
            "coordinate_convention": {
                "z_positive": "up",
            },
            "port": self.port_var.get(),
            "baud": self.baud_var.get(),
            "mouse_profile_name": self.current_mouse_profile_var.get(),
            "safe_z": self.safe_z_var.get(),
            "mouth": [self.mouth_x_var.get(), self.mouth_y_var.get(), self.mouth_z_var.get()],
            "reference": [self.ref_x_var.get(), self.ref_y_var.get(), self.ref_z_var.get()],
            "dock": [self.dock_x_var.get(), self.dock_y_var.get(), self.dock_z_var.get()],
            "reward_ms": self.reward_ms_var.get(),
            "reward_ul": self.reward_ul_var.get(),
            "water_limit_ul": self.water_limit_ul_var.get(),
            "reward_cal_pulses": self.reward_cal_pulses_var.get(),
            "reward_mode": self.reward_mode_var.get(),
            "auto_reward_delay": self.auto_reward_delay_var.get(),
            "auto_hold_after_miss_enabled": self.auto_hold_after_miss_enabled_var.get(),
            "auto_hold_after_miss_threshold": self.auto_hold_after_miss_threshold_var.get(),
            "cue": {
                "hz": self.cue_hz_var.get(),
                "duration_ms": self.cue_duration_var.get(),
                "volume_pct": self.cue_volume_var.get(),
            },
            "enforce": self.enforce_var.get(),
            "timing": {
                "settle": self.settle_ms_var.get(),
                "posthold": self.posthold_ms_var.get(),
                "precue_min": self.precue_min_var.get(),
                "precue_max": self.precue_max_var.get(),
                "response_window": self.response_window_var.get(),
                "iti_min": self.iti_min_var.get(),
                "iti_jitter": self.iti_jitter_var.get(),
                "sync_pulse_min": self.sync_pulse_min_var.get(),
                "sync_pulse_max": self.sync_pulse_max_var.get(),
                "sync_interval_min": self.sync_interval_min_var.get(),
                "sync_interval_max": self.sync_interval_max_var.get(),
                "block_size_min": self.block_size_min_var.get(),
                "block_size_max": self.block_size_max_var.get(),
                "target_trials_enabled": self.target_trials_enabled_var.get(),
                "target_trials_per_position": self.target_trials_per_pos_var.get(),
                "max_duration_enabled": self.max_duration_enabled_var.get(),
                "max_duration_min": self.max_duration_min_var.get(),
                "scheduling_mode": self.scheduling_mode_var.get(),
                "stop_mode": self.stop_mode_var.get(),
            },
            "geometry": {
                "dist_close": self.dist_close_var.get(),
                "dist_far": self.dist_far_var.get(),
                "az_center": self.az_center_var.get(),
                "az_left": self.az_left_var.get(),
                "az_right": self.az_right_var.get(),
                "down_angle": self.down_angle_var.get(),
                "head_roll": self.head_roll_var.get(),
                "enabled": [v.get() for v in self.pos_enabled_vars],
                "position_names": [v.get() for v in self.position_name_vars],
            },
            "adaptive": {
                "enabled": self.adaptive_enabled_var.get(),
                "use_per_position": self.adapt_use_per_position_var.get(),
                "hits": self.adapt_hits_var.get(),
                "misses": self.adapt_misses_var.get(),
                "step": self.adapt_step_var.get(),
                "step_down": self.adapt_step_down_var.get(),
                "min": self.adapt_min_var.get(),
                "max": self.adapt_max_var.get(),
                "positions": [
                    {
                        "enabled": self.adapt_pos_enabled_vars[i].get(),
                        "hits": self.adapt_pos_hits_vars[i].get(),
                        "misses": self.adapt_pos_misses_vars[i].get(),
                        "step": self.adapt_pos_step_vars[i].get(),
                        "step_down": self.adapt_pos_step_down_vars[i].get(),
                        "min": self.adapt_pos_min_vars[i].get(),
                        "max": self.adapt_pos_max_vars[i].get(),
                    }
                    for i in range(6)
                ],
                "free_enabled": self.free_reward_enabled_var.get(),
                "free_after": self.free_after_misses_var.get(),
                "free_delay": self.free_delay_var.get(),
            },
            "lick": {
                "threshold": self.lick_thresh_var.get(),
                "hysteresis": self.lick_hyst_var.get(),
                "polarity": self.lick_polarity_var.get(),
                "alpha": self.lick_alpha_var.get(),
                "refractory": self.lick_refract_var.get(),
                "debug": self.lick_debug_var.get(),
            },
            "axes": {
                axis: {
                    "ms_pos": self.axis_cal[axis]["ms_pos"].get(),
                    "ms_neg": self.axis_cal[axis]["ms_neg"].get(),
                    "overhead": self.axis_cal[axis]["overhead"].get(),
                    "cw_pos": self.axis_cal[axis]["cw_pos"].get(),
                }
                for axis in ("X", "Y", "Z")
            },
            "smc": {
                "stage_label": self.stage_label_var.get(),
                "step_angle": self.step_angle_var.get(),
                "microstep": self.microstep_var.get(),
                "lead": self.screw_lead_var.get(),
                "rpm_x": self.rpm_x_var.get(),
                "rpm_y": self.rpm_y_var.get(),
                "rpm_z": self.rpm_z_var.get(),
                "accel": self.accel_text_var.get(),
                "mode": self.mode_text_var.get(),
            },
            "visualization": {
                "monitor_pos": self.monitor_pos_var.get(),
                "sequence_dwell_ms": self.sequence_dwell_var.get(),
                "sequence_cycles": self.sequence_cycles_var.get(),
                "sequence_with_cue": self.sequence_with_cue_var.get(),
                "auto_status_poll": self.autopoll_var.get(),
                "auto_status_poll_interval_s": self.autopoll_interval_var.get(),
            },
            "logging": {
                "save_dir": self.save_dir_var.get(),
                "prefix": self.session_prefix_var.get(),
                "auto_on_start": self.auto_log_on_start_var.get(),
                "raw_protocol_log_enabled": self.raw_protocol_log_var.get(),
                "timeseries_enabled": self.timeseries_enabled_var.get(),
                "timeseries_interval_ms": self.timeseries_interval_var.get_int(self._timeseries_sample_ms or 20),
            },
        }

    def _negate_number_like(self, value):
        try:
            f = float(value)
        except Exception:
            return value
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return value
            if "." in s or "e" in s.lower():
                return str(-f)
            return str(int(round(-f)))
        return -f

    def _migrate_legacy_z_positive_down_cfg(self, cfg):
        if not isinstance(cfg, dict):
            return cfg
        conv = cfg.get("coordinate_convention", {})
        if isinstance(conv, dict) and conv.get("z_positive") == "up":
            return cfg
        migrated = json.loads(json.dumps(cfg))
        if "safe_z" in migrated:
            migrated["safe_z"] = self._negate_number_like(migrated.get("safe_z"))
        for key in ("mouth", "reference", "dock"):
            vals = migrated.get(key)
            if isinstance(vals, list) and len(vals) == 3:
                vals[2] = self._negate_number_like(vals[2])
        axes = migrated.get("axes")
        if isinstance(axes, dict):
            z_axis = axes.get("Z")
            if isinstance(z_axis, dict):
                ms_pos = z_axis.get("ms_pos")
                ms_neg = z_axis.get("ms_neg")
                z_axis["ms_pos"] = ms_neg if ms_neg is not None else z_axis.get("ms_pos")
                z_axis["ms_neg"] = ms_pos if ms_pos is not None else z_axis.get("ms_neg")
                if "cw_pos" in z_axis:
                    z_axis["cw_pos"] = not bool(z_axis.get("cw_pos"))
        migrated["coordinate_convention"] = {"z_positive": "up"}
        return migrated

    def _apply_config_dict(self, cfg):
        cfg = self._migrate_legacy_z_positive_down_cfg(cfg)
        self.port_var.set(cfg.get("port", self.port_var.get()))
        self.baud_var.set(cfg.get("baud", self.baud_var.get()))
        self.current_mouse_profile_var.set(cfg.get("mouse_profile_name", self.current_mouse_profile_var.get()))
        self.safe_z_var.set(cfg.get("safe_z", self.safe_z_var.get()))

        mouth = cfg.get("mouth", [])
        if len(mouth) == 3:
            self.mouth_x_var.set(mouth[0]); self.mouth_y_var.set(mouth[1]); self.mouth_z_var.set(mouth[2])
        ref = cfg.get("reference", [])
        if len(ref) == 3:
            self.ref_x_var.set(ref[0]); self.ref_y_var.set(ref[1]); self.ref_z_var.set(ref[2])
        dock = cfg.get("dock", [])
        if len(dock) == 3:
            self.dock_x_var.set(dock[0]); self.dock_y_var.set(dock[1]); self.dock_z_var.set(dock[2])

        self.reward_ms_var.set(cfg.get("reward_ms", self.reward_ms_var.get()))
        self.reward_ul_var.set(cfg.get("reward_ul", self.reward_ul_var.get()))
        self.water_limit_ul_var.set(cfg.get("water_limit_ul", self.water_limit_ul_var.get()))
        self.reward_cal_pulses_var.set(cfg.get("reward_cal_pulses", self.reward_cal_pulses_var.get()))
        self.reward_mode_var.set(cfg.get("reward_mode", self.reward_mode_var.get()))
        self.auto_reward_delay_var.set(cfg.get("auto_reward_delay", self.auto_reward_delay_var.get()))
        self.auto_hold_after_miss_enabled_var.set(cfg.get("auto_hold_after_miss_enabled", self.auto_hold_after_miss_enabled_var.get()))
        self.auto_hold_after_miss_threshold_var.set(cfg.get("auto_hold_after_miss_threshold", self.auto_hold_after_miss_threshold_var.get()))
        cue = cfg.get("cue", {})
        self.cue_hz_var.set(cue.get("hz", self.cue_hz_var.get()))
        self.cue_duration_var.set(cue.get("duration_ms", self.cue_duration_var.get()))
        self.cue_volume_var.set(cue.get("volume_pct", self.cue_volume_var.get()))
        self.enforce_var.set(cfg.get("enforce", self.enforce_var.get()))

        t = cfg.get("timing", {})
        self.settle_ms_var.set(t.get("settle", self.settle_ms_var.get()))
        self.posthold_ms_var.set(t.get("posthold", self.posthold_ms_var.get()))
        self.precue_min_var.set(t.get("precue_min", self.precue_min_var.get()))
        self.precue_max_var.set(t.get("precue_max", self.precue_max_var.get()))
        self.response_window_var.set(t.get("response_window", self.response_window_var.get()))
        self.iti_min_var.set(t.get("iti_min", self.iti_min_var.get()))
        self.iti_jitter_var.set(t.get("iti_jitter", self.iti_jitter_var.get()))
        self.sync_pulse_min_var.set(t.get("sync_pulse_min", self.sync_pulse_min_var.get()))
        self.sync_pulse_max_var.set(t.get("sync_pulse_max", self.sync_pulse_max_var.get()))
        self.sync_interval_min_var.set(t.get("sync_interval_min", self.sync_interval_min_var.get()))
        self.sync_interval_max_var.set(t.get("sync_interval_max", self.sync_interval_max_var.get()))
        legacy_block = t.get("block_size", self.block_size_var.get())
        block_min_loaded = t.get("block_size_min", legacy_block)
        self.block_size_var.set(block_min_loaded)
        self.block_size_min_var.set(block_min_loaded)
        self.block_size_max_var.set(t.get("block_size_max", block_min_loaded))
        self.target_trials_enabled_var.set(t.get("target_trials_enabled", self.target_trials_enabled_var.get()))
        self.target_trials_per_pos_var.set(t.get("target_trials_per_position", self.target_trials_per_pos_var.get()))
        self.max_duration_enabled_var.set(t.get("max_duration_enabled", self.max_duration_enabled_var.get()))
        self.max_duration_min_var.set(t.get("max_duration_min", self.max_duration_min_var.get()))
        self.scheduling_mode_var.set(t.get("scheduling_mode", self.scheduling_mode_var.get()))
        self.stop_mode_var.set(t.get("stop_mode", self.stop_mode_var.get()))

        g = cfg.get("geometry", {})
        self.dist_close_var.set(g.get("dist_close", self.dist_close_var.get()))
        self.dist_far_var.set(g.get("dist_far", self.dist_far_var.get()))
        self.az_center_var.set(g.get("az_center", self.az_center_var.get()))
        self.az_left_var.set(g.get("az_left", self.az_left_var.get()))
        self.az_right_var.set(g.get("az_right", self.az_right_var.get()))
        self.down_angle_var.set(g.get("down_angle", self.down_angle_var.get()))
        self.head_roll_var.set(g.get("head_roll", self.head_roll_var.get()))
        enabled = g.get("enabled")
        if isinstance(enabled, list) and len(enabled) == 6:
            for i, v in enumerate(enabled):
                self.pos_enabled_vars[i].set(bool(v))
        names = g.get("position_names")
        if isinstance(names, list) and len(names) == 6:
            for i, nm in enumerate(names):
                self.position_name_vars[i].set(str(nm))
        self._refresh_position_names()

        a = cfg.get("adaptive", {})
        self.adaptive_enabled_var.set(a.get("enabled", self.adaptive_enabled_var.get()))
        self.adapt_use_per_position_var.set(a.get("use_per_position", self.adapt_use_per_position_var.get()))
        self.adapt_hits_var.set(a.get("hits", self.adapt_hits_var.get()))
        self.adapt_misses_var.set(a.get("misses", self.adapt_misses_var.get()))
        self.adapt_step_var.set(a.get("step", self.adapt_step_var.get()))
        self.adapt_step_down_var.set(a.get("step_down", self.adapt_step_down_var.get()))
        self.adapt_min_var.set(a.get("min", self.adapt_min_var.get()))
        self.adapt_max_var.set(a.get("max", self.adapt_max_var.get()))
        pos_adapt = a.get("positions", [])
        for i in range(6):
            pdata = pos_adapt[i] if i < len(pos_adapt) and isinstance(pos_adapt[i], dict) else {}
            self.adapt_pos_enabled_vars[i].set(pdata.get("enabled", self.adaptive_enabled_var.get()))
            self.adapt_pos_hits_vars[i].set(pdata.get("hits", self.adapt_hits_var.get()))
            self.adapt_pos_misses_vars[i].set(pdata.get("misses", self.adapt_misses_var.get()))
            self.adapt_pos_step_vars[i].set(pdata.get("step", self.adapt_step_var.get()))
            self.adapt_pos_step_down_vars[i].set(pdata.get("step_down", self.adapt_step_down_var.get()))
            self.adapt_pos_min_vars[i].set(pdata.get("min", self.adapt_min_var.get()))
            self.adapt_pos_max_vars[i].set(pdata.get("max", self.adapt_max_var.get()))
        if not pos_adapt:
            self._sync_all_adaptive_positions_from_global()
        self._load_selected_adaptive_position_into_editor()
        self._refresh_adaptive_ui_state()
        self.free_reward_enabled_var.set(a.get("free_enabled", self.free_reward_enabled_var.get()))
        self.free_after_misses_var.set(a.get("free_after", self.free_after_misses_var.get()))
        self.free_delay_var.set(a.get("free_delay", self.free_delay_var.get()))

        l = cfg.get("lick", {})
        self.lick_thresh_var.set(l.get("threshold", self.lick_thresh_var.get()))
        self.lick_hyst_var.set(l.get("hysteresis", self.lick_hyst_var.get()))
        self.lick_polarity_var.set(l.get("polarity", self.lick_polarity_var.get()))
        self.lick_alpha_var.set(l.get("alpha", self.lick_alpha_var.get()))
        self.lick_refract_var.set(l.get("refractory", self.lick_refract_var.get()))
        self.lick_debug_var.set(l.get("debug", self.lick_debug_var.get()))

        axes = cfg.get("axes", {})
        for axis in ("X", "Y", "Z"):
            d = axes.get(axis, {})
            self.axis_cal[axis]["ms_pos"].set(d.get("ms_pos", self.axis_cal[axis]["ms_pos"].get()))
            self.axis_cal[axis]["ms_neg"].set(d.get("ms_neg", self.axis_cal[axis]["ms_neg"].get()))
            self.axis_cal[axis]["overhead"].set(d.get("overhead", self.axis_cal[axis]["overhead"].get()))
            self.axis_cal[axis]["cw_pos"].set(d.get("cw_pos", self.axis_cal[axis]["cw_pos"].get()))

        s = cfg.get("smc", {})
        self.stage_label_var.set(s.get("stage_label", self.stage_label_var.get()))
        self.step_angle_var.set(s.get("step_angle", self.step_angle_var.get()))
        self.microstep_var.set(s.get("microstep", self.microstep_var.get()))
        self.screw_lead_var.set(s.get("lead", self.screw_lead_var.get()))
        self.rpm_x_var.set(s.get("rpm_x", self.rpm_x_var.get()))
        self.rpm_y_var.set(s.get("rpm_y", self.rpm_y_var.get()))
        self.rpm_z_var.set(s.get("rpm_z", self.rpm_z_var.get()))
        self.accel_text_var.set(s.get("accel", self.accel_text_var.get()))
        self.mode_text_var.set(s.get("mode", self.mode_text_var.get()))

        viscfg = cfg.get("visualization", {})
        self.monitor_pos_var.set(viscfg.get("monitor_pos", self.monitor_pos_var.get()))
        self.sequence_dwell_var.set(viscfg.get("sequence_dwell_ms", self.sequence_dwell_var.get()))
        self.sequence_cycles_var.set(viscfg.get("sequence_cycles", self.sequence_cycles_var.get()))
        self.sequence_with_cue_var.set(viscfg.get("sequence_with_cue", self.sequence_with_cue_var.get()))
        self.autopoll_var.set(viscfg.get("auto_status_poll", self.autopoll_var.get()))
        self.autopoll_interval_var.set(viscfg.get("auto_status_poll_interval_s", self.autopoll_interval_var.get()))

        logcfg = cfg.get("logging", {})
        self.save_dir_var.set(logcfg.get("save_dir", self.save_dir_var.get()))
        self.session_prefix_var.set(logcfg.get("prefix", self.session_prefix_var.get()))
        self.auto_log_on_start_var.set(logcfg.get("auto_on_start", self.auto_log_on_start_var.get()))
        self.raw_protocol_log_var.set(logcfg.get("raw_protocol_log_enabled", self.raw_protocol_log_var.get()))
        self.timeseries_enabled_var.set(logcfg.get("timeseries_enabled", self.timeseries_enabled_var.get()))
        self.timeseries_interval_var.set(logcfg.get("timeseries_interval_ms", self.timeseries_interval_var.get()))
        self._timeseries_sample_ms = max(1, self.timeseries_interval_var.get_int(self._timeseries_sample_ms or 20))

        self._recompute_geometry_preview()
        self._recompute_stepper_calculator()
        self._refresh_position_stats_tree()

    def _save_config_to_path(self, path):
        path = Path(path)
        path.write_text(json.dumps(self._config_dict(), indent=2))
        self._log_local(f"[GUI] Saved config to {path}")

    def _set_current_mouse_profile(self, path_or_name):
        if isinstance(path_or_name, Path):
            name = path_or_name.stem
        else:
            raw = str(path_or_name).strip()
            if not raw:
                name = "default"
            else:
                pathish = Path(raw)
                if pathish.suffix.lower() == ".json" or ("\\" in raw) or ("/" in raw):
                    name = pathish.stem or raw
                else:
                    name = raw
        self.current_mouse_profile_var.set(name)

    def save_mouse_profile(self):
        try:
            MOUSE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            initial_name = self.current_mouse_profile_var.get().strip() or "mouse_profile"
            path = filedialog.asksaveasfilename(
                title="Save mouse profile as",
                initialdir=str(MOUSE_PROFILE_DIR),
                initialfile=f"{initial_name}.json",
                defaultextension=".json",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            )
            if not path:
                return
            path = Path(path)
            if path.exists():
                if not messagebox.askyesno(APP_TITLE, f"Overwrite existing mouse profile?\n\n{path}"):
                    return
            payload = self._config_dict()
            payload["mouse_profile_name"] = path.stem
            path.write_text(json.dumps(payload, indent=2))
            self._set_current_mouse_profile(path)
            self._log_local(f"[GUI] Saved mouse profile to {path}")
            messagebox.showinfo(APP_TITLE, f"Saved mouse profile:\n{path}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not save mouse profile:\n{e}")

    def load_mouse_profile(self):
        try:
            MOUSE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            path = filedialog.askopenfilename(
                title="Load mouse profile",
                initialdir=str(MOUSE_PROFILE_DIR),
                filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            )
            if not path:
                return
            path = Path(path)
            data = json.loads(path.read_text())
            self._apply_config_dict(data)
            self._set_current_mouse_profile(path)
            self._log_local(f"[GUI] Loaded mouse profile from {path}")
            messagebox.showinfo(APP_TITLE, f"Loaded mouse profile:\n{path}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not load mouse profile:\n{e}")

    def save_config(self):
        try:
            self._save_config_to_path(CONFIG_PATH)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Save failed:\n{e}")

    def save_config_as(self):
        path = filedialog.asksaveasfilename(
            title="Save GUI config as",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialfile="spout_task_config.json",
        )
        if not path:
            return
        try:
            self._save_config_to_path(path)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Save As failed:\n{e}")

    def _load_config_if_exists(self):
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text())
                self._apply_config_dict(data)
        except Exception:
            pass
        # Keep auto STATUS polling off on startup so connection handshakes are not interrupted.
        try:
            self.autopoll_var.set(False)
        except Exception:
            pass

    def _current_positions_snapshot(self):
        positions = {}
        try:
            # Use live preview values as a reasonable snapshot for logging.
            az_vals = [
                self.az_center_var.get_float(),
                self.az_left_var.get_float(),
                self.az_right_var.get_float(),
            ]
            labels = []
            for i, pv in enumerate(self.position_preview_vars):
                xyz_text = pv["xyz"].get().strip().strip("()")
                x = y = z = None
                try:
                    x_str, y_str, z_str = [s.strip() for s in xyz_text.split(",")]
                    x, y, z = float(x_str), float(y_str), float(z_str)
                except Exception:
                    x = y = z = ""
                positions[i] = {
                    "name": self.position_labels[i] if i < len(self.position_labels) else pv["label"].get(),
                    "label": pv["label"].get(),
                    "x_mm": x,
                    "y_mm": y,
                    "z_mm": z,
                    "dist_mm": pv["dist"].get().replace(" mm", ""),
                    "down_deg": pv["down"].get().replace("°", ""),
                    "az_deg": az_vals[i % 3] if i < 6 else "",
                }
        except Exception:
            pass
        return positions


    def _current_backend_name(self):
        try:
            return self.backend_var.get()
        except Exception:
            return "unknown"

    def _session_manifest_payload(self, gui_cfg: dict):
        return {
            "app_title": APP_TITLE,
            "backend": self._current_backend_name(),
            "gui_file": Path(__file__).name,
            "notes": self.session_notes_var.get().strip(),
            "device_snapshot": {
                "latest_status": dict(self.latest_status) if isinstance(self.latest_status, dict) else {},
                "config_cache": dict(self.device_config_cache),
            },
            "initial_positions_snapshot": self._current_positions_snapshot(),
            "logging": {
                "auto_on_start": self.auto_log_on_start_var.get(),
                "timeseries_enabled": self.timeseries_enabled_var.get(),
                "timeseries_interval_ms": self.timeseries_interval_var.get_int(20),
                "raw_protocol_log_enabled": self.raw_protocol_log_var.get(),
            },
            "hardware_alignment_channels": {
                "sync": "digital onset pulses",
                "cue": "digital onset pulses",
                "reward": "digital onset pulses",
                "lick": "continuous digital state mirror",
                "position_code": "binary position code plus strobe",
            },
            "gui_config_embedded": gui_cfg,
        }

    def _position_stats_rows(self):
        rows = []
        snapshot = self._current_positions_snapshot()
        adapt_enabled_global = bool(self.adaptive_enabled_var.get())
        use_per_position = bool(self.adapt_use_per_position_var.get())
        for i in range(6):
            data = dict(self.position_stats.get(i, {}) or {})
            trials = int(float(data.get("trials", 0) or 0)) if str(data.get("trials", 0)).strip() != "" else 0
            hits = int(float(data.get("hits", 0) or 0)) if str(data.get("hits", 0)).strip() != "" else 0
            misses = int(float(data.get("misses", 0) or 0)) if str(data.get("misses", 0)).strip() != "" else 0
            free_rewards = int(float(data.get("free_rewards", 0) or 0)) if str(data.get("free_rewards", 0)).strip() != "" else 0
            hit_rate = (hits / trials) if trials > 0 else ""
            pos = snapshot.get(i, {})
            adapt_enabled = adapt_enabled_global and ((not use_per_position) or bool(self.adapt_pos_enabled_vars[i].get()))
            rows.append({
                "pos_idx": i,
                "pos_name": self.position_labels[i] if i < len(self.position_labels) else pos.get("label", f"Pos {i}"),
                "enabled": data.get("enabled", 1 if self.pos_enabled_vars[i].get() else 0),
                "dist_mm": data.get("dist_mm", pos.get("dist_mm", "")),
                "trials": trials,
                "hits": hits,
                "misses": misses,
                "free_rewards": free_rewards,
                "adaptive_hit_counter": data.get("adaptive_hit_counter", "") if adapt_enabled else "",
                "hit_rate": f"{hit_rate:.4f}" if isinstance(hit_rate, float) else "",
            })
        return rows

    def _summary_end_dict(self):
        latest = dict(self.latest_status) if isinstance(self.latest_status, dict) else {}
        summary = dict(getattr(self, "summary_stats", {}) or {})
        counts = dict(getattr(self.session_logger, "event_counts", {}) or {})

        def pick(*keys, default=""):
            for src in (latest, summary):
                for key in keys:
                    if key in src and src.get(key, "") != "":
                        return src.get(key)
            return default

        return {
            "backend": self._current_backend_name(),
            "session_id": self.session_logger.session_dir.name if self.session_logger.session_dir else "",
            "total_trials": pick("total_trials"),
            "hits": pick("hits"),
            "misses": pick("misses"),
            "free_rewards": pick("free_rewards"),
            "auto_rewards": pick("auto_rewards"),
            "total_rewards": pick("total_rewards"),
            "water_ul": pick("water_ul"),
            "water_limit_ul": pick("water_limit_ul"),
            "reward_mode": pick("reward_mode"),
            "sync_count": pick("sync_count"),
            "manual_rewards": counts.get("manual_reward", 0),
            "cue_events": counts.get("cue", 0),
            "cue_only_events": counts.get("cue_only", 0),
            "reward_events": counts.get("reward", 0),
            "lick_on_events": counts.get("lick_on", 0),
            "lick_off_events": counts.get("lick_off", 0),
            "session_running_at_stop": pick("run"),
            "final_state": pick("state"),
            "notes": self.session_notes_var.get().strip(),
        }

    def start_task(self):
        if getattr(self, "_command_batch_active", False):
            messagebox.showinfo(APP_TITLE, "A settings batch is still being sent to the device. Please wait for it to finish before starting the task.")
            return
        if getattr(self, "_pending_command_batches", None):
            if len(self._pending_command_batches) > 0:
                messagebox.showinfo(APP_TITLE, "A queued device batch is still pending. Please wait for it to finish before starting the task.")
                return
        if getattr(self, "_adaptive_verify_after_id", None):
            messagebox.showinfo(APP_TITLE, "Adaptive scope verification is still in progress. Please wait for it to finish before starting the task.")
            return
        if self._validate_geometry_settings(show_dialog=True):
            return
        missing = sorted(self._required_apply_categories() - self._applied_categories_since_connect)
        if missing:
            msg = (
                "These settings have not been pushed to the device since the most recent connect:\n\n"
                + ", ".join(missing)
                + "\n\nPress Yes to START anyway, or No to cancel and use 'Apply ALL session/task settings' first."
            )
            if not messagebox.askyesno(APP_TITLE, msg):
                return
        if self.auto_log_on_start_var.get():
            if self.session_logger.active:
                self._log_local(f"[GUI] Finalizing active log before START: {self.session_logger.session_dir}")
                self.stop_session_logging()
            if not self.session_logger.active:
                self.start_session_logging(auto_started=True)
        self._clear_session_visual_history()
        self._start_requested_wall_unix = time.time()
        self._awaiting_run_confirmation = True
        self._task_start_wall_unix = None
        self._session_timeline_start_unix = None
        self._session_timeline_end_unix = None
        self._last_status_run = False
        self.visual_elapsed_var.set("Time since task start: -- min")
        self.send("START")

    def fetch_device_state(self):
        cmds = ["GET kind=config", "GET kind=positions", "GET kind=stats", "GET kind=status"]
        if self._current_backend_name() == "mega_zaber":
            cmds.insert(1, "GET kind=zaberconfig")
        self._queue_commands(cmds, delay_ms=220, final_status="Fetched device state")

    def browse_save_dir(self):
        path = filedialog.askdirectory(
            title="Choose session save directory",
            initialdir=self.save_dir_var.get() or str(SESSION_ROOT_DEFAULT),
        )
        if path:
            self.save_dir_var.set(path)

    def start_session_logging(self, auto_started=False):
        if self.session_logger.active:
            self._log_local(f"[GUI] Finalizing active log before starting a new log: {self.session_logger.session_dir}")
            self.stop_session_logging()
            if self.session_logger.active:
                return
        try:
            self._timeseries_sample_ms = max(1, self.timeseries_interval_var.get_int(self._timeseries_sample_ms or 20))
            gui_cfg = self._config_dict()
            session_dir = self.session_logger.start(
                base_dir=self.save_dir_var.get(),
                prefix=self.session_prefix_var.get(),
                gui_config=gui_cfg,
                manifest_payload=self._session_manifest_payload(gui_cfg),
                raw_log_enabled=self.raw_protocol_log_var.get(),
                timeseries_enabled=self.timeseries_enabled_var.get(),
                timeseries_interval_ms=self._timeseries_sample_ms,
            )
            self._logging_auto_started = bool(auto_started)
            self.logging_state_var.set(f"Logging: on ({session_dir.name})")
            self._log_local(f"[GUI] Session logging started: {session_dir}")
            if not auto_started:
                self.fetch_device_state()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not start session logging:\n{e}")

    def stop_session_logging(self):
        if not self.session_logger.active:
            self._logging_auto_started = False
            self.logging_state_var.set("Logging: off")
            return
        session_dir = self.session_logger.session_dir
        try:
            summary_end = self._summary_end_dict()
            position_stats_rows = self._position_stats_rows()
            final_manifest_updates = {
                "backend": self._current_backend_name(),
                "notes": self.session_notes_var.get().strip(),
                "final_position_stats_rows": position_stats_rows,
            }
            self.session_logger.stop(
                summary_end=summary_end,
                position_stats_rows=position_stats_rows,
                final_manifest_updates=final_manifest_updates,
                final_gui_config=self._config_dict(),
                final_device_snapshot={
                    "latest_status": dict(self.latest_status) if isinstance(self.latest_status, dict) else {},
                    "config_cache": dict(self.device_config_cache),
                },
            )
            self._logging_auto_started = False
            self.logging_state_var.set("Logging: off")
            self._log_local(f"[GUI] Session logging stopped: {session_dir}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not stop session logging:\n{e}")

    def load_config_dialog(self):
        path = filedialog.askopenfilename(
            title="Load GUI config",
            filetypes=[("JSON", "*.json"), ("All files", "*")],
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text())
            self._apply_config_dict(data)
            self._log_local(f"[GUI] Loaded config from {path}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Load failed:\n{e}")

    def load_motion_coords_from_config_dialog(self):
        path = filedialog.askopenfilename(
            title="Load mouth / dock / SAFE Z from config",
            filetypes=[("JSON", "*.json"), ("All files", "*")],
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text())
            self._apply_motion_coords_from_config_dict(data)
            # For a coords-only load, the operator picked a specific file on purpose.
            # Show that file's stem in the Mouse profile area even if the JSON carries
            # an older embedded mouse_profile_name from a previous save.
            self._set_current_mouse_profile(Path(path).stem)
            self._log_local(f"[GUI] Loaded mouth/dock/safe_z from config {path}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Load motion coordinates failed:\n{e}")

    def _apply_motion_coords_from_config_dict(self, cfg):
        cfg = self._migrate_legacy_z_positive_down_cfg(cfg)
        self.safe_z_var.set(cfg.get("safe_z", self.safe_z_var.get()))
        mouth = cfg.get("mouth", [])
        if len(mouth) == 3:
            self.mouth_x_var.set(mouth[0]); self.mouth_y_var.set(mouth[1]); self.mouth_z_var.set(mouth[2])
        dock = cfg.get("dock", [])
        if len(dock) == 3:
            self.dock_x_var.set(dock[0]); self.dock_y_var.set(dock[1]); self.dock_z_var.set(dock[2])
        self.mouth_sync_var.set("MOUTH: GUI changed / not applied")
        self.dock_sync_var.set("DOCK: GUI changed / not applied")
        self.safez_sync_var.set("SAFE Z: GUI changed / not applied")
        self._recompute_geometry_preview()

    def on_close(self):
        try:
            self.stop_position_sequence(silent=True)
        except Exception:
            pass
        try:
            self.save_config()
        except Exception:
            pass
        try:
            self.stop_session_logging()
        except Exception:
            pass
        self.client.disconnect()
        self.destroy()


class App(BaseApp):
    def __init__(self):
        super().__init__()
        self.backend_var = tk.StringVar(master=self, value="teensy_smc02")
        self.teensy_io_side_var = tk.StringVar(master=self, value="left")  # legacy config placeholder; fixed-left mapping in current Teensy firmware
        self.zaber_x_device_var = NumberVar(master=self, value="1")
        self.zaber_y_device_var = NumberVar(master=self, value="1")
        self.zaber_z_device_var = NumberVar(master=self, value="1")
        self.zaber_x_axis_var = NumberVar(master=self, value="2")
        self.zaber_y_axis_var = NumberVar(master=self, value="1")
        self.zaber_z_axis_var = NumberVar(master=self, value="3")
        self.zaber_mapping_unlock_var = tk.BooleanVar(master=self, value=False)
        self.zaber_x_upm_var = NumberVar(master=self, value="5249.34")
        self.zaber_y_upm_var = NumberVar(master=self, value="5249.34")
        self.zaber_z_upm_var = NumberVar(master=self, value="5249.34")
        self.zaber_test_device_var = NumberVar(master=self, value="1")
        self.zaber_test_axis_var = NumberVar(master=self, value="1")
        self.zaber_test_mm_var = NumberVar(master=self, value="1.0")
        self.zaber_status_var = tk.StringVar(master=self, value="Backend/Zaber: not configured")
        self.title(APP_TITLE)
        self._add_backend_zaber_tab()
        self.backend_var.trace_add("write", lambda *args: self._on_backend_changed())
        self.port_var.trace_add("write", lambda *args: self._update_port_info())
        self._on_backend_changed()

    def _find_notebook(self):
        def walk(w):
            if isinstance(w, ttk.Notebook):
                return w
            for c in w.winfo_children():
                r = walk(c)
                if r is not None:
                    return r
            return None
        return walk(self)

    def _add_backend_zaber_tab(self):
        nb = self._find_notebook()
        if nb is None:
            return
        self.tab_backend = ttk.Frame(nb, padding=0)
        nb.insert(nb.index('end')-1, self.tab_backend, text="6. Backend")
        self.backend_scroll_host, self.tab_backend_inner = self._make_scrollable_container(self.tab_backend)
        self.backend_scroll_host.grid(row=0, column=0, sticky="nsew")
        self.tab_backend.rowconfigure(0, weight=1)
        self.tab_backend.columnconfigure(0, weight=1)
        try:
            nb.tab(self.tab_console, text="7. Console")
        except Exception:
            pass
        self._build_backend_tab()

    def _build_backend_tab(self):
        f = getattr(self, 'tab_backend_inner', self.tab_backend)
        for c in range(2):
            f.columnconfigure(c, weight=1)

        sel = ttk.LabelFrame(f, text="Backend selector", padding=8)
        sel.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=4, pady=4)
        ttk.Label(sel, text="Backend").grid(row=0, column=0, sticky="w")
        ttk.Combobox(sel, textvariable=self.backend_var, state="readonly", width=24,
                     values=["teensy_smc02", "mega_zaber"]).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Button(sel, text="Use selected backend", command=self.apply_backend_selection).grid(row=0, column=2, padx=4)
        self.backend_note_var = tk.StringVar(value="")
        ttk.Label(sel, textvariable=self.backend_note_var, wraplength=720, justify="left").grid(row=1, column=0, columnspan=3, sticky="w", pady=(6,0))


        tio = ttk.LabelFrame(f, text="Teensy breakout-board I/O", padding=8)
        tio.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.backend_teensy_io_frame = tio
        ttk.Label(
            tio,
            text=("Current Teensy firmware uses a fixed single-side mapping. "
                  "Lick input = pin15, solenoid = pin5, reward TTL = pin19, sync = pin2, audio PWM = pin3, "
                  "session start/stop TTL = pin4, and trial-start TTL = pin6. There is no left/right selector in the current Teensy build."),
            wraplength=430,
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        za = ttk.LabelFrame(f, text="Mega + Zaber device mapping", padding=8)
        za.grid(row=1, column=1, sticky="nsew", padx=4, pady=4)
        self.backend_zaber_frame = za
        ttk.Label(za, text="Axis").grid(row=0, column=0, sticky="w")
        ttk.Label(za, text="Device ID").grid(row=0, column=1, sticky="w")
        ttk.Label(za, text="Axis #").grid(row=0, column=2, sticky="w")
        ttk.Label(za, text="Units / mm").grid(row=0, column=3, sticky="w")
        ttk.Label(za, text="Apply").grid(row=0, column=4, sticky="w")
        ttk.Label(za, text="Notes").grid(row=0, column=5, sticky="w")
        ttk.Checkbutton(
            za, text="Unlock mapping edits", variable=self.zaber_mapping_unlock_var,
            command=self._refresh_zaber_mapping_edit_state
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2,4))
        rows = [
            ("X", self.zaber_x_device_var, self.zaber_x_axis_var, self.zaber_x_upm_var),
            ("Y", self.zaber_y_device_var, self.zaber_y_axis_var, self.zaber_y_upm_var),
            ("Z", self.zaber_z_device_var, self.zaber_z_axis_var, self.zaber_z_upm_var),
        ]
        notes = {
            "X":"Your confirmed mapping: X=device 1 axis 2",
            "Y":"Your confirmed mapping: Y=device 1 axis 1",
            "Z":"Your confirmed mapping: Z=device 1 axis 3",
        }
        self.zaber_mapping_entries = []
        for r,(axis,dv,av,upm) in enumerate(rows, start=2):
            ttk.Label(za, text=axis).grid(row=r, column=0, sticky="w")
            dev_entry = ttk.Entry(za, textvariable=dv, width=8)
            dev_entry.grid(row=r, column=1, sticky="w", padx=4)
            axis_entry = ttk.Entry(za, textvariable=av, width=8)
            axis_entry.grid(row=r, column=2, sticky="w", padx=4)
            upm_entry = ttk.Entry(za, textvariable=upm, width=12)
            upm_entry.grid(row=r, column=3, sticky="w", padx=4)
            self.zaber_mapping_entries.extend([dev_entry, axis_entry])
            ttk.Button(za, text=f"Apply {axis} scale", command=lambda a=axis: self.apply_zaber_axis_settings(a)).grid(row=r, column=4, sticky="ew", padx=(4,8))
            ttk.Label(za, text=notes[axis]).grid(row=r, column=5, sticky="w")
        ttk.Button(za, text="Apply ALL Zaber scales (staggered)", command=self.apply_zaber_settings).grid(row=5, column=0, columnspan=6, sticky="ew", pady=(8,0))
        ttk.Button(za, text="Reset scales to 5249.34", command=self.reset_zaber_scale_defaults).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4,0))
        ttk.Label(za, text=(
            "For your LSM100B-T4A, start with 5249.34 units/mm (0.1905 µm per microstep). "
            "Device ID / Axis # fields are locked by default because your mapping is now known; unlock them only if hardware changes. "
            "The Apply X/Y/Z buttons send only the units_per_mm setting. "
            "COM port numbers (for example COM5) are not the same thing as Zaber device IDs. "
            "The GUI auto-loads your last saved config at startup, so older saved values can override these defaults until you reset them or save a new config."
        ), wraplength=760, justify="left").grid(row=7, column=0, columnspan=6, sticky="w", pady=(6,0))

        test = ttk.LabelFrame(f, text="Identify / test controller axis", padding=8)
        test.grid(row=4, column=1, sticky="nsew", padx=4, pady=4)
        self.backend_zaber_test_frame = test
        ttk.Label(test, text="Test device ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(test, textvariable=self.zaber_test_device_var, width=8).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(test, text="Axis #").grid(row=1, column=0, sticky="w")
        ttk.Entry(test, textvariable=self.zaber_test_axis_var, width=8).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(test, text="Jog distance (mm)").grid(row=2, column=0, sticky="w")
        ttk.Entry(test, textvariable=self.zaber_test_mm_var, width=8).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Button(test, text="Jog selected device/axis +mm", command=lambda: self.zaber_test_jog(sign=+1)).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8,2))
        ttk.Button(test, text="Jog selected device/axis -mm", command=lambda: self.zaber_test_jog(sign=-1)).grid(row=4, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Separator(test, orient="horizontal").grid(row=5, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Button(test, text="Assign selected device/axis to X", command=lambda: self.zaber_assign_axis('X')).grid(row=6, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(test, text="Assign selected device/axis to Y", command=lambda: self.zaber_assign_axis('Y')).grid(row=7, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(test, text="Assign selected device/axis to Z", command=lambda: self.zaber_assign_axis('Z')).grid(row=8, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(test, textvariable=self.zaber_status_var, wraplength=340, justify="left").grid(row=9, column=0, columnspan=2, sticky="w", pady=(8,0))
        ttk.Label(test, text=(
            "For an X-MCC with 3 axes, keep the same device ID and jog axis 1, 2, or 3. "
            "Observe which stage moves, then assign that device/axis pair to X/Y/Z."
        ), wraplength=340, justify="left").grid(row=10, column=0, columnspan=2, sticky="w", pady=(6,0))


        calc = ttk.LabelFrame(f, text="SMC02 / stage calculator", padding=8)
        calc.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)
        self.smc_calc_frame = calc

        self._labeled_entry(calc, "Stage label", self.stage_label_var, 0)
        self._labeled_entry(calc, "Motor step angle (deg)", self.step_angle_var, 1)
        self._labeled_entry(calc, "Microstep subdivision", self.microstep_var, 2)
        self._labeled_entry(calc, "Screw lead (mm/rev)", self.screw_lead_var, 3)
        self._labeled_entry(calc, "X RPM", self.rpm_x_var, 4)
        self._labeled_entry(calc, "Y RPM", self.rpm_y_var, 5)
        self._labeled_entry(calc, "Z RPM", self.rpm_z_var, 6)
        self._labeled_entry(calc, "Suggested accel/decel (F-12)", self.accel_text_var, 7)
        self._labeled_entry(calc, "Suggested work mode (F-01)", self.mode_text_var, 8)
        ttk.Button(calc, text="Recompute calculator", command=self._recompute_stepper_calculator).grid(row=9, column=0, columnspan=2, sticky="ew", pady=(8,0))

        out = ttk.LabelFrame(f, text="Computed theoretical motion", padding=8)
        out.grid(row=2, column=1, sticky="nsew", padx=4, pady=4)
        self.smc_out_frame = out
        self._readonly_labeled(out, "Pulses / rev", self.pulses_rev_var, 0)
        self._readonly_labeled(out, "SMC02 F-09 display value", self.f09_display_var, 1)
        self._readonly_labeled(out, "Steps / mm", self.steps_per_mm_var, 2)
        self._readonly_labeled(out, "X mm/s", self.mm_per_s_x_var, 3)
        self._readonly_labeled(out, "Y mm/s", self.mm_per_s_y_var, 4)
        self._readonly_labeled(out, "Z mm/s", self.mm_per_s_z_var, 5)
        self._readonly_labeled(out, "X theoretical ms/mm", self.ms_per_mm_x_var, 6)
        self._readonly_labeled(out, "Y theoretical ms/mm", self.ms_per_mm_y_var, 7)
        self._readonly_labeled(out, "Z theoretical ms/mm", self.ms_per_mm_z_var, 8)

        advice = ttk.LabelFrame(f, text="Recommended SMC02 settings for this Teensy build", padding=8)
        advice.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=4, pady=4)
        self.smc_advice_frame = advice
        advice_text = (
            "Use one SMC02 per axis. Recommended starting setup:\n\n"
            "• F-01 = P02  (motor moves while CW/CCW is held; stops on release)\n"
            "• F-09 = pulses-per-rev / 10\n"
            "    Example: 1.8° motor, 1/8 subdivision -> 200 * 8 = 1600 pulses/rev -> set F-09 = 160\n"
            "• F-03 / F-05 = desired forward / reverse RPM for that axis\n"
            "• F-12 = moderate accel/decel, for example 020 to start\n"
            "• Keep all three axes on the same lead convention in the GUI, then tune ms/mm empirically\n\n"
            "Why the GUI still needs empirical ms/mm:\n"
            "The Teensy is not sending individual steps. It is simulating button-holds on CW/CCW, so travel depends\n"
            "on controller timing, acceleration, load, and backlash. The calculator provides a starting guess only."
        )
        ttk.Label(advice, text=advice_text, justify="left").grid(row=0, column=0, sticky="nw")

        for var in (
            self.step_angle_var,
            self.microstep_var,
            self.screw_lead_var,
            self.rpm_x_var,
            self.rpm_y_var,
            self.rpm_z_var,
        ):
            var.trace_add("write", lambda *args: self._recompute_stepper_calculator())

        self._refresh_zaber_mapping_edit_state()

    def _layout_backend_sections(self, is_mega):
        try:
            if hasattr(self, 'smc_calc_frame'):
                self.smc_calc_frame.grid_configure(row=2 if not is_mega else 3, column=0, columnspan=1)
            if hasattr(self, 'smc_out_frame'):
                self.smc_out_frame.grid_configure(row=2 if not is_mega else 3, column=1, columnspan=1)
            if hasattr(self, 'smc_advice_frame'):
                self.smc_advice_frame.grid_configure(row=3 if not is_mega else 4, column=0, columnspan=2)
            if hasattr(self, 'backend_zaber_test_frame'):
                self.backend_zaber_test_frame.grid_configure(row=2 if is_mega else 4, column=1, columnspan=1)
        except Exception:
            pass

    def _set_children_state(self, widget, enabled=True):
        desired = "normal" if enabled else "disabled"
        for child in widget.winfo_children():
            try:
                if isinstance(child, (ttk.Entry, ttk.Button, ttk.Checkbutton, ttk.Combobox, tk.Entry, tk.Button, tk.Checkbutton)):
                    child.configure(state=desired)
            except Exception:
                pass
            self._set_children_state(child, enabled)

    def _refresh_backend_ui(self):
        backend = self.backend_var.get()
        is_mega = (backend == "mega_zaber")
        self._layout_backend_sections(is_mega)

        if hasattr(self, "home_ref_button"):
            try:
                self.home_ref_button.configure(text=("HOME" if is_mega else "Set current = ref XYZ"))
            except Exception:
                pass

        if hasattr(self, "session_notes_var"):
            if is_mega:
                self.session_notes_var.set(
                    "Recommended workflow:\n"
                    "1) HOME axes\n"
                    "2) Jog to mouth and set MOUTH from current XYZ\n"
                    "3) Jog to dock/wick and set DOCK from current XYZ\n"
                    "4) Review generated target positions\n"
                    "5) Apply task / geometry / lick parameters\n"
                    "6) START session\n\n"
                    "For Mega/Zaber, absolute positioning and device IDs are used instead of timed-motion calibration."
                )
            else:
                self.session_notes_var.set(
                    "Recommended workflow:\n"
                    "1) Manually place the rig at a known reference location if needed\n"
                    "2) Enter the desired reference XYZ (often 0,0,0) and click 'Set current = ref XYZ'\n"
                    "3) Jog to mouth and set MOUTH from current XYZ\n"
                    "4) Jog to dock/wick and set DOCK from current XYZ\n"
                    "5) Review generated target positions\n"
                    "6) Apply task / geometry / lick parameters\n"
                    "7) START session\n\n"
                    "No home switches are assumed for the Teensy/SMC02 setup. The SMC02 is used in hold-to-move mode, so timed ms/mm calibration still applies."
                )

        if hasattr(self, "motion_axiscal_frame"):
            self._set_children_state(self.motion_axiscal_frame, enabled=not is_mega)
        if hasattr(self, "motion_reference_frame"):
            if is_mega:
                self.motion_reference_frame.grid_remove()
            else:
                self.motion_reference_frame.grid()
        if hasattr(self, "move_to_ref_button"):
            try:
                if is_mega:
                    self.move_to_ref_button.state(["disabled"])
                else:
                    self.move_to_ref_button.state(["!disabled"])
            except Exception:
                pass
        if hasattr(self, "motion_zaber_note_frame"):
            if is_mega:
                self.motion_zaber_note_frame.grid()
            else:
                self.motion_zaber_note_frame.grid_remove()

        for attr in ("backend_zaber_frame", "backend_zaber_test_frame"):
            if hasattr(self, attr):
                self._set_children_state(getattr(self, attr), enabled=is_mega)
        if hasattr(self, "zaber_mapping_unlock_var") and is_mega:
            self._refresh_zaber_mapping_edit_state()

        for attr in ("backend_teensy_io_frame", "smc_calc_frame", "smc_out_frame", "smc_advice_frame"):
            if hasattr(self, attr):
                self._set_children_state(getattr(self, attr), enabled=not is_mega)

        if hasattr(self, "cue_vol_label"):
            if is_mega:
                self.cue_vol_label.configure(text="Cue volume (amp knob)")
            else:
                self.cue_vol_label.configure(text="Cue volume (%)")
        if hasattr(self, "cue_vol_entry"):
            try:
                if is_mega:
                    self.cue_vol_entry.state(["disabled"])
                else:
                    self.cue_vol_entry.state(["!disabled"])
            except Exception:
                pass
        if hasattr(self, "cue_note_var"):
            if is_mega:
                self.cue_note_var.set(
                    "Use Cue only from the Session tab to test cue delivery before starting the task.\n"
                    "For the Mega/Zaber backend, cue frequency and duration are software-controlled, but actual loudness is set with the external amplifier volume knob."
                )
            else:
                self.cue_note_var.set(
                    "Use Cue only from the Session tab to test cue delivery before starting the task.\n"
                    "Volume is implemented as PWM duty cycle on the speaker pin, so perceived loudness depends on your amplifier/speaker."
                )

    def _on_backend_changed(self):
        self._refresh_backend_note()
        self._refresh_backend_ui()
        try:
            nb = self._find_notebook()
            if nb is not None:
                if self.backend_var.get() == "mega_zaber":
                    nb.tab(self.tab_motion, text="3. Motion")
                else:
                    nb.tab(self.tab_motion, text="3. Motion & Calibration")
                nb.tab(self.tab_backend, text="6. Backend")
                nb.tab(self.tab_console, text="7. Console")
        except Exception:
            pass

    def _refresh_backend_note(self):
        if self.backend_var.get() == "mega_zaber":
            self.backend_note_var.set(
                "Mega/Zaber backend is active. Zaber device-ID tools are enabled, digital lick input is assumed, and the COM port should be the Arduino Mega. "
                "The Motion & Calibration tab still handles jog/origin/dock moves, but timed ms/mm calibration and the SMC02 calculator are disabled. "
                "Use Probe after selecting a COM port to confirm you are talking to the Mega/Zaber controller."
            )
        else:
            self.backend_note_var.set(
                "Teensy/SMC02 backend is active. Analog lick thresholding and SMC02 timed-motion calibration apply, the COM port should be the Teensy, and no X/Y/Z home switches are assumed. "
                "Use the reference-setting controls instead of HOME, and use the unified Backend tab for SMC02 calculator/settings. Use Probe after selecting a COM port to confirm you are talking to the Teensy."
            )


    def _update_port_info(self):
        backend = self.backend_var.get() if hasattr(self, "backend_var") else "teensy_smc02"
        port = self.port_var.get().strip() if hasattr(self, "port_var") else ""
        port_desc = ""
        try:
            if hasattr(self, "ports") and isinstance(self.ports, dict):
                port_desc = self.ports.get(port, "")
        except Exception:
            port_desc = ""

        if backend == "mega_zaber":
            backend_hint = "Arduino Mega / Zaber backend selected"
            device_hint = "Choose the Mega USB serial port (not the direct Zaber COM ports)."
        else:
            backend_hint = "Teensy / SMC02 backend selected"
            device_hint = "Choose the Teensy USB serial port."

        if port:
            if port_desc:
                msg = f"{backend_hint}. Selected port: {port} — {port_desc}. {device_hint}"
            else:
                msg = f"{backend_hint}. Selected port: {port}. {device_hint}"
        else:
            msg = f"{backend_hint}. No COM port selected yet. {device_hint}"

        try:
            self.port_info_var.set(msg)
        except Exception:
            pass
    def reset_zaber_scale_defaults(self):
        self.zaber_x_upm_var.set("5249.34")
        self.zaber_y_upm_var.set("5249.34")
        self.zaber_z_upm_var.set("5249.34")
        try:
            self.zaber_status_var.set("Reset X/Y/Z units-per-mm fields to 5249.34")
        except Exception:
            pass

    def _refresh_zaber_mapping_edit_state(self):
        unlocked = bool(self.zaber_mapping_unlock_var.get()) if hasattr(self, "zaber_mapping_unlock_var") else False
        state = "normal" if unlocked else "readonly"
        for entry in getattr(self, "zaber_mapping_entries", []):
            try:
                entry.configure(state=state)
            except Exception:
                pass

    def apply_backend_selection(self):
        self._on_backend_changed()
        backend = self.backend_var.get()
        self.port_info_var.set(("Backend set to Teensy/SMC02. Choose the COM port for the Teensy and probe it before connecting."
                                if backend == "teensy_smc02" else
                                "Backend set to Mega/Zaber. Choose the COM port for the Arduino Mega and probe it before connecting."))
        self._log_local(f"[GUI] Backend selected: {backend}")
        self._update_port_info()

    def apply_teensy_io_settings(self):
        messagebox.showinfo(APP_TITLE, "The current Teensy firmware uses a fixed single-side I/O mapping; there is no left/right side setting to apply.")

    def _queue_commands(self, commands, delay_ms=180, final_status=None, on_complete=None):
        if not commands:
            return
        batch = (list(commands), delay_ms, final_status, on_complete)
        if self._command_batch_active:
            self._pending_command_batches.append(batch)
            return
        self._start_command_batch(*batch)

    def _start_command_batch(self, commands, delay_ms=180, final_status=None, on_complete=None):
        if not commands:
            self._drain_next_command_batch()
            return
        if not self._ensure_connected_for_command():
            self._pending_command_batches.clear()
            return
        self._command_batch_active = True

        def _finish():
            self._command_batch_active = False
            if final_status:
                self.zaber_status_var.set(final_status)
            if on_complete:
                try:
                    on_complete()
                except Exception:
                    pass
            if not self._command_batch_active:
                self._drain_next_command_batch()

        def _abort():
            self._command_batch_active = False
            self._pending_command_batches.clear()

        def _send_next(i=0):
            if i >= len(commands):
                _finish()
                return
            if not self.client.is_connected():
                self._ensure_connected_for_command()
                _abort()
                return
            ok = self.send(commands[i])
            if not ok:
                _abort()
                return
            self.after(delay_ms, lambda: _send_next(i + 1))

        _send_next(0)

    def _drain_next_command_batch(self):
        if self._command_batch_active:
            return
        if not self._pending_command_batches:
            return
        commands, delay_ms, final_status, on_complete = self._pending_command_batches.popleft()
        self._start_command_batch(commands, delay_ms=delay_ms, final_status=final_status, on_complete=on_complete)

    def apply_zaber_axis_settings(self, axis_name):
        if self.backend_var.get() != "mega_zaber":
            messagebox.showinfo(APP_TITLE, "Select the Mega/Zaber backend first.")
            return
        axis_name = axis_name.upper()
        if axis_name == "X":
            upm = self.zaber_x_upm_var.get_float()
            key = "x"
        elif axis_name == "Y":
            upm = self.zaber_y_upm_var.get_float()
            key = "y"
        else:
            upm = self.zaber_z_upm_var.get_float()
            key = "z"
        cmds = [
            f"SET zaber.axis.{key}.units_per_mm={upm}",
        ]
        self._queue_commands(cmds, delay_ms=220, final_status=f"Applied {axis_name} units/mm = {upm}")

    def apply_zaber_settings(self):
        if self.backend_var.get() != "mega_zaber":
            messagebox.showinfo(APP_TITLE, "Select the Mega/Zaber backend first.")
            return
        cmds = [
            f"SET zaber.axis.x.units_per_mm={self.zaber_x_upm_var.get_float()}",
            f"SET zaber.axis.y.units_per_mm={self.zaber_y_upm_var.get_float()}",
            f"SET zaber.axis.z.units_per_mm={self.zaber_z_upm_var.get_float()}",
        ]
        self._queue_commands(cmds, delay_ms=220, final_status="Applied ALL Zaber units/mm to device")

    def zaber_test_jog(self, sign=1):
        if self.backend_var.get() != "mega_zaber":
            messagebox.showinfo(APP_TITLE, "Select the Mega/Zaber backend first.")
            return
        dev = self.zaber_test_device_var.get_int()
        axis_num = self.zaber_test_axis_var.get_int()
        mm = self.zaber_test_mm_var.get_float() * (1 if sign >= 0 else -1)
        self.send(f"MOVE mode=device device={dev} axis={axis_num} mm={mm}")
        self.zaber_status_var.set(f"Sent device jog: device {dev}, axis {axis_num}, mm={mm}")

    def zaber_assign_axis(self, axis_name):
        if self.backend_var.get() != "mega_zaber":
            messagebox.showinfo(APP_TITLE, "Select the Mega/Zaber backend first.")
            return
        dev = self.zaber_test_device_var.get_int()
        axis_num = self.zaber_test_axis_var.get_int()
        if axis_name == 'X':
            self.zaber_x_device_var.set(str(dev))
            self.zaber_x_axis_var.set(str(axis_num))
        elif axis_name == 'Y':
            self.zaber_y_device_var.set(str(dev))
            self.zaber_y_axis_var.set(str(axis_num))
        elif axis_name == 'Z':
            self.zaber_z_device_var.set(str(dev))
            self.zaber_z_axis_var.set(str(axis_num))
        self.zaber_status_var.set(f"Assigned device {dev}, axis {axis_num} to logical axis {axis_name}. Click 'Apply Zaber IDs / axis numbers / scales to device' to send it.")

    def _config_dict(self):
        cfg = super()._config_dict()
        cfg["backend"] = {
            "type": self.backend_var.get(),
            "teensy": {
                "io_side": self.teensy_io_side_var.get(),
            },
            "zaber": {
                "x_device_id": self.zaber_x_device_var.get(),
                "y_device_id": self.zaber_y_device_var.get(),
                "z_device_id": self.zaber_z_device_var.get(),
                "x_axis_number": self.zaber_x_axis_var.get(),
                "y_axis_number": self.zaber_y_axis_var.get(),
                "z_axis_number": self.zaber_z_axis_var.get(),
                "x_units_per_mm": self.zaber_x_upm_var.get(),
                "y_units_per_mm": self.zaber_y_upm_var.get(),
                "z_units_per_mm": self.zaber_z_upm_var.get(),
            }
        }
        return cfg

    def _apply_config_dict(self, cfg):
        super()._apply_config_dict(cfg)
        b = cfg.get("backend", {})
        if isinstance(b, dict):
            self.backend_var.set(b.get("type", self.backend_var.get()))
            t = b.get("teensy", {}) if isinstance(b.get("teensy", {}), dict) else {}
            self.teensy_io_side_var.set(t.get("io_side", self.teensy_io_side_var.get()))
            z = b.get("zaber", {}) if isinstance(b.get("zaber", {}), dict) else {}
            self.zaber_x_device_var.set(z.get("x_device_id", self.zaber_x_device_var.get()))
            self.zaber_y_device_var.set(z.get("y_device_id", self.zaber_y_device_var.get()))
            self.zaber_z_device_var.set(z.get("z_device_id", self.zaber_z_device_var.get()))
            self.zaber_x_axis_var.set(z.get("x_axis_number", self.zaber_x_axis_var.get()))
            self.zaber_y_axis_var.set(z.get("y_axis_number", self.zaber_y_axis_var.get()))
            self.zaber_z_axis_var.set(z.get("z_axis_number", self.zaber_z_axis_var.get()))
            self.zaber_x_upm_var.set(z.get("x_units_per_mm", self.zaber_x_upm_var.get()))
            self.zaber_y_upm_var.set(z.get("y_units_per_mm", self.zaber_y_upm_var.get()))
            self.zaber_z_upm_var.set(z.get("z_units_per_mm", self.zaber_z_upm_var.get()))
        self._refresh_backend_note()

    def _parse_serial_line(self, line: str):
        super()._parse_serial_line(line)
        def kv_from_tokens(tokens):
            data = {}
            for p in tokens:
                if "=" in p:
                    k, v = p.split("=", 1)
                    data[k] = v
            return data

        if line.startswith("EVT "):
            data = kv_from_tokens(line.split()[1:])
            if data.get("name", "") == "position":
                idx = data.get("idx", "")
                if idx.isdigit():
                    self._sequence_on_position_arrival(int(idx))
        elif line.startswith("ERR "):
            data = kv_from_tokens(line.split()[1:])
            if self._sequence_running and data.get("cmd", "") == "move":
                detail = data.get("detail", "")
                code = data.get("code", "")
                mode = "move"
                if "mode=" in detail:
                    mode = detail
                elif code:
                    mode = f"{code}"
                self._sequence_fail(f"Sequence: move failed ({mode})")

        if not line.startswith("CFG "):
            return
        data = kv_from_tokens(line.split()[1:])
        key = data.get("key", "")
        val = data.get("value", "")
        if key == "backend.type":
            self.backend_var.set(val)
            self._refresh_backend_note()
        elif key == "backend.teensy.io_side":
            self.teensy_io_side_var.set(val)
        elif key == "zaber.axis.x.device_id":
            self.zaber_x_device_var.set(val)
        elif key == "zaber.axis.y.device_id":
            self.zaber_y_device_var.set(val)
        elif key == "zaber.axis.z.device_id":
            self.zaber_z_device_var.set(val)
        elif key == "zaber.axis.x.axis_number":
            self.zaber_x_axis_var.set(val)
        elif key == "zaber.axis.y.axis_number":
            self.zaber_y_axis_var.set(val)
        elif key == "zaber.axis.z.axis_number":
            self.zaber_z_axis_var.set(val)
        elif key == "zaber.axis.x.units_per_mm":
            self.zaber_x_upm_var.set(val)
        elif key == "zaber.axis.y.units_per_mm":
            self.zaber_y_upm_var.set(val)
        elif key == "zaber.axis.z.units_per_mm":
            self.zaber_z_upm_var.set(val)



if __name__ == "__main__":
    app = App()
    app.mainloop()
