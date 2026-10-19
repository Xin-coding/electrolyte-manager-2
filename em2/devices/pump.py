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


class CounterPending(AccountingError):
    """Counter observation ended without a stable value; use labeled estimates."""


class MotionContinues(Exception):
    """An early stopped report preceded motion; keep observing the same START."""


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
        expected = decode(pair)
        try:
            got = decode(actual)
        except ValueError:
            if register == 0x6a:
                return ["Flow readback could not be decoded; monitoring only"]
            raise
        tolerance = min(decode((1, actual[1])), expected*.001) if register == 0x6a else 1e-10
        if abs(expected-got) > tolerance+1e-10:
            if register == 0x6a:
                return [f"Flow readback differs: requested {expected:g}, reported {got:g} mL/min; monitoring only"]
            raise ConfigurationError("Pump parameter readback mismatch")
        return []

    def status(self, emergency=False):
        r = self.read(0x100, 6, emergency)
        measured = {"error": r[0], "state": r[1] & 15, "read_at": time.monotonic(), "warnings": []}
        for key, decode, pair in (("flow_ml_min",flow,r[2:4]), ("counter_ml",volume,r[4:6]),
                                   ("resolution_ml",volume,(1,r[5]))):
            try:
                measured[key] = decode(pair)
            except ValueError:
                measured[key] = None
                measured["warnings"].append(key+" could not be decoded; monitoring only")
        return measured

    def hardware(self, emergency=False):
        method = self.read(0x5e, 1, emergency)[0]
        syringe = self.read(0x61, 4, emergency)
        force = self.read(0x6e, 1, emergency)[0]
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
        warnings = list(self.pair(0x6a, encoded["refresh_flow"]) or [])
        warnings.extend(self.pair(0x6a, encoded["flow"]) or [])
        verified = self.verify_dose(dose, expected_signature)
        verified["warnings"] = list(dict.fromkeys(warnings+verified["warnings"]))
        return verified

    def verify_dose(self, dose, expected_signature, emergency=False):
        """Read-only reconciliation; never reconfigure or restart a dose."""
        r = self.read(0x68, 15, emergency)
        target = volume(r[:2])
        warnings = []
        try:
            speed = flow(r[2:4])
        except ValueError:
            speed = None
        try:
            seconds = duration(r[13:15])
        except ValueError:
            seconds = None
        expected_s = dose.cap/dose.flow_ml_min*60
        if abs(target-dose.cap) > 1e-10:
            raise ConfigurationError("Pump finite volume limit readback does not match the command")
        time_tolerance = min(TIME_UNITS.get(r[14], 0), max(.1, .1*expected_s))+1e-7
        if seconds is None or abs(seconds-expected_s) > time_tolerance:
            warnings.append(f"Device duration differs from requested duration ({expected_s:g} s); monitoring only")
        speed_tolerance = min(flow((1, r[3])), dose.flow_ml_min*.001)+1e-10 if speed is not None else 0
        if speed is None or abs(speed-dose.flow_ml_min) > speed_tolerance:
            warnings.append("Flow differs from requested flow; monitoring only")
        after = self.hardware(emergency)
        if after["signature"] != expected_signature or after["force_percent"] != dose.force_percent:
            raise ConfigurationError("Pump configuration changed during preparation")
        return {"target_ml": target, "flow_ml_min": speed, "device_duration_s": seconds,
                "expected_duration_s": expected_s, "hardware": after, "warnings": warnings}


def assess_counter(before, after, dose, interrupted=False):
    if not all(isinstance(s.get(k), (float,int)) and math.isfinite(s[k]) and (k != "resolution_ml" or s[k] > 0)
               for s in (before, after) for k in ("counter_ml", "resolution_ml")):
        return None, "unverified"
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
        self.stop_generation = 0

    def start(self):
        self.thread.start()

    def submit(self, dose):
        with self.lock:
            if self.busy or not self.connected or self.context:
                raise ValueError("Pump is not ready for a new dose")
            self.busy = True
            self.queue.put_nowait(("dose", (dose, self.stop_generation)))

    def stop(self, reason="manual_pause"):
        with self.lock:
            self.stop_generation += 1
            self.reason = reason
            self.stop_event.set()

    def service_stop(self):
        with self.lock:
            generation = self.stop_generation
        self.ensure_stopped()
        with self.lock:
            if generation == self.stop_generation:
                self.stop_event.clear()

    def manual_result(self, amount):
        if not self.context or not self.connected:
            raise ValueError("No stopped interrupted dose to resolve")
        self.queue.put_nowait(("resolve", float(amount)))

    def close(self):
        self.close_event.set()
        self.stop("closing")

    def publish_status(self, status):
        self.status = status
        if self.context and status["error"]:
            self.context["error"] = f"Pump protection fault {status['error']}"
            self.context["error_kind"] = "pump_fault"
        if self.context:
            self.context["warnings"].extend(w for w in status.get("warnings", []) if w not in self.context["warnings"])
            requested = self.context["dose"].flow_ml_min
            measured = status.get("flow_ml_min")
            warning = "Reported running flow differs from requested flow; monitoring only"
            if status["state"] == 1 and measured is not None and abs(measured-requested) > requested*.05:
                self.note(warning)
        self.emit("pump_status", status)

    def note(self, message):
        if message not in self.context["warnings"]:
            self.context["warnings"].append(message)
            self.emit("pump_warning", {"id": self.context["dose"].id, "warning": message})

    def ensure_stopped(self, evidence=None):
        evidence = evidence if evidence is not None else {}
        # Cached stopped state can precede a START whose reply was lost.
        # Never make a successful status query a prerequisite for STOP.
        last = None
        for _ in range(2):
            try:
                try:
                    self.transport.write(1, 0, emergency=True)
                    evidence["stop_ack_at"] = time.monotonic()
                except Exception as exc:
                    last = exc  # STOP may have executed despite a lost reply.
                finally:
                    evidence["stop_sent_at"] = self.transport.last_tx
                current = self.transport.status(emergency=True)
                self.publish_status(current)
                if current["state"] == 3:
                    evidence.setdefault("stopped_at", current["read_at"])
                    return current
                last = RuntimeError("Pump has not confirmed stopped")
            except Exception as exc:
                last = exc
        raise CommunicationError("Pump stop is unconfirmed: "+str(last))

    def settle(self, initial):
        begin = stable_at = time.monotonic()
        previous = initial
        while time.monotonic()-begin < 3:
            time.sleep(.2)
            current = self.transport.status(emergency=True)
            self.publish_status(current)
            if current["state"] != 3:
                raise MotionContinues()
            # Stability is not an allowance for dose error. Even one counter
            # tick restarts the stable window; scale changes do so as well.
            if any(s.get(k) is None for s in (current,previous) for k in ("counter_ml","resolution_ml")):
                return current
            if (not math.isclose(current["counter_ml"], previous["counter_ml"], rel_tol=0.,
                                 abs_tol=min(current["resolution_ml"], previous["resolution_ml"])*1e-6)
                    or current["resolution_ml"] != previous["resolution_ml"]):
                stable_at = time.monotonic()
            previous = current
            if time.monotonic()-begin >= 1.2 and time.monotonic()-stable_at >= .4:
                return current
        raise CounterPending("Final pump counter has not settled")

    def _finish_context(self, recovered=False):
        ctx = self.context
        dose = ctx["dose"]
        evidence = ctx["evidence"]
        force_stop = bool(ctx["interrupted"] or ctx["error"] and ctx["started"]
                          or ctx.get("error_kind") in {"communication_error", "pump_fault"}
                          or recovered or self.stop_event.is_set()
                          or evidence.get("software_cutoff_reached") or evidence.get("volume_limit_reached"))
        if force_stop:
            stopped = self.ensure_stopped(evidence)
        else:
            stopped = self.transport.status(emergency=True)
            self.publish_status(stopped)
            if stopped["state"] != 3:
                raise MotionContinues()
            evidence.setdefault("stopped_at", stopped["read_at"])
        stopped_at = evidence["stopped_at"]
        # Capture identity before preparation, including failures before START.
        # Recovery is permitted only after read-only configuration verification.
        recovery = recovered or ctx.get("error_kind") == "communication_error"
        if recovery:
            actual = self.transport.hardware(emergency=True)
            if actual["signature"] != ctx["signature"]:
                raise ConfigurationError("Syringe configuration changed during interrupted dose")
            if ctx["started"]:
                verified = self.transport.verify_dose(dose, ctx["signature"], emergency=True)
                ctx["warnings"].extend(w for w in verified.get("warnings", []) if w not in ctx["warnings"])
            self.hardware = actual
            self.emit("pump_hardware", actual)
        stable = True
        try:
            final = self.settle(stopped)
        except MotionContinues:
            if force_stop:
                self.ensure_stopped(evidence)
                raise CommunicationError("Pump motion continued after requested STOP")
            evidence.pop("stopped_at", None)
            raise
        except CounterPending as exc:
            stable = False
            final = self.status
            ctx["warnings"].append(str(exc)+"; using programmed volume estimate")
        if not ctx["started"]:
            amount, source = 0., "not_started"
            if ctx["before"] and final.get("counter_ml") != ctx["before"].get("counter_ml"):
                ctx["warnings"].append("Counter changed before START; no software injection was started")
        else:
            amount, source = assess_counter(ctx["before"], final, dose,
                                            ctx["interrupted"] or recovery or dose.purpose == "stop_test")
            if not stable or amount is None or amount == 0:
                # Counter precision never gates feedback. Charge the finite
                # programmed cap once when the counter is unusable; do not
                # pretend it is a physical measurement or an empty injection.
                amount, source = dose.cap, "programmed_cap_estimate"
                ctx["warnings"].append("Counter unavailable, inconsistent or unchanged; programmed finite volume recorded as estimate")
            elif source == "bounded_estimate":
                ctx["warnings"].append("Counter resolution is coarse; programmed volume recorded as estimate")
        evidence.update(before=ctx["before"], final=final, recovered=recovery,
                        expected_s=dose.expected_s, hardware_cap_ml=dose.cap,
                        stop_reason=self.reason, error=ctx["error"])
        if dose.purpose == "stop_test" and ctx["before"]:
            resolution = (ctx["before"].get("resolution_ml") or 0)+(final.get("resolution_ml") or 0)
            margin = dose.cap-(amount or 0)-resolution
            pre = evidence.get("pre_stop")
            evidence["stop_test_passed"] = bool(ctx["started"] and not ctx["error"] and
                evidence.get("software_cutoff_reached") and
                pre and pre["state"] == 1 and evidence.get("stop_ack_at") and
                evidence.get("stop_sent_at", 0) >= pre["read_at"] and
                evidence.get("stop_sent_at", 0)-pre["read_at"] <= 1 and
                source == "device_counter" and margin > 0 and
                stopped_at < evidence.get("started_at", 0)+dose.cap/dose.flow_ml_min*60)
        error, error_kind = ctx["error"], ctx.get("error_kind", "")
        if recovery and amount is not None and error_kind == "communication_error":
            evidence["recovered_error"] = error
            error = error_kind = ""
        if ctx["started"] and amount is not None:
            evidence["deviation_ml"] = amount-dose.volume_ml
            if not ctx["interrupted"] and not recovery and dose.purpose != "stop_test":
                tolerance = max((ctx["before"].get("resolution_ml") or 0)+(final.get("resolution_ml") or 0), dose.volume_ml*.05)+1e-10
                if abs(amount-dose.volume_ml) > tolerance:
                    ctx["warnings"].append(f"Delivered volume differs from request by {amount-dose.volume_ml:+g} mL; monitoring only")
        if final["error"]:
            error = f"Pump protection fault {final['error']}"
            error_kind = "pump_fault"
        result = DoseResult(dose.id, True, stopped_at, amount, source, ctx["started"], error,
                            ctx["interrupted"], recovery, evidence, error_kind, list(dict.fromkeys(ctx["warnings"])))
        self.context = None
        self.busy = False
        self.emit("dose_result", asdict(result))
        return True

    def execute(self, dose, generation=None):
        if generation is None:
            generation = self.stop_generation
        self.context = {"dose": dose, "started": False, "before": None,
                        "signature": self.hardware["signature"],
                        "error": "", "error_kind": "", "interrupted": False, "evidence": {}, "warnings": []}
        ctx = self.context
        phase = "preflight"
        try:
            if generation != self.stop_generation or self.stop_event.is_set() or self.close_event.is_set():
                raise Cancelled("Queued dose cancelled by STOP")
            self.reason = ""
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
            ctx["warnings"].extend(verified.get("warnings", []))
            fresh = self.transport.status()
            self.publish_status(fresh)
            if fresh["error"]:
                raise ProtectionError(f"Pump protection fault {fresh['error']}")
            if fresh["state"] != 3:
                raise ConfigurationError("Pump stopped state changed before START")
            ctx["before"] = fresh
            if not self.authorize(dose, {"before": fresh, "hardware": self.hardware}):
                self.note("START authorization declined; no injection was started")
                raise Cancelled("Injection intent could not be persisted or was cancelled")
            if generation != self.stop_generation or self.stop_event.is_set() or self.close_event.is_set():
                raise Cancelled("Cancelled before START")
            phase = "starting"
            ctx["started"] = True  # START can execute even if its reply is lost.
            try:
                self.transport.write(1, 1)
            finally:
                started = self.transport.last_tx
                ctx["evidence"]["started_at"] = started
            self.emit("pump_phase", {"phase": "injecting", "id": dose.id, "started_at": started})
            self.observe_motion(ctx)
        except Cancelled:
            ctx["interrupted"] = ctx["started"]
        except Exception as exc:
            ctx["error"] = f"{phase}: {exc}"
            ctx["error_kind"] = failure_kind(exc)
            ctx["interrupted"] = ctx["started"]
        finally:
            self.emit("pump_phase", {"phase": "confirming_stop", "id": dose.id})
            while self.context:
                try:
                    self._finish_context()
                    break
                except MotionContinues:
                    if not ctx["started"]:
                        self.pending(ConfigurationError("Pump is moving without this command; wait for the existing operation"))
                        break
                    ctx["evidence"].pop("native_stop_at", None)
                    self.emit("pump_phase", {"phase": "injecting", "id": dose.id})
                    try:
                        self.observe_motion(ctx)
                    except Exception as exc:
                        ctx["error"], ctx["error_kind"] = str(exc), failure_kind(exc)
                        ctx["interrupted"] = True
                except Exception as exc:
                    # A failure in final status acquisition is a connection
                    # problem: attempt STOP before handing over to reconnect.
                    if failure_kind(exc) == "communication_error":
                        ctx["error"], ctx["error_kind"], ctx["interrupted"] = str(exc), "communication_error", ctx["started"]
                        try:
                            self._finish_context(recovered=True)
                            break
                        except Exception as final_exc:
                            exc = final_exc
                    self.pending(exc)
                    break

    def observe_motion(self, ctx):
        dose, evidence = ctx["dose"], ctx["evidence"]
        started = evidence["started_at"]
        observed_running = evidence.get("running_observed", False)
        next_poll, next_progress = time.monotonic()+.2, 0.
        limit_confirmations = 0
        while True:
            if self.stop_event.wait(.02) or self.close_event.is_set():
                ctx["interrupted"] = True
                return
            now = time.monotonic()
            if now >= next_progress:
                self.emit("pump_progress", {"id": dose.id, "estimate_ml": min(dose.cap, (now-started)*dose.flow_ml_min/60), "elapsed_s": now-started})
                next_progress = now+1
            if now < next_poll:
                continue
            s = self.transport.status()
            self.publish_status(s)
            if s["error"]:
                raise ProtectionError(f"Pump protection fault {s['error']}")
            delta, source = assess_counter(ctx["before"], s, dose, interrupted=True)
            tolerance = (s.get("resolution_ml") or 0)+(ctx["before"].get("resolution_ml") or 0)
            if s["state"] == 3:
                full = delta is not None and 0 < delta and delta >= dose.cap-tolerance
                if observed_running or full or now >= started+2.+dose.expected_s:
                    evidence["native_stop_at"] = s["read_at"]
                    return
                next_poll = now+.2
                continue
            if s["state"] != 1:
                raise CommunicationError("Unexpected pump motion state")
            observed_running = evidence["running_observed"] = True
            if now > started+2.+dose.expected_s:
                warning = "Pump duration exceeds the requested-time estimate; monitoring only"
                self.note(warning)
            # A counter outlier is not a STOP condition. A plausible finite-cap
            # reading must persist across two real replies to supervise volume.
            limit_confirmations = limit_confirmations+1 if delta is not None and delta >= dose.cap else 0
            if limit_confirmations >= 2:
                evidence["volume_limit_reached"] = True
                return
            if dose.purpose == "stop_test" and (delta is not None and delta >= dose.volume_ml or now-started >= dose.expected_s):
                # This is an explicitly requested STOP test, not a precision or
                # duration fault in ordinary feedback operation.
                evidence["software_cutoff_reached"] = True
                evidence["software_cutoff_basis"] = "device_counter" if delta is not None and delta >= dose.volume_ml else "requested_test_time"
                evidence["pre_stop"] = s
                return
            next_poll = now+.5

    def pending(self, exc):
        ctx = self.context
        evidence = ctx["evidence"]
        ctx["retry_at"] = float("inf")
        stopped = bool(self.status and self.status["state"] == 3 and evidence.get("stopped_at"))
        if str(exc) != self.last_notice:
            self.last_notice = str(exc)
            self.emit("pump_phase", {"phase": "waiting_pump", "id": ctx["dose"].id})
            self.emit("dose_pending", asdict(DoseResult(ctx["dose"].id, stopped,
                evidence.get("stopped_at", time.monotonic()), None, "unverified", ctx["started"],
                str(exc), ctx["interrupted"], evidence=evidence, error_kind=failure_kind(exc))))
        if failure_kind(exc) == "communication_error":
            # This also covers a failure during the final verification itself.
            if not ctx["error"]:
                ctx["error"], ctx["error_kind"] = str(exc), "communication_error"
            ctx["interrupted"] = ctx["started"]
            self.connected = False
            raise exc

    def reconcile(self, reconnected=False):
        if not self.context:
            return
        ctx = self.context
        if not reconnected and time.monotonic() < ctx.get("retry_at", float("inf")):
            return
        try:
            self._finish_context(recovered=reconnected)
        except Exception as exc:
            self.pending(exc)

    def run(self):
        from .sensors import resolve
        while not self.close_event.is_set():
            try:
                endpoint = resolve(self.binding)
                self.transport = self.transport_factory(endpoint["port"], self.log, self.stop_event)
                self.transport.open()
                if self.stop_event.is_set() or self.context:
                    self.service_stop()
                self.hardware = self.transport.hardware()
                self.publish_status(self.transport.status())
                self.connected = True
                self.emit("pump_hardware", self.hardware)
                self.emit("connection", {"kind": "pump", "connected": True, "endpoint": endpoint})
                if self.context:
                    self.reconcile(reconnected=True)
                next_poll = next_hardware = 0.
                while not self.close_event.is_set():
                    if self.stop_event.is_set():
                        self.service_stop()
                    try:
                        kind, data = self.queue.get(timeout=.05)
                    except queue.Empty:
                        kind = None
                    if kind == "dose":
                        self.execute(*data)
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
                    self.reconcile()
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
                        self.service_stop()
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
