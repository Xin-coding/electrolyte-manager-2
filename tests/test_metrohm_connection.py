"""Serial fixtures only: these regressions must never open physical ports."""
import threading

import pytest
import serial

from em2.devices import sensors
from em2.runtime import Runtime
import em2.runtime as runtime_module


class MeterPort:
    def __init__(self, *, cts, dsr, firmware='"5.781.0020"', **settings):
        self.cts, self.dsr, self.firmware = cts, dsr, firmware
        self.settings = settings
        self.rtscts, self.dsrdtr = settings['rtscts'], settings['dsrdtr']
        self.closed = False
        self.commands = []
        self.pending = bytearray()

    @property
    def in_waiting(self):
        return len(self.pending)

    def reset_input_buffer(self):
        self.pending.clear()

    def write(self, payload):
        self.commands.append(payload)
        if self.rtscts and not self.cts or self.dsrdtr and not self.dsr:
            raise serial.SerialTimeoutException('Write timeout')
        replies = {
            b'&Config.Aux.Prog $Q\r\n': self.firmware,
            b'&Mode.Select $Q\r\n': '"pH"',
            b'&Info.ActualInfo.MeasValue $Q\r\n': '.Primary"7.344"\r\n..Secondary"21.5"',
        }
        self.pending.extend((replies[payload]+'\r\r\n').encode('ascii'))
        return len(payload)

    def read(self, size):
        # Exercise response framing over multiple serial reads.
        size = min(size, 7)
        data = bytes(self.pending[:size])
        del self.pending[:size]
        return data

    def close(self):
        self.closed = True


def install_port(monkeypatch, cts, dsr, **kwargs):
    created = []
    def factory(port, baud, **settings):
        assert port == 'fixture' and baud == 9600
        assert settings['bytesize'] == 8 and settings['parity'] == 'N' and settings['stopbits'] == 1
        assert not settings['xonxoff']
        result = MeterPort(cts=cts, dsr=dsr, **kwargs, **settings)
        created.append(result)
        return result
    monkeypatch.setattr(sensors.serial, 'Serial', factory)
    return created


@pytest.mark.parametrize('cts,dsr,expected', [(False, True, 'DTR/DSR'),
                                           (True, False, 'RTS/CTS'),
                                           (True, True, 'RTS/CTS')])
def test_hardware_readiness_supports_both_cables(monkeypatch, cts, dsr, expected):
    created = install_port(monkeypatch, cts, dsr)
    meter = sensors.Metrohm('fixture')
    try:
        info = meter.open()
        assert info['serial_settings'] == '9600 8N1 · '+expected
        assert meter.read() == (7.344, 21.5)
        assert len(created[0].commands) == 3
        assert created[0].rtscts != created[0].dsrdtr
    finally:
        meter.close()
    assert created[0].closed


def test_missing_readiness_is_not_bypassed_and_failed_open_releases_port(monkeypatch):
    created = install_port(monkeypatch, False, False)
    with pytest.raises(TimeoutError, match='CTS=0, DSR=0'):
        sensors.Metrohm('fixture').open()
    assert created[0].closed
    assert created[0].rtscts and not created[0].dsrdtr
    assert created[0].commands == [b'&Config.Aux.Prog $Q\r\n']


def test_wrong_identity_releases_port_without_measurement(monkeypatch):
    created = install_port(monkeypatch, False, True, firmware='"OTHER"')
    with pytest.raises(ValueError, match='Expected Metrohm 781'):
        sensors.Metrohm('fixture').open()
    assert created[0].closed and len(created[0].commands) == 1


def test_detection_never_sends_other_protocols_to_identified_metrohm(monkeypatch):
    endpoint = {'port': 'fixture', 'identity': 'fixture-id'}
    monkeypatch.setattr(runtime_module, 'ports', lambda: [endpoint])
    probes = []
    def identify(kind, port, log):
        probes.append((kind, port))
        assert kind == 'ph', 'Wrong-baud probe must not precede a working Metrohm identity'
        return {'model': 'Metrohm 781'}
    monkeypatch.setattr(runtime_module, 'identify', identify)
    r = Runtime.__new__(Runtime)
    r.scan_busy, r.bindings, r.workers = False, {}, {}
    r.trace = lambda *args: None
    finished = threading.Event()
    result = []
    def emit(event, data):
        result.append((event, data))
        finished.set()
    r.emit = emit
    r.scan()
    assert finished.wait(3)
    assert probes == [('ph', 'fixture')]
    assert result[0][0] == 'scan_done'
    assert result[0][1]['found'][0][0].kind == 'ph'
