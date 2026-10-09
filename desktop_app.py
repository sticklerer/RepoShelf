#!/usr/bin/env python3
"""Desktop window and system-tray launcher for RepoShelf."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QLockFile, QProcess, QTimer, QUrl
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

import server


ROOT = Path(__file__).resolve().parent
ICON_PATH = ROOT / "assets" / "reposhelf.svg"
UNINSTALL_PATH = ROOT / "uninstall-linux.sh"
APP_URL = "http://127.0.0.1:8765/"


class RepoShelfWindow(QMainWindow):
    def __init__(self, lock: QLockFile) -> None:
        super().__init__()
        self._lock = lock
        self._server = None
        self._tray = None
        self._has_hidden = False
        self.setWindowTitle("RepoShelf")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1280, 840)
        self.setMinimumSize(760, 520)

        self.web_view = QWebEngineView(self)
        self.web_view.setUrl(QUrl(APP_URL))
        self.setCentralWidget(self.web_view)

        self._start_server()
        self._create_tray()
        self._uninstall_timer = QTimer(self)
        self._uninstall_timer.timeout.connect(self._process_uninstall_request)
        self._uninstall_timer.start(250)

    def _start_server(self) -> None:
        server.load_state()
        server.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self._server = server.ThreadingHTTPServer(("127.0.0.1", 8765), server.Handler)
        thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="reposhelf-web-server",
        )
        thread.start()
        threading.Thread(
            target=server.automation_loop,
            daemon=True,
            name="reposhelf-automation",
        ).start()

    def _create_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            raise RuntimeError(
                "The system tray is unavailable. Enable the system tray in your desktop panel and try again."
            )

        self._tray = QSystemTrayIcon(QIcon(str(ICON_PATH)), self)
        self._tray.setToolTip("RepoShelf is running")

        menu = QMenu()
        show_action = QAction("Open RepoShelf", self)
        show_action.triggered.connect(self.show_window)
        menu.addAction(show_action)
        menu.addSeparator()
        quit_action = QAction("Quit RepoShelf", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window()

    def show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
        if not self._has_hidden and self._tray is not None:
            self._tray.showMessage(
                "RepoShelf is still running",
                "The package library is available from the system tray.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
            self._has_hidden = True

    def _process_uninstall_request(self) -> None:
        delete_data = server.consume_uninstall_request()
        if delete_data is None:
            return
        if not UNINSTALL_PATH.is_file():
            QMessageBox.critical(
                self,
                "Uninstaller not found",
                "The RepoShelf uninstaller is missing. Reinstall the app to restore it.",
            )
            return
        arguments = ["--purge-data"] if delete_data else []
        started, _process_id = QProcess.startDetached(str(UNINSTALL_PATH), arguments)
        if not started:
            QMessageBox.critical(
                self,
                "Could not start the uninstaller",
                "RepoShelf is still installed. You can run uninstall-linux.sh from a terminal.",
            )
            return

        QTimer.singleShot(250, self.quit_app)

    def quit_app(self) -> None:
        if self._tray is not None:
            self._tray.hide()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self._lock.unlock()
        QApplication.quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("RepoShelf")
    app.setApplicationDisplayName("RepoShelf")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    app.setQuitOnLastWindowClosed(False)

    server.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = server.DATA_DIR / "reposhelf.lock"
    lock = QLockFile(str(lock_path))
    if not lock.tryLock(0):
        QMessageBox.information(
            None,
            "RepoShelf is already open",
            "RepoShelf is already running. Open it from the system tray.",
        )
        return 0

    try:
        window = RepoShelfWindow(lock)
    except Exception as error:
        lock.unlock()
        QMessageBox.critical(None, "Could not start RepoShelf", str(error))
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
