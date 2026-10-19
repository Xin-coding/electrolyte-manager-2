"""Single-owner ASP transport. Only STOP is retried; START is never retried."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import queue
import threading
import time

import serial

from ..models import Dose, DoseResult
from ..parameters import pump_parameters
from ..errors import ParameterError, ConfigurationError, ProtectionError, AccountingError, CommunicationError, failure_kind
from .wire import TIME_UNITS, duration, encode, flow, packet, response, volume


class Cancelled(Exception):
    pass


class PumpTransport:
    def __init__(self, port, log=lambda *_: None, stop_event=None):
        self.port, self.log = port, log
        self.stop_event = stop_event or threading.Event()
        self.io = None
        self.last_end = self.last_tx = 0.

    def open(self):
        self.io = serial.Serial(self.port, 115200, timeout=.5, write_timeout=.5)

    def close(self):
        if self.io:
            self.io.close()

    def exchange(self, function, register, values, size, emergency=False):
        delay = max(0., .15-(time.monotonic()-self.last_end))
        if emergency:
            time.sleep(delay)
        elif self.stop_event.wait(delay):
            raise Cancelled("Stop requested")
        if self.stop_event.is_set() and not emergency:
            raise Cancelled("Stop requested")
        request = packet(function, register, values)
        self.io.reset_input_buffer()
        self.log("tx", request.hex())
        self.last_tx = time.monotonic()
        self.io.write(request)
        answer = self.io.read(size)
        self.last_end = time.monotonic()
        self.log("rx", answer.hex())
        if not answer:
            raise TimeoutError("Pump response timed out")
        return response(answer, function)

    def read(self, register, count, emergency=False):
        answer = self.exchange(3, register, count, 5+2*count, emergency)
        if len(answer) != count:
            raise CommunicationError("Pump register count mismatch")
        return answer

    def write(self, register, value, emergency=False):
        if self.exchange(6, register, value, 8, emergency) != (register, value):
            raise CommunicationError("Pump write acknowledgement mismatch")

    def checked(self, register, value):
        self.write(register, value)
        if self.read(register, 1) != [value]:
            raise ConfigurationError("Pump parameter readback mismatch")

    def pair(self, register, pair):
        if self.exchange(16, register, pair, 8) != (register, 2):
            raise CommunicationError("Pump pair acknowledgement mismatch")
        actual = self.read(register, 2)
        decode = flow if register == 0x6a else volume
        expected, got = decode(pair), decode(actual)
        tolerance = min(decode((1, actual[1])), expected*.001) if register == 0x6a else 1e-10
        if abs(expected-got) > tolerance+1e-10:
            raise ConfigurationError("Pump parameter readback mismatch")

    def status(self, emergency=False):
        r = self.read(0x100, 6, emergency)
        return {"error": r[0], "state": r[1] & 15, "flow_ml_min": flow(r[2:4]),
                "counter_ml": volume(r[4:6]), "resolution_ml": volume((1, r[5])), "read_at": time.monotonic()}

    def hardware(self):
        method = self.read(0x5e, 1)[0]
        syringe = self.read(0x61, 4)
        force = self.read(0x6e, 1)[0]
        capacity = volume(syringe[:2])
        if not 94 <= syringe[3] <= 100 or capacity <= 0 or not 20 <= force <= 100:
            raise ValueError("Invalid pump hardware configuration")
        area = syringe[2]*10.**(syringe[3]-100)
        if area <= 0:
            raise ValueError("Invalid syringe effective area")
        signature = hashlib.sha256(json.dumps([method, syringe]).encode()).hexdigest()[:20]
        return {"method": method, "capacity_ml": capacity, "area_mm2": area, "force_percent": force,
                "signature": signature, "syringe_registers": syringe,
                "model_name": None, "calibration_history": None}

    def configure_dose(self, dose, expected_signature):
        encoded = pump_parameters(dose.cap, dose.flow_ml_min, dose.force_percent, dose.mix_s)
        hardware = self.hardware()
        if hardware["signature"] != expected_signature:
            raise ConfigurationError("Syringe configuration changed; review hardware information")
        if dose.cap > hardware["capacity_ml"]+1e-10:
            raise ParameterError("Dose exceeds syringe capacity")
        self.checked(0x6e, encoded["force"])
        self.checked(0x60, 1)
        self.checked(0x74, 1)
        self.pair(0x68, encoded["volume"])
        self.pair(0x6a, encoded["refresh_flow"])
        self.pair(0x6a, encoded["flow"])
        r = self.read(0x68, 15)
        target, speed, seconds = volume(r[:2]), flow(r[2:4]), duration(r[13:15])
        expected_s = dose.cap/dose.flow_ml_min*60
        time_tolerance = min(TIME_UNITS[r[14]], max(.1, .1*expected_s))+1e-7
        speed_tolerance = min(flow((1, r[3])), dose.flow_ml_min*.001)+1e-10
        if abs(target-dose.cap) > 1e-10 or abs(speed-dose.flow_ml_min) > speed_tolerance or abs(seconds-expected_s) > time_tolerance:
            raise ConfigurationError("Pump volume, flow and derived duration do not agree")
        after = self.hardware()
        if after["signature"] != expected_signature or after["force_percent"] != dose.force_percent:
            raise ConfigurationError("Pump configuration changed during preparation")
        return {"target_ml": target, "flow_ml_min": speed, "device_duration_s": seconds,
                "expected_duration_s": expected_s, "hardware": after}


def assess_counter(before, after, dose, interrupted=False):
    delta = after["counter_ml"]-before["counter_ml"]
    tolerance = before["resolution_ml"]+after["resolution_ml"]+1e-10
    if delta < -tolerance or delta > dose.cap+tolerance:
        return None, "unverified"
    delta = max(0., delta)
    if max(before["resolution_ml"], after["resolution_ml"]) > dose.volume_ml*.25:
        return (None, "unverified") if interrupted else (dose.volume_ml, "bounded_estimate")
    return delta, "device_counter"


class PumpWorker:
    def __init__(self, binding, emit, authorize, log=lambda *_: None, transport_factory=PumpTransport):
        self.binding, self.emit, self.authorize, self.log = binding, emit, authorize, log
        self.transport_factory = transport_factory
        self.stop_event = threading.Event()
        self.close_event = threading.Event()
        self.queue = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self.run, name="em2-pump", daemon=True)
        self.transport = None
        self.status = None
        self.hardware = None
        self.context = None
        self.busy = False
        self.connected = False
        self.lock = threading.Lock()
        self.reason = ""
        self.last_notice = ""

    def start(self):
        self.thread.start()

    def submit(self, dose):
        with self.lock:
            if self.busy or not self.connected or self.context:
                raise ValueError("Pump is not ready for a new dose")
            self.stop_event.clear()
            self.reason = ""
            self.busy = True
            self.queue.put_nowait(("dose", dose))

    def stop(self, reason="manual_pause"):
        self.reason = reason
        self.stop_event.set()

    def manual_result(self, amount):
        if not self.context or not self.connected:
            raise ValueError("No stopped interrupted dose to resolve")
        self.queue.put_nowait(("resolve", float(amount)))

    def close(self):
        self.close_event.set()
        self.stop("closing")

    def publish_status(self, status):
        self.status = status
        self.emit("pump_status", status)

    def ensure_stopped(self, evidence=None):
        evidence = evidence if evidence is not None else {}
        if self.status and self.status["state"] == 3:
            fresh = self.transport.status(emergency=True)
            self.publish_status(fresh)
            if fresh["state"] == 3:
                evidence["stopped_at"] = fresh["read_at"]
                return fresh
        last = None
        for _ in range(2):
            try:
                try:
                    self.transport.write(1, 0, emergency=True)
                    evidence["stop_ack_at"] = time.monotonic()
                finally:
                    evidence["stop_sent_at"] = self.transport.last_tx
                current = self.transport.status(emergency=True)
                self.publish_status(current)
                if current["state"] == 3:
                    evidence["stopped_at"] = current["read_at"]
                    return current
                last = RuntimeError("Pump has not confirmed stopped")
            except Exception as exc:
                last = exc
        raise RuntimeError("Pump stop is unconfirmed: "+str(last))

    def settle(self, initial):
        begin = stable_at = time.monotonic()
        previous = initial
        while time.monotonic()-begin < 3:
            time.sleep(.2)
            current = self.transport.status(emergency=True)
            self.publish_status(current)
            if current["state"] != 3:
                self.ensure_stopped()
                raise AccountingError("Pump moved during final accounting")
            # Stability is not an allowance for dose error. Even one counter
            # tick restarts the stable window; scale changes do so as well.
            if (not math.isclose(current["counter_ml"], previous["counter_ml"], rel_tol=0.,
                                 abs_tol=min(current["resolution_ml"], previous["resolution_ml"])*1e-6)
                    or current["resolution_ml"] != previous["resolution_ml"]):
                stable_at = time.monotonic()
            previous = current
            if time.monotonic()-begin >= 1.2 and time.monotonic()-stable_at >= .4:
                return current
        raise AccountingError("Final pump counter has not settled")

    def _finish_context(self, recovered=False):
        ctx = self.context
        dose = ctx["dose"]
        evidence = ctx["evidence"]
        stopped = self.ensure_stopped(evidence)
        stopped_at = evidence["stopped_at"]
        final = self.settle(stopped)
        if not ctx["started"]:
            amount, source = 0., "not_started"
        else:
            amount, source = assess_counter(ctx["before"], final, dose,
                                            ctx["interrupted"] or recovered or dose.purpose == "stop_test")
        evidence.update(before=ctx["before"], final=final, recovered=recovered,
                        expected_s=dose.expected_s, hardware_cap_ml=dose.cap,
                        stop_reason=self.reason, error=ctx["error"])
        if dose.purpose == "stop_test" and ctx["before"]:
            margin = dose.cap-(amount or 0)-(ctx["before"]["resolution_ml"]+final["resolution_ml"])
            pre = evidence.get("pre_stop")
            evidence["stop_test_passed"] = bool(ctx["started"] and not ctx["error"] and
                evidence.get("software_cutoff_reached") and
                pre and pre["state"] == 1 and evidence.get("stop_ack_at") and
                evidence.get("stop_sent_at", 0) >= pre["read_at"] and
                evidence.get("stop_sent_at", 0)-pre["read_at"] <= 1 and
                amount is not None and margin > 0 and
                stopped_at < evidence.get("started_at", 0)+dose.cap/dose.flow_ml_min*60)
        error, error_kind = ctx["error"], ctx.get("error_kind", "")
        if ctx["started"] and amount is not None:
            evidence["deviation_ml"] = amount-dose.volume_ml
            if not ctx["interrupted"] and not recovered and dose.purpose != "stop_test":
                tolerance = max(ctx["before"]["resolution_ml"]+final["resolution_ml"], dose.volume_ml*.05)+1e-10
                if abs(amount-dose.volume_ml) > tolerance:
                    error = "Pump stopped with verified under-delivery; inspect pump and tubing"
                    error_kind = "accounting_error"
        if final["error"]:
            error = f"Pump protection fault {final['error']}"
            error_kind = "pump_fault"
        result = DoseResult(dose.id, True, stopped_at, amount, source, ctx["started"], error,
                            ctx["interrupted"], recovered, evidence, error_kind)
        if amount is None:
            self.emit("dose_pending", asdict(result))
            return False
        self.context = None
        self.busy = False
        self.emit("dose_result", asdict(result))
        return True

    def execute(self, dose):
        self.context = {"dose": dose, "started": False, "before": None,
                        "error": "", "error_kind": "", "interrupted": False, "evidence": {}}
        ctx = self.context
        phase = "preflight"
        try:
            initial = self.transport.status()
            self.publish_status(initial)
            if initial["error"]:
                raise ProtectionError(f"Pump protection fault {initial['error']}")
            if initial["state"] != 3:
                raise ConfigurationError("Pump must be stopped before preparation")
            ctx["before"] = initial
            signature = self.hardware["signature"]
            phase = "preparing"
            self.emit("pump_phase", {"phase": phase, "id": dose.id})
            verified = self.transport.configure_dose(dose, signature)
            self.hardware = verified["hardware"]
            self.emit("pump_hardware", self.hardware)
            ctx["evidence"]["parameters"] = verified
            fresh = self.transport.status()
            self.publish_status(fresh)
            if fresh["error"]:
                raise ProtectionError(f"Pump protection fault {fresh['error']}")
            if fresh["state"] != 3:
                raise ConfigurationError("Pump stopped state changed before START")
            ctx["before"] = fresh
            if not self.authorize(dose, {"before": fresh, "hardware": self.hardware}):
                raise RuntimeError("Injection intent could not be persisted or was cancelled")
            if self.stop_event.is_set() or self.close_event.is_set():
                raise Cancelled("Cancelled before START")
            phase = "starting"
            ctx["started"] = True  # START can execute even if its reply is lost.
            try:
                self.transport.write(1, 1)
            finally:
                started = self.transport.last_tx
                ctx["evidence"]["started_at"] = started
            self.emit("pump_phase", {"phase": "injecting", "id": dose.id, "started_at": started})
            # START acknowledgement precedes physical motion. The finite pump
            # target is authoritative; a bounded startup margin supervises it.
            deadline = started+(dose.cap/dose.flow_ml_min*60-.6 if dose.purpose == "stop_test" else dose.expected_s+max(.6, dose.expected_s*.02))
            next_poll, next_progress = started+.2, started
            while time.monotonic() < deadline:
                now = time.monotonic()
                if self.stop_event.wait(.02) or self.close_event.is_set():
                    ctx["interrupted"] = True
                    break
                if now >= next_progress:
                    self.emit("pump_progress", {"id": dose.id, "estimate_ml": min(dose.cap, (now-started)*dose.flow_ml_min/60), "elapsed_s": now-started})
                    next_progress = now+1
                if now >= next_poll and deadline-now > .7:
                    s = self.transport.status()
                    self.publish_status(s)
                    if s["error"]:
                        raise ProtectionError(f"Pump protection fault {s['error']}")
                    if s["state"] == 3:
                        ctx["evidence"]["native_stop_at"] = s["read_at"]
                        break
                    if dose.purpose != "stop_test" and s["counter_ml"]-ctx["before"]["counter_ml"] >= dose.cap:
                        break
                    if dose.purpose == "stop_test" and s["counter_ml"]-ctx["before"]["counter_ml"] >= dose.volume_ml:
                        ctx["evidence"]["software_cutoff_reached"] = True
                        ctx["evidence"]["software_cutoff_basis"] = "device_counter"
                        break
                    if s["state"] != 1:
                        raise ConfigurationError("Unexpected pump motion state")
                    next_poll = time.monotonic()+.5
            if dose.purpose == "stop_test" and not ctx["interrupted"]:
                ctx["evidence"]["pre_stop"] = self.transport.status(emergency=True)
                self.publish_status(ctx["evidence"]["pre_stop"])
        except Cancelled:
            ctx["interrupted"] = True
        except Exception as exc:
            ctx["error"] = f"{phase}: {exc}"
            ctx["error_kind"] = failure_kind(exc)
            ctx["interrupted"] = ctx["started"]
        finally:
            self.emit("pump_phase", {"phase": "confirming_stop", "id": dose.id})
            try:
                if not self._finish_context():
                    self.last_notice = "Dose accounting needs review"
            except Exception as exc:
                self.connected = False
                self.emit("dose_pending", asdict(DoseResult(dose.id, False, time.monotonic(), None,
                    "unverified", ctx["started"], str(exc), ctx["interrupted"], evidence=ctx["evidence"], error_kind=failure_kind(exc))))
                raise
            finally:
                self.stop_event.clear()

    def run(self):
        from .sensors import resolve
        while not self.close_event.is_set():
            try:
                endpoint = resolve(self.binding)
                self.transport = self.transport_factory(endpoint["port"], self.log, self.stop_event)
                self.transport.open()
                if self.stop_event.is_set():
                    self.status = self.transport.status(emergency=True)
                    self.ensure_stopped()
                    self.stop_event.clear()
                self.hardware = self.transport.hardware()
                self.publish_status(self.transport.status())
                self.connected = True
                self.emit("pump_hardware", self.hardware)
                self.emit("connection", {"kind": "pump", "connected": True, "endpoint": endpoint})
                if self.context:
                    self.ensure_stopped()
                    expected = self.context["evidence"].get("parameters", {}).get("hardware", {}).get("signature")
                    if expected == self.hardware["signature"]:
                        # A trustworthy final count resolves communication interruption.
                        if not self.status["error"] and self.context.get("error_kind") == "communication_error":
                            self.context["error"] = self.context["error_kind"] = ""
                        try:
                            self._finish_context(recovered=True)
                        except Exception as exc:
                            self.emit("pump_notice", {"error": str(exc)})
                next_poll = next_hardware = 0.
                while not self.close_event.is_set():
                    if self.stop_event.is_set():
                        self.publish_status(self.ensure_stopped())
                        self.stop_event.clear()
                    try:
                        kind, data = self.queue.get(timeout=.05)
                    except queue.Empty:
                        kind = None
                    if kind == "dose":
                        self.execute(data)
                    elif kind == "resolve":
                        if not self.context or data < 0 or not math.isfinite(data):
                            raise ValueError("Invalid interrupted dose amount")
                        s = self.transport.status()
                        if s["state"] != 3 or s["error"]:
                            raise ValueError("Pump must be stopped before resolving a dose")
                        result = DoseResult(self.context["dose"].id, True, s["read_at"], data, "operator_confirmed",
                                            self.context["started"], evidence={"manual_review": True})
                        self.context = None
                        self.busy = False
                        self.emit("dose_result", asdict(result))
                    now = time.monotonic()
                    try:
                        if not self.busy and now >= next_poll:
                            self.publish_status(self.transport.status())
                            next_poll = now+2
                        if not self.busy and now >= next_hardware:
                            self.hardware = self.transport.hardware()
                            self.emit("pump_hardware", self.hardware)
                            next_hardware = time.monotonic()+30
                    except Cancelled:
                        self.ensure_stopped()
                        self.stop_event.clear()
            except Exception as exc:
                self.connected = False
                self.emit("connection", {"kind": "pump", "connected": False, "error": str(exc)})
            finally:
                if self.transport:
                    self.transport.close()
            self.close_event.wait(2)
        if self.transport:
            try:
                self.transport = self.transport_factory(resolve(self.binding)["port"], self.log, self.stop_event)
                self.transport.open()
                self.publish_status(self.ensure_stopped())
            except Exception as exc:
                self.emit("pump_notice", {"error": str(exc)})
            finally:
                self.transport.close()
        self.emit("connection", {"kind": "pump", "connected": False, "closed": True})
