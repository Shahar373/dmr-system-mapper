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

        def setSampleRate(self, *a):
            pass

        def __init__(self, args):
            opened.append(args)
            if open_raises:
                raise open_raises

    mod = types.ModuleType("SoapySDR")
    mod.Device = Device
    mod.SOAPY_SDR_RX, mod.SOAPY_SDR_CF32 = 0, "CF32"
    monkeypatch.setitem(sys.modules, "SoapySDR", mod)
    return opened


def _here(cfg):
    """A copy of the (session-wide) config that asks the driver in this process, where the fakes live."""
    c = copy.deepcopy(cfg)
    c["sdr"]["probe_in_subprocess"] = False
    return c


def test_probe_never_opens_the_device(monkeypatch, cfg):
    cfg = _here(cfg)
    opened = _fake_soapy(monkeypatch, [{"driver": "sdrplay", "serial": "123"}])
    s = SoapyBackend(cfg).probe()
    assert s.status == "present" and s.serial == "123" and opened == []


def test_plugged_in_but_hidden_by_the_driver_is_busy(monkeypatch, cfg):
    cfg = _here(cfg)
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


def test_open_uses_the_full_enumerated_arguments(monkeypatch, cfg):
    opened = _fake_soapy(monkeypatch, [{"driver": "sdrplay", "serial": "ABC", "label": "RSP1B"}], open_raises=RuntimeError("no match"))
    with pytest.raises(RuntimeError) as e:
        SoapyBackend(cfg).open()
    assert opened == ["driver=sdrplay,serial=ABC"] and "no match" in str(e.value)
    _fake_soapy(monkeypatch, [])
    with pytest.raises(RuntimeError, match="no free receiver"):
        SoapyBackend(cfg).open()


def test_device_args_string():
    from dmr_mapper.collector.sdr import device_args_string
    assert device_args_string({"serial": "240404AF60", "driver": "sdrplay", "label": "SDRplay Dev0 RSP1B 240404AF60"}) == "driver=sdrplay,serial=240404AF60"
    assert device_args_string({"driver": "sdrplay", "serial": ""}) == "driver=sdrplay"


def test_free_receiver_has_no_holder_even_with_other_sdr_processes(monkeypatch, cfg):
    cfg = _here(cfg)
    _fake_soapy(monkeypatch, [{"driver": "sdrplay", "serial": "240404AF60"}])
    monkeypatch.setattr(sdrmod, "sdr_users", lambda c: ["/usr/bin/python3 app.py (pid 7)"])
    s = SoapyBackend(cfg).probe()
    assert s.status == "present" and s.holder is None and s.detail is None


class CountingBackend(SyntheticBackend):
    def __init__(self):
        super().__init__(SyntheticSource())
        self.full = 0
        self.plugged = True

    def probe(self):
        self.full += 1
        return super().probe()

    def connected(self):
        return self.plugged


def test_idle_loop_does_not_call_the_driver_every_tick(tmp_path, cfg):
    c = copy.deepcopy(cfg)
    c["daemon"].update({"tick_s": 0.05, "sdr_probe_interval_s": 0.05, "sdr_full_probe_interval_s": 3600})
    c["power"]["state_file"] = ""
    b = CountingBackend()
    d = Daemon(tmp_path / "i.db", c, b)
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    try:
        time.sleep(0.8)
        assert b.full == 1 and d.sdr.status == "present"          # one driver call at start, then USB checks only
        b.plugged = False
        time.sleep(0.3)
        assert d.sdr.status == "absent" and b.full == 1
        b.plugged = True
        time.sleep(0.3)
        assert d.sdr.status == "present" and b.full == 2           # plugged in again: the driver is asked once
    finally:
        d.stop_event.set()
        th.join(timeout=10)


# -- the driver stays out of the daemon -------------------------------------------------------------
FAKE_SOAPY_PKG = """
import json, os
print("SDRplay API banner on stdout")              # the real driver may print: must not confuse the parent
open(os.environ["FAKE_SOAPY_MARK"], "a").write("loaded\\n")
class Device:
    @staticmethod
    def enumerate(args):
        if os.environ.get("FAKE_SOAPY_FAIL"):
            raise RuntimeError("sdrplay_api_Open failed")
        return [{"driver": args["driver"], "serial": "240404AF60", "label": "RSP1B"}]
"""


@pytest.fixture
def child_soapy(tmp_path, monkeypatch):
    d = tmp_path / "pkgs"
    d.mkdir()
    (d / "SoapySDR.py").write_text(FAKE_SOAPY_PKG)
    mark = tmp_path / "mark"
    monkeypatch.setenv("PYTHONPATH", str(d))
    monkeypatch.setenv("FAKE_SOAPY_MARK", str(mark))
    monkeypatch.delitem(sys.modules, "SoapySDR", raising=False)
    return mark


def test_probe_asks_the_driver_from_a_child_process(child_soapy, cfg):
    b = SoapyBackend(cfg)
    s = b.probe()
    assert s.status == "present" and s.serial == "240404AF60"
    assert child_soapy.read_text() == "loaded\n"               # loaded in the child ...
    assert "SoapySDR" not in sys.modules and not b.driver_loaded  # ... never here


def test_child_probe_reports_driver_errors_and_missing_bindings(child_soapy, monkeypatch, tmp_path, cfg):
    monkeypatch.setenv("FAKE_SOAPY_FAIL", "1")
    s = SoapyBackend(cfg).probe()
    assert s.status == "error" and "sdrplay_api_Open failed" in (s.detail or "")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "SoapySDR.py").write_text("raise ImportError('libSoapySDR.so.0.8: cannot open shared object file')\n")
    monkeypatch.setenv("PYTHONPATH", str(broken))
    s = SoapyBackend(cfg).probe()
    assert s.status == "error" and "not importable" in (s.detail or "") and "libSoapySDR" in (s.detail or "")


def test_child_probe_gives_up_on_a_hung_driver(child_soapy, monkeypatch, tmp_path, cfg):
    hung = tmp_path / "hung"
    hung.mkdir()
    (hung / "SoapySDR.py").write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setenv("PYTHONPATH", str(hung))
    cfg = copy.deepcopy(cfg)
    cfg["sdr"]["probe_timeout_s"] = 1
    t0 = time.monotonic()
    s = SoapyBackend(cfg).probe()
    assert s.status == "error" and "did not answer" in (s.detail or "") and time.monotonic() - t0 < 10


class DriverBackend(SyntheticBackend):
    """A synthetic receiver whose open() 'loads the driver', like SoapyBackend."""

    def __init__(self):
        super().__init__(SyntheticSource())
        self.driver_loaded = False

    def open(self):
        self.driver_loaded = True
        return super().open()


def _idle_daemon(tmp_path, cfg, backend, release_s):
    c = copy.deepcopy(cfg)
    c["daemon"].update({"tick_s": 0.05, "release_driver_idle_s": release_s})
    c["power"]["state_file"] = ""
    return Daemon(tmp_path / "r.db", c, backend)


def test_idle_daemon_restarts_once_a_session_loaded_the_driver(tmp_path, cfg):
    b = DriverBackend()
    d = _idle_daemon(tmp_path, cfg, b, 0.3)
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    try:
        time.sleep(0.6)
        assert th.is_alive() and not d.restart_requested          # driver never loaded: stays up
        b.driver_loaded = True                                     # as after a session
        th.join(timeout=5)
        assert not th.is_alive() and d.restart_requested
        s = Store(tmp_path / "r.db")
        try:
            status = st.read_status(s)
            assert status.alive and status.mode == "idle"         # not marked offline: it is back in a moment
            assert "release the SDR driver" in (status.message or "")
        finally:
            s.close()
    finally:
        d.stop_event.set()
        th.join(timeout=10)


def test_no_restart_while_a_command_waits_or_when_disabled(tmp_path, cfg):
    b = DriverBackend()
    b.driver_loaded = True
    d = _idle_daemon(tmp_path, cfg, b, 0.0)                        # 0 = never
    th = threading.Thread(target=d.run, daemon=True)
    th.start()
    try:
        time.sleep(0.5)
        assert th.is_alive() and not d.restart_requested
    finally:
        d.stop_event.set()
        th.join(timeout=10)
    d2 = _idle_daemon(tmp_path, cfg, b, 0.2)
    s = Store(tmp_path / "r.db")
    try:
        d2._store = s
        d2._last_busy = time.monotonic() - 10
        assert d2._should_release_driver(time.monotonic())
        s.conn.execute("INSERT INTO commands (created, source, kind, params, status) VALUES (0, 'test', 'stop', '{}', 'pending')")
        s.commit()
        assert not d2._should_release_driver(time.monotonic())    # the queued command first
    finally:
        s.close()
