import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore as C, QtWidgets as W, QtTest
from em2.integer_input import IntegerInput, unsigned_integer
from em2.runtime import Runtime
from em2.ui import Window


@pytest.mark.parametrize("text", ["", " ", " 1", "1 ", "+1", "-1", "1.0", "1e2", "1,2", "abc", "１", "一", "NaN", "9"*32])
def test_rejects_every_non_digit_draft(text):
    with pytest.raises(ValueError): unsigned_integer(text, 999)


@pytest.mark.parametrize("text,result", [("0",0),("1",1),("001",1),("999",999)])
def test_plain_integer_digits(text,result):
    assert unsigned_integer(text,999)==result


@pytest.mark.parametrize("scale", [1,1.25,1.5])
def test_step_buttons_mouse_and_keyboard_and_invalid_drafts(scale):
    app=W.QApplication.instance() or W.QApplication([])
    edit=IntegerInput(59,1); edit.resize(round(120*scale),round(40*scale)); edit.show(); app.processEvents()
    try:
        QtTest.QTest.mouseClick(edit.up,C.Qt.MouseButton.LeftButton)
        assert edit.value()==2
        QtTest.QTest.mouseClick(edit.down,C.Qt.MouseButton.LeftButton)
        assert edit.value()==1
        QtTest.QTest.keyClick(edit.edit,C.Qt.Key.Key_Up)
        assert edit.value()==2
        edit.setValue(59); edit.up.click(); assert edit.value()==59
        edit.setValue(0); edit.down.click(); assert edit.value()==0
        edit.edit.setText("1.5"); edit.up.click(); assert edit.edit.text()=="1.5"
        edit.setValue(1); edit.setEnabled(False); QtTest.QTest.mouseClick(edit.up,C.Qt.MouseButton.LeftButton)
        assert edit.value()==1
    finally:
        edit.close()


def test_invalid_input_stays_red_after_refresh_and_never_schedules(tmp_path):
    app=W.QApplication.instance() or W.QApplication([])
    runtime=Runtime(tmp_path,autostart=False); runtime.publish()
    w=Window(runtime); w.timer.stop()
    try:
        w.delay_hours.edit.setText("1.5")
        w.delay_minutes.edit.setText("word")
        w.refresh(); w.schedule_control()
        assert w.delay_hours.edit.text()=="1.5" and w.delay_minutes.edit.text()=="word"
        assert w.delay_hours.edit.property("invalid") and w.delay_minutes.edit.property("invalid")
        assert runtime.commands.empty()
        w.delay_hours.setValue(0); w.delay_minutes.setValue(0)
        assert not w.validate_countdown_input()
        w.delay_minutes.up.click()
        assert w.validate_countdown_input() and w.delay_minutes.value()==1
        w.delay_hours.edit.setText("-1"); w.switch_language(1)
        assert "digits 0–9" in w.countdown_error.text()
    finally:
        w.close(); app.processEvents()
        for h in list(runtime.log.handlers): h.close(); runtime.log.removeHandler(h)
