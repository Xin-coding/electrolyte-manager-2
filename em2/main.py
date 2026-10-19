from __future__ import annotations
import os
import sys

from PySide6 import QtCore, QtWidgets
from .paths import root_dir
from .runtime import Runtime
from .ui import Window


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Electrolyte Manager 2")
    app.setOrganizationName("Xin-coding")
    root = root_dir()
    (root/"data").mkdir(parents=True, exist_ok=True)
    lock = QtCore.QLockFile(str(root/"data/application.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QtWidgets.QMessageBox.warning(None, "Electrolyte Manager", "Another instance is using this workspace.")
        return 1
    try:
        runtime = Runtime(root)
        window = Window(runtime)
        window.show()
        return app.exec()
    except Exception as exc:
        QtWidgets.QMessageBox.critical(None, "Electrolyte Manager", str(exc))
        return 1
    finally:
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
