from __future__ import annotations

import csv

import pytest

from edge.config import ConfigError, Profile, load_experiment, load_profiles, load_scenarios
from edge.logging_utils import FRAMES_COLUMNS, CsvLog, make_trial_id, new_trial_dir


def test_csv_header_and_rows(tmp_path):
    lg = CsvLog(tmp_path / "frames.csv", FRAMES_COLUMNS, "t1")
    lg.write(frame_id=1, profile="low", latency_ms=12.3456, outcome="ok")
    lg.close()
    rows = list(csv.DictReader(open(tmp_path / "frames.csv")))
    assert list(rows[0].keys()) == list(FRAMES_COLUMNS)
    assert rows[0]["trial_id"] == "t1" and rows[0]["latency_ms"] == "12.346" and rows[0]["send_ns"] == ""


def test_csv_rejects_unknown_columns(tmp_path):
    lg = CsvLog(tmp_path / "f.csv", FRAMES_COLUMNS, "t1")
    with pytest.raises(KeyError):
        lg.write(latency_seconds=1.0)
    lg.close()


def test_logs_and_trial_dirs_never_overwrite(tmp_path):
    d = new_trial_dir(tmp_path, "trial_a")
    with pytest.raises(FileExistsError):
        new_trial_dir(tmp_path, "trial_a")
    CsvLog(d / "frames.csv", FRAMES_COLUMNS, "trial_a").close()
    with pytest.raises(FileExistsError):
        CsvLog(d / "frames.csv", FRAMES_COLUMNS, "trial_a")


def test_trial_id_format():
    tid = make_trial_id("adaptive", "poor", 2, prefix="m1")
    assert tid == "m1_adaptive_poor_r2"
    assert make_trial_id("fixed_low", "good", 1) != make_trial_id("fixed_low", "good", 2)


def test_repo_configs_are_valid():
    profiles, order = load_profiles()
    scenarios, schedules = load_scenarios()
    exp = load_experiment()
    assert order[0] == "low" and order[-1] == "high"
    assert {"good", "poor"} <= set(scenarios) and "dynamic" in schedules
    assert exp.controller.start_profile in profiles


def test_profile_validation_rejects_4x3():
    with pytest.raises(ConfigError):
        Profile("bad", 640, 480, 70, 10, 1000).validate()
