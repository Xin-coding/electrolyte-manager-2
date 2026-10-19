"""Application service: owns decisions; device workers own all serial I/O."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict
import copy
import logging
import queue
import threading
import time

from .engine import Engine
from .models import Binding, Dose, DoseResult, Load, Sample, Settings, finite, utc_now
from .paths import atomic_json, diagnostic_logger, keep_awake, read_json, root_dir
from .storage import Store, mark_interrupted
from .devices.sensors import SensorWorker, ports, identify
from .devices.pump import PumpWorker


class Runtime:
    def __init__(self, root=None):
        self.root = root or root_dir()
        for folder in ("data", "exports", "data/profiles", "data/tests"):
            (self.root/folder).mkdir(parents=True, exist_ok=True)
        self.commands = queue.Queue(maxsize=100)
        self.incoming = queue.Queue(maxsize=5000)
        self.quit = threading.Event()
        self.workers = {}
        self.bindings = {}
        self.connections = {}
        self.notices = deque(maxlen=100)
        self.pump_status = {}
        self.hardware = {}
        self.confirmed_signature = None
        self.phase = "idle"
        self.progress = {}
        self.samples = {}
        self.sequence = {"ph": 0, "ec": 0}
        self.snapshot = {}
        self.store = None
        self.bench = None
        self.epoch = time.monotonic()
        self.bench_epoch = self.epoch
        self.log = diagnostic_logger(self.root)
        self.config_file = self.root/"data"/"settings.json"
        self.ledger_file = self.root/"data"/"load.json"
        settings = Settings.from_dict(read_json(self.config_file, {}))
        ledger = read_json(self.ledger_file, {})
        load = Load(**ledger.get("load", {}))
        if ledger.get("intent"):
            load.uncertain = True
        self.engine = Engine(settings, load, self.event)
        self.intent = ledger.get("intent")
        self.scan_busy = False
        self.finish_pending = False
        self.pending_reason = ""
        self.pending_allowance = None
        self.last_pump_sample = 0.
        self.local_storage_error = ""
        self.thread = threading.Thread(target=self.run, name="em2-service", daemon=True)
        self.thread.start()

    def command(self, action, **data):
        self.commands.put_nowait((action, data))

    def stop(self, reason="emergency_stop"):
        # Direct priority flag bypasses storage, GUI refresh and the service queue.
        worker = self.workers.get("pump")
        if worker:
            worker.stop(reason)
        self.command("pause", reason=reason)

    def emit(self, name, data):
        try:
            self.incoming.put((name, data), timeout=.2)
        except queue.Full:
            pump = self.workers.get("pump")
            if pump:
                pump.stop("event_queue_overflow")
            self.log.error("Device event queue overflow: %s", name)

    def trace(self, direction, value):
        self.log.info("%s %s", direction, value)

    def elapsed(self):
        return max(0., time.monotonic()-self.epoch)

    def event(self, name, data):
        now = self.elapsed()
        entry = {"time": utc_now(), "kind": name, "data": data}
        if name not in {"decision", "pump_progress"}:
            self.notices.append(entry)
        if self.store:
            self.store.event(now, name, data)
        if self.bench and name in {"dose_accounted", "refill", "allowance_changed"}:
            self.bench.event(time.monotonic()-self.bench_epoch, name, data)
        if name == "settings_applied":
            if self.store:
                self.store.put("settings", (data["new"]["version"], now, data["new"]))
            atomic_json(self.config_file, data["new"])
        if name == "dose_accounted":
            self.intent = None
            try:
                self.persist_ledger()
            except OSError:
                self.engine.pause("storage_paused")
            destination = self.store if data["dose"]["purpose"] == "experiment" else self.bench
            if destination:
                destination.put("injection", {**data, "t": now if destination == self.store else time.monotonic()-self.bench_epoch}, critical=True)
        if name in {"allowance_changed", "refill"}:
            self.persist_ledger()

    def persist_ledger(self):
        try:
            atomic_json(self.ledger_file, {"load": asdict(self.engine.load), "intent": self.intent})
            self.local_storage_error = ""
        except OSError as exc:
            self.local_storage_error = "Local dose ledger: "+str(exc)
            raise

    def authorize(self, dose, evidence):
        ack = threading.Event()
        request = {"dose": dose, "evidence": evidence, "ack": ack, "ok": False}
        self.emit("authorize", request)
        return ack.wait(5) and request["ok"]

    def _authorize(self, request):
        try:
            d = request["dose"]
            e = self.engine
            if not e.active or e.active.id != d.id or e.stop_requested or e.storage_failed:
                return
            if d.purpose == "experiment" and (not e.enabled or e.alarm or not e.connected[e.settings.control]):
                return
            if self.confirmed_signature != request["evidence"]["hardware"]["signature"]:
                return
            destination = self.store if d.purpose == "experiment" else self.bench
            if not destination:
                return
            self.intent = {"dose": asdict(d), "evidence": request["evidence"], "time": utc_now()}
            if "pump" in self.bindings:
                self.engine.load.identity = self.bindings["pump"].identity
            self.persist_ledger()
            t = self.elapsed() if destination == self.store else time.monotonic()-self.bench_epoch
            request["ok"] = destination.event(t, "injection_intent", self.intent, critical=True)
        finally:
            request["ack"].set()

    def connect(self, binding):
        if self.scan_busy:
            raise ValueError("Device detection is in progress")
        if binding.kind in self.workers:
            raise ValueError("Disconnect this device before changing its port")
        if any(b.identity == binding.identity or b.port == binding.port for b in self.bindings.values()):
            raise ValueError("This port is already owned by another device")
        self.bindings[binding.kind] = binding
        if binding.kind == "pump" and self.engine.load.identity and self.engine.load.identity != binding.identity:
            self.engine.load.uncertain = True
            self.pending_reason = "Load belongs to a different pump; establish this pump's syringe load"
        self.connections[binding.kind] = {"connected": False, "connecting": True, "endpoint": asdict(binding)}
        if binding.kind == "pump":
            worker = PumpWorker(binding, self.emit, self.authorize, self.trace)
        else:
            worker = SensorWorker(binding, self.emit, lambda: self.engine.settings.ph_interval_s, self.trace)
        self.workers[binding.kind] = worker
        worker.start()

    def scan(self):
        if self.scan_busy:
            return
        self.scan_busy = True
        used = {b.port for b in self.bindings.values()}
        missing = [k for k in ("pump", "ec", "ph") if k not in self.workers]
        def work():
            found = []
            try:
                for endpoint in ports():
                    if endpoint["port"] in used:
                        continue
                    for kind in list(missing):
                        try:
                            info = identify(kind, endpoint["port"], self.trace)
                            found.append((Binding(kind, endpoint["port"], endpoint["identity"]), info))
                            missing.remove(kind)
                            break
                        except Exception:
                            continue
            finally:
                self.emit("scan_done", {"found": found})
        threading.Thread(target=work, name="em2-detection", daemon=True).start()

    def handle(self, name, data):
        e = self.engine
        if name == "authorize":
            self._authorize(data)
        elif name == "scan_done":
            self.scan_busy = False
            for binding, info in data["found"]:
                self.connect(binding)
            self.event("detection_finished", {"found": [b.kind for b, _ in data["found"]]})
        elif name == "sample":
            kind = data["kind"]
            self.sequence[kind] += 1
            sample = Sample(**data, sequence=self.sequence[kind])
            filtered = e.observe(sample)
            self.samples[kind] = {**asdict(sample), "filtered": filtered}
            if self.store:
                self.store.sample(max(0, sample.mono-self.epoch), sample, filtered)
        elif name == "connection":
            kind = data["kind"]
            previous = self.connections.get(kind, {})
            self.connections[kind] = {**previous, **data, "connecting": False}
            e.connection(kind, data["connected"])
            if kind == "pump":
                e.hardware_ready = data["connected"] and self.pump_status.get("state") == 3 and not e.load.uncertain
            if previous.get("connected") != data["connected"] or previous.get("error") != data.get("error"):
                self.event("connection", data)
            if data.get("closed"):
                self.workers.pop(kind, None)
                self.bindings.pop(kind, None)
        elif name == "pump_status":
            old = self.pump_status
            self.pump_status = data
            e.hardware_ready = e.connected["pump"] and data["state"] == 3 and not data["error"] and not e.load.uncertain
            if old.get("state") != data["state"] or old.get("error") != data["error"]:
                self.event(name, data)
            elif data["state"] == 1 and time.monotonic()-self.last_pump_sample >= 1:
                destination = self.store if e.active and e.active.purpose == "experiment" else self.bench
                if destination:
                    destination.event(self.elapsed() if destination == self.store else time.monotonic()-self.bench_epoch, "pump_running_sample", data)
                self.last_pump_sample = time.monotonic()
            if data["error"] and e.enabled:
                self.stop("pump_fault")
        elif name == "pump_hardware":
            old = self.hardware.get("signature")
            self.hardware = data
            if old and old != data["signature"]:
                self.confirmed_signature = None
                e.pause("hardware_changed")
                self.event("hardware_changed", data)
        elif name == "pump_phase":
            self.phase = data["phase"]
            self.event(name, data)
        elif name == "pump_progress":
            self.progress = data
            if self.store:
                self.store.event(self.elapsed(), name, data)
        elif name in {"dose_result", "dose_pending"}:
            result = DoseResult(**data)
            e.complete(result)
            if name == "dose_pending":
                self.pending_reason = result.error or "Final dose is unknown; verify the delivered amount"
                self.event(name, data)
            else:
                self.pending_reason = ""
                self.phase = "idle"
                self.progress = {}
                e.hardware_ready = e.connected["pump"] and not self.pump_status.get("error") and self.pump_status.get("state") == 3
        else:
            self.event(name, data)

    def dispatch(self, name, data):
        e = self.engine
        if name == "scan":
            self.scan()
        elif name == "connect":
            self.connect(Binding(**data))
        elif name == "disconnect":
            kind = data["kind"]
            if kind == "pump" and (e.active or e.enabled):
                raise ValueError("Stop feedback and wait for confirmed stop before disconnecting")
            if kind in self.workers:
                self.workers[kind].close()
        elif name == "start_recording":
            if self.store:
                raise ValueError("An experiment is already recording")
            if e.active:
                raise ValueError("Wait for the pump to stop")
            new_store = Store.create(self.root/"data", data.get("name", "Experiment"), e.settings.payload())
            e.begin()
            self.epoch = time.monotonic()
            self.store = new_store
            self.store.put("settings", (e.settings.version, 0., e.settings.payload()))
            self.store.event(0, "recording_started", {"bindings": {k: asdict(v) for k, v in self.bindings.items()}, "load": asdict(e.load)})
            keep_awake(True)
        elif name == "finish":
            e.pause("ending")
            self.finish_pending = True
        elif name == "enable":
            if self.local_storage_error:
                self.persist_ledger()
                e.storage_failed = any(s.failure_age >= 30 for s in (self.store,self.bench) if s)
            if data.get("signature") != self.hardware.get("signature"):
                raise ValueError("Confirm the current syringe information")
            self.confirmed_signature = data["signature"]
            e.enable()
        elif name == "pause":
            e.pause(data.get("reason", "manual_pause"))
        elif name == "apply":
            e.apply(Settings.from_dict(data["settings"]), time.monotonic())
        elif name == "allowance":
            if not e.connected["pump"] or (self.pump_status.get("state") != 3 and (data.get("refill") or not e.active)):
                raise ValueError("Connect the pump and confirm stopped before changing the load")
            if e.load.uncertain and not data.get("refill"):
                raise ValueError("Review the unresolved dose or refill the syringe")
            if not finite(data["value"]) or data["value"] <= 0:
                raise ValueError("Load allowance must be positive")
            if not data.get("refill") and (e.active or time.monotonic() < e.ready_at):
                self.pending_allowance = data["value"]
                self.event("allowance_requested", {"old": e.load.limit_ml, "new": data["value"]})
                return
            e.set_allowance(data["value"], data.get("refill", False), self.bindings["pump"].identity)
            self.pending_allowance = None
            if data.get("refill"):
                self.intent = None
                self.pending_reason = ""
                self.persist_ledger()
        elif name == "test":
            if data.pop("signature", None) != self.hardware.get("signature"):
                raise ValueError("Confirm the current syringe information")
            self.confirmed_signature = self.hardware["signature"]
            d = Dose(**data)
            if not all(finite(x) and x > 0 for x in (d.volume_ml, d.flow_ml_min, d.cap)) or not 20 <= d.force_percent <= 100:
                raise ValueError("Invalid pump test parameters")
            if d.purpose == "stop_test" and d.cap <= d.volume_ml:
                raise ValueError("STOP test hardware cap must exceed its software cutoff")
            if not e.connected["pump"] or not e.hardware_ready:
                raise ValueError("Pump stop must be confirmed")
            if self.bench is None:
                self.bench_epoch = time.monotonic()
                self.bench = Store.create(self.root/"data/tests", "Pump_test", {})
            e.test(d)
            try:
                self.workers["pump"].submit(d)
            except Exception:
                e.active = None
                raise
        elif name == "resolve":
            self.workers["pump"].manual_result(data["amount"])
            e.pause("manual_review")
        elif name == "diagnostics":
            self.log.setLevel(logging.INFO if data["enabled"] else logging.WARNING)
        else:
            raise ValueError("Unsupported application command")

    def publish(self):
        e = self.engine
        self.snapshot = copy.deepcopy({
            "settings": e.settings.payload(), "pending": e.pending_settings.payload() if e.pending_settings else None,
            "load": asdict(e.load), "remaining": e.load.remaining, "connections": self.connections,
            "bindings": {k: asdict(v) for k, v in self.bindings.items()}, "hardware": self.hardware,
            "pump": self.pump_status, "phase": self.phase, "progress": self.progress,
            "active": asdict(e.active) if e.active else None, "state": e.state, "enabled": e.enabled,
            "recording": e.recording, "path": str(self.store.path) if self.store else "",
            "test_path": str(self.bench.path) if self.bench else "", "samples": self.samples,
            "target": e.target, "auto_target": e.auto_target, "mix_remaining": max(0., e.ready_at-time.monotonic()),
            "cumulative_ml": e.cumulative_ml, "elapsed": self.elapsed() if self.store else 0,
            "notices": list(self.notices)[-30:], "scan_busy": self.scan_busy,
            "pending_reason": self.pending_reason, "storage_error": self.local_storage_error or (self.store.error if self.store else ""),
            "pending_allowance": self.pending_allowance,
            "confirmed_signature": self.confirmed_signature})

    def run(self):
        try:
            mark_interrupted(self.root/"data")
            while not self.quit.is_set():
                try:
                    for _ in range(min(100, self.incoming.qsize())):
                        name, data = self.incoming.get_nowait()
                        self.handle(name, data)
                    for _ in range(min(10, self.commands.qsize())):
                        name, data = self.commands.get_nowait()
                        self.dispatch(name, data)
                    e = self.engine
                    stores = [s for s in (self.store, self.bench) if s]
                    e.storage_failed = bool(self.local_storage_error) or any(s.failure_age >= 30 for s in stores)
                    if e.storage_failed and (e.enabled or e.active):
                        self.stop("storage_paused")
                    if e.stop_requested:
                        worker = self.workers.get("pump")
                        if worker:
                            worker.stop("alarm" if e.alarm else e.manual_reason)
                        e.stop_requested = False
                    if self.finish_pending and not e.active:
                        if self.store:
                            self.store.close()
                            self.store = None
                        e.recording = False
                        e.manual_reason = ""
                        self.finish_pending = False
                        keep_awake(False)
                    if self.pending_allowance is not None and not e.active and time.monotonic() >= e.ready_at:
                        e.set_allowance(self.pending_allowance)
                        self.pending_allowance = None
                    d = e.tick(time.monotonic())
                    if d:
                        try:
                            self.workers["pump"].submit(d)
                        except Exception:
                            e.active = None
                            e.pause("pump_unavailable")
                            raise
                except Exception as exc:
                    self.notices.append({"time": utc_now(), "kind": "error", "data": {"error": str(exc)}})
                    self.log.exception("Application service")
                self.publish()
                self.quit.wait(.05)
        finally:
            for worker in self.workers.values():
                worker.close()
            for worker in list(self.workers.values()):
                worker.thread.join(timeout=8)
            for store in (self.store, self.bench):
                if store:
                    store.close("interrupted" if self.engine.active else "completed")
            keep_awake(False)
            for handler in list(self.log.handlers):
                handler.close()
                self.log.removeHandler(handler)

    def close(self):
        pump = self.workers.get("pump")
        if pump:
            pump.stop("closing")
        self.quit.set()
