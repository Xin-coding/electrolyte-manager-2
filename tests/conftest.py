"""Unit/UI tests must never open a physical serial port."""
import pytest
import serial


@pytest.fixture(autouse=True)
def block_physical_serial(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Physical serial access is forbidden in automated tests")
    monkeypatch.setattr(serial, "Serial", blocked)
