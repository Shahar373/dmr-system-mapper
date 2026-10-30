"""Receiver probing must never open the device, and must not blame programs that only share a
word with an SDR program's path (the Pi's power guard lives in the same folder as the web tuner)."""

from __future__ import annotations

import copy
import subprocess
import sys
import threading
import time
import types

import pytest

from dmr_mapper.collector import sdr as sdrmod
from dmr_mapper.collector import state as st
from dmr_mapper.collector.daemon import Daemon
from dmr_mapper.collector.sdr import SoapyBackend, SyntheticBackend, SyntheticSource, sdr_users
from dmr_mapper.db import Store


class FakeSoapy(types.SimpleNamespace):
    pass


def _fake_soapy(monkeypatch, found: list, open_raises: Exception | None = None):
    opened = []

    class Device:
        @staticmethod
        def enumerate(args):
            return found

        def __init__(self, args):
            opened.append(args)
            if open_raises:
                raise open_raises

    mod = types.ModuleType("SoapySDR")
    mod.Device = Device
    monkeypatch.setitem(sys.modules, "SoapySDR", mod)
    return opened


def test_probe_never_opens_the_device(monkeypatch, cfg):
    opened = _fake_soapy(monkeypatch, [{"driver": "sdrplay", "serial": "123"}])
    s = SoapyBackend(cfg).probe()
    assert s.status == "present" and s.serial == "123" and opened == []


def test_plugged_in_but_hidden_by_the_driver_is_busy(monkeypatch, cfg):
    _fake_soapy(monkeypatch, [])
    monkeypatch.setattr(sdrmod, "usb_present", lambda c: True)
    s = SoapyBackend(cfg).probe()
    assert s.status == "busy" and "another program" in (s.detail or "")
    monkeypatch.setattr(sdrmod, "usb_present", lambda c: False)
    assert SoapyBackend(cfg).probe().status == "absent"


def test_a_script_in_a_folder_named_like_an_sdr_tool_is_not_a_user(tmp_path, cfg):
    d = tmp_path / "webtune"
    d.mkdir()
    script = d / "power_guard.py"
    script.write_text("import time\ntime.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        time.sleep(0.3)
        c = copy.deepcopy(cfg)
        c["sdr"]["known_users"] = ["webtune", "trunk-recorder"]
        assert not any(f"pid {proc.pid}" in u for u in sdr_users(c))
        c["sdr"]["known_users"] = ["power_guard.py"]   # an exact script name still counts when maps are unreadable
        users = sdr_users(c)
        assert not any(f"pid {proc.pid}" in u for u in users)   # maps ARE readable here (same user) and show no SDR library
    finally:
        proc.terminate()
        proc.wait()


def test_driver_refusal_at_start_is_the_command_result(tmp_path, cfg):
    class Refusing(SyntheticBackend):
        def open(self):
            raise RuntimeError("sdrplay_api_SelectDevice() failed: sdrplay_api_ServiceNotResponding")

    c = copy.deepcopy(cfg)
    c["daemon"].update({"tick_s": 0.2, "sdr_probe_interval_s": 1})
    c["power"]["state_file"] = ""
    db = tmp_path / "r.db"
    d = Daemon(db, c, Refusing(SyntheticSource()))
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    client = Store(db)
    try:
        time.sleep(0.5)
        cmd = st.wait_for_command(client, st.post_command(client, "test", "start_survey"), 10)
        assert cmd["status"] == "failed" and "SelectDevice" in cmd["result"], cmd
        assert st.sessions(client) == []
        time.sleep(0.5)
        assert st.read_status(client).mode == "idle"
    finally:
        d.stop_event.set()
        th.join(timeout=10)
        client.close()
