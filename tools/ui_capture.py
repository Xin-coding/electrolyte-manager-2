import os
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtWidgets as W, QtCore as C, QtGui as G
from em2.runtime import Runtime
from em2.ui import Window

app = W.QApplication([])
root = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory() as folder:
    runtime = Runtime(Path(folder))
    w = Window(runtime)
    w.show()
    for i in range(5):
        w.navigation.setCurrentRow(i)
        for _ in range(5):
            app.processEvents()
            time.sleep(.05)
        w.refresh()
        app.processEvents()
        w.grab().save(str(root/f"exports/ui-page-{i+1}.png"))
    runtime.close()
    runtime.thread.join(10)
    w.close()
    app.processEvents()
print("Captured five pages")
