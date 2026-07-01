#!/usr/bin/env python3

"""Minimal PySide6 frontend for the PiDP-8 ASR-33 emulator."""

import errno
import math
import os
import re
import sys
from pathlib import Path

import paramiko
from PySide6.QtCore import QRectF, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QFileDialog,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


KEYBOARD_UPPERCASE_ONLY = False
KEYBOARD_PARITY_MODE = "space"
DEFAULT_FONT_PATH = "Teletype33.ttf"
DEFAULT_FONT_SIZE = 20
PAPER_TAPE_EXTENSIONS = (
    ".pt", ".pb", ".pa", ".pr", ".bpt", ".apt", ".rpt", ".tap", ".rim", ".bin", ".bn"
)


class QtPaperTapeReader:
    """Qt-side paper tape reader backend using the existing SSH/throttle path."""

    def __init__(self, backend, config, ssh_config=None):
        self.backend = backend
        self.config = config
        self.tape_loaded = False
        self.active = False
        self.state = "FREE"
        self.tape_name = ""
        self.tape_data = b""
        self.position = 0
        self.stop_cause = ""
        self.trailing_o000_idx = None
        self.trailing_o200_idx = None
        self.skip_leading_nulls = config.get("skip_leading_nulls", default=True)
        self.auto_stop = config.get("auto_stop", default=True)
        self.set_msb = config.get("set_msb", default=False)

        def remote_cfg(key, fallback_key=None, default=None):
            value = config.get(key, default=None)
            if value is not None:
                return value
            if ssh_config is not None:
                return ssh_config.get(fallback_key or key, default=default)
            return default

        self.remote_enabled = config.get("remote_enabled", default=False)
        self.remote_host = remote_cfg("remote_host", "host")
        self.remote_username = remote_cfg("remote_username", "username")
        self.remote_password = remote_cfg("remote_password", "password")
        self.remote_password_file = remote_cfg("remote_password_file", "password_file")
        self.remote_port = remote_cfg("remote_port", "port", 22)
        self.remote_dir = config.get("remote_dir", default=None)
        self.remote_key_filename = remote_cfg("remote_key_filename", "key_filename")
        self.remote_use_agent = remote_cfg("remote_use_agent", "use_agent", True)
        self.remote_look_for_keys = remote_cfg("remote_look_for_keys", "look_for_keys", True)

    @staticmethod
    def _read_password_file(path: str | None) -> str | None:
        if not path:
            return None
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
                password = f.read().strip()
        except OSError:
            return None
        return password or None

    def load_tape(self, parent) -> bool:
        """Load a local or remote paper tape."""
        if self.active:
            self.stop()
        if self.remote_enabled:
            return self._load_remote_tape(parent)
        path, _ = QFileDialog.getOpenFileName(
            parent,
            "Load Paper Tape",
            self.config.get("initial_file_path", default="."),
            "Paper tape files (*.pt *.pb *.pa *.pr *.bpt *.apt *.rpt *.tap *.rim *.bin *.bn);;All files (*)",
        )
        if not path:
            return False
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            print(f"Could not load tape file: {e}")
            return False
        self._set_tape(os.path.basename(path), data)
        return True

    def start(self) -> bool:
        if not self.tape_loaded:
            self.active = False
            self.state = "STOP"
            return False
        if self.skip_leading_nulls:
            while self.position < len(self.tape_data) and self.tape_data[self.position] == 0o000:
                self.position += 1
        self.stop_cause = ""
        self.active = True
        self.state = "START"
        return True

    def stop(self) -> None:
        self.active = False
        self.state = "STOP"

    def free(self) -> None:
        self.active = False
        self.state = "FREE"

    def unload(self) -> None:
        self.tape_loaded = False
        self.active = False
        self.state = "FREE"
        self.tape_name = ""
        self.tape_data = b""
        self.position = 0
        self.stop_cause = ""
        self.trailing_o000_idx = None
        self.trailing_o200_idx = None

    def set_position(self, position: int) -> None:
        if not self.tape_data:
            self.position = 0
            return
        self.position = max(0, min(len(self.tape_data) - 1, int(position)))
        self.stop_cause = ""

    def process(self) -> None:
        if not self.active:
            return
        if self._should_stop(self.position):
            self.active = False
            self.state = "STOP"
            return
        data_byte = bytes(self.tape_data[self.position:self.position + 1])
        if not data_byte:
            self.active = False
            self.state = "STOP"
            return
        if self.set_msb:
            data_byte = bytes([data_byte[0] | 0x80])
        if self.backend is not None:
            self.backend.send_data(data_byte)
        self.position += 1

    def active_status(self) -> bool:
        return self.active

    def progress(self) -> float:
        if not self.tape_data:
            return 0.0
        return min(1.0, self.position / len(self.tape_data))

    def _set_tape(self, name: str, data: bytes) -> None:
        self.tape_name = name
        self.tape_data = data
        self.position = 0
        self.tape_loaded = True
        self.active = False
        self.state = "STOP"
        self.stop_cause = ""
        self._locate_trailers()

    def _locate_trailers(self) -> None:
        n = len(self.tape_data)
        i = n - 1
        while i >= 0 and self.tape_data[i] == 0o000:
            i -= 1
        self.trailing_o000_idx = i + 1 if i < n - 1 else None
        j = i
        while j >= 0 and self.tape_data[j] == 0o200:
            j -= 1
        self.trailing_o200_idx = j + 1 if j < i else None

    def _should_stop(self, position: int) -> bool:
        if position >= len(self.tape_data):
            self.stop_cause = "end_of_tape"
            return True
        if not self.auto_stop:
            return False
        if self.trailing_o200_idx is not None and position > self.trailing_o200_idx:
            self.stop_cause = "trailing_o200"
            return True
        if self.trailing_o000_idx is not None and position > self.trailing_o000_idx:
            self.stop_cause = "trailing_o000"
            return True
        return False

    def _load_remote_tape(self, parent) -> bool:
        ssh = None
        sftp = None
        try:
            if not self.remote_host or not self.remote_username or not self.remote_dir:
                print("Remote paper tape is enabled but host, username, or dir is not configured")
                return False
            cached_password = None
            if self.backend is not None and hasattr(self.backend, "get_cached_ssh_password"):
                cached_password = self.backend.get_cached_ssh_password()
            file_password = self._read_password_file(self.remote_password_file)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                hostname=self.remote_host,
                username=self.remote_username,
                port=self.remote_port,
                password=self.remote_password or cached_password or file_password,
                key_filename=self.remote_key_filename,
                timeout=8,
                look_for_keys=self.remote_look_for_keys,
                allow_agent=self.remote_use_agent,
            )
            cmd = f"mountpoint -q {self.remote_dir}; echo $?"
            _, stdout, _ = ssh.exec_command(cmd)
            mounted = stdout.read().decode("ascii", errors="ignore").strip()
            if mounted != "0":
                print(f"Remote paper tape directory is not mounted: {self.remote_dir}")
                return False
            sftp = ssh.open_sftp()
            tape_names = [
                n for n in sftp.listdir(self.remote_dir)
                if not n.startswith("._") and n.lower().endswith(PAPER_TAPE_EXTENSIONS)
            ]
            tape_names.sort()
            if not tape_names:
                print(f"No paper tape file found in {self.remote_dir}")
                return False
            while tape_names:
                chooser = PaperTapeChooser(parent, tape_names)
                if chooser.exec() != QDialog.Accepted or not chooser.selected_tape:
                    return False
                selected = chooser.selected_tape
                remote_path = self.remote_dir.rstrip("/") + "/" + selected
                if chooser.delete_requested:
                    answer = QMessageBox.question(
                        parent,
                        "Delete Paper Tape",
                        f"Delete {selected} from {self.remote_dir}?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                    if answer == QMessageBox.Yes:
                        try:
                            sftp.remove(remote_path)
                            tape_names.remove(selected)
                            print(f"Remote paper tape deleted: {remote_path}")
                        except Exception as e:
                            QMessageBox.warning(
                                parent,
                                "Delete Paper Tape",
                                f"Could not delete {selected}:\n{e}",
                            )
                            print(f"Could not delete remote paper tape: {e}")
                    continue
                with sftp.open(remote_path, "rb") as f:
                    data = f.read()
                self._set_tape(selected, data)
                print(f"Remote paper tape loaded: {remote_path} ({len(data)} bytes)")
                return True
            print(f"No paper tape file found in {self.remote_dir}")
            return False
        except Exception as e:
            print(f"Could not load remote paper tape: {e}")
            return False
        finally:
            try:
                if sftp is not None:
                    sftp.close()
            finally:
                if ssh is not None:
                    ssh.close()


class QtPaperTapePunch:
    """Qt-side paper tape punch for SIMH PTP / OS/8 .PUNCH output."""

    def __init__(self, backend, config, ssh_config=None):
        self.backend = backend
        self.config = config
        self.mode = "PTP"
        self.active = False
        self.ptp_attached = False
        self.output_name = ""
        self.output_path = ""
        self.byte_count = 0
        self.punched_bytes: list[int] = []
        self.pending_punch_bytes: list[int] = []
        self.visual_phase = 0.0
        self._visual_feed_accumulator = 0.0
        self.visual_activity_ticks = 0
        self._ptp_poll_ssh = None
        self._ptp_poll_sftp = None
        self._ptp_observed_size = 0
        self._ptp_poll_ticks = 0

        def remote_cfg(key, fallback_key=None, default=None):
            value = config.get(key, default=None)
            if value is not None:
                return value
            if ssh_config is not None:
                return ssh_config.get(fallback_key or key, default=default)
            return default

        self.remote_enabled = config.get("remote_enabled", default=False)
        self.remote_host = remote_cfg("remote_host", "host")
        self.remote_username = remote_cfg("remote_username", "username")
        self.remote_password = remote_cfg("remote_password", "password")
        self.remote_password_file = remote_cfg("remote_password_file", "password_file")
        self.remote_port = remote_cfg("remote_port", "port", 22)
        self.remote_dir = config.get("remote_dir", default=None)
        self.remote_key_filename = remote_cfg("remote_key_filename", "key_filename")
        self.remote_use_agent = remote_cfg("remote_use_agent", "use_agent", True)
        self.remote_look_for_keys = remote_cfg("remote_look_for_keys", "look_for_keys", True)

    @staticmethod
    def _read_password_file(path: str | None) -> str | None:
        if not path:
            return None
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
                password = f.read().strip()
        except OSError:
            return None
        return password or None

    def select_output(self, parent) -> bool:
        """Attach SIMH PTP to a punch output file."""
        return self.attach_ptp(parent)

    def start(self) -> bool:
        return self.attach_ptp(None)

    def stop(self) -> None:
        self.detach_ptp()

    def close_output(self) -> None:
        self.stop()
        self.output_name = ""
        self.output_path = ""

    def has_output(self) -> bool:
        return self.ptp_attached

    def poll_ptp_activity(self) -> int:
        """Observe a SIMH PTP output file and animate newly written bytes."""
        if self.mode != "PTP" or not self.ptp_attached or not self.output_path:
            return 0
        self._ptp_poll_ticks += 1
        if self._ptp_poll_ticks < 5:
            return 0
        self._ptp_poll_ticks = 0
        try:
            sftp = self._ensure_ptp_poll_sftp()
            if sftp is None:
                return 0
            size = sftp.stat(self.output_path).st_size
            if size <= self._ptp_observed_size:
                self._ptp_observed_size = size
                return 0
            old_size = self._ptp_observed_size
            delta = size - old_size
            with sftp.open(self.output_path, "rb") as remote_file:
                remote_file.seek(max(0, size - min(delta, 512)))
                data = remote_file.read(min(delta, 512))
            self._ptp_observed_size = size
            self._record_punch_activity(data, delta)
            return delta
        except FileNotFoundError:
            return 0
        except OSError as e:
            if getattr(e, "errno", None) == errno.ENOENT or "No such file" in str(e):
                return 0
            print(f"Could not observe PTP output: {e}")
            self._close_ptp_poll()
            return 0
        except Exception as e:
            print(f"Could not observe PTP output: {e}")
            self._close_ptp_poll()
            return 0

    def _record_punch_activity(self, data: bytes, count: int) -> None:
        self.byte_count += count
        if data:
            self.pending_punch_bytes.extend(data)
        elif count > 0:
            self.pending_punch_bytes.extend([0] * count)
        self.visual_activity_ticks = max(self.visual_activity_ticks, min(600, 80 + count * 3))
        if len(self.pending_punch_bytes) > 8192:
            self.pending_punch_bytes = self.pending_punch_bytes[-8192:]

    def advance_visual_feed(self) -> None:
        if self.visual_activity_ticks <= 0 and not self.pending_punch_bytes:
            return
        feed_speed = 0.28
        if len(self.pending_punch_bytes) > 400:
            feed_speed = 0.72
        elif len(self.pending_punch_bytes) > 120:
            feed_speed = 0.45

        self.visual_phase += feed_speed
        self._visual_feed_accumulator += feed_speed
        while self._visual_feed_accumulator >= 1.0 and self.pending_punch_bytes:
            self.punched_bytes.append(self.pending_punch_bytes.pop(0))
            self._visual_feed_accumulator -= 1.0
        if self.visual_activity_ticks > 0:
            self.visual_activity_ticks -= 1
        if len(self.punched_bytes) > 4096:
            self.punched_bytes = self.punched_bytes[-4096:]

    def visual_active(self) -> bool:
        return self.visual_activity_ticks > 0 or bool(self.pending_punch_bytes)

    def attach_ptp(self, parent) -> bool:
        """Attach SIMH PTP to a remote file for clean OS/8 .PUNCH output."""
        if self.backend is None or not hasattr(self.backend, "send_data"):
            return False
        initial = self.output_name or "punch.pt"
        name, ok = QInputDialog.getText(
            parent,
            "New Paper Tape",
            "New paper tape filename:",
            text=initial,
        )
        if not ok or not name:
            return False
        name = os.path.basename(name.strip())
        if not name:
            return False
        if "." not in name:
            name += ".pt"

        self.close_output()
        self.mode = "PTP"
        remote_dir = self.remote_dir or "."
        remote_path = remote_dir.rstrip("/") + "/" + name
        self.backend.send_data(b"\x05")
        QTimer.singleShot(500, lambda: self.backend.send_data(b"detach ptp\r"))
        quoted_remote_path = self._simh_quoted_path(remote_path).encode("ascii", "ignore")
        QTimer.singleShot(
            1000,
            lambda: self.backend.send_data(
                b"attach -n ptp " + quoted_remote_path + b"\r"
            ),
        )
        QTimer.singleShot(1500, lambda: self.backend.send_data(b"cont\r"))
        self.output_name = name
        self.output_path = remote_path
        self.ptp_attached = True
        self.active = False
        self.byte_count = 0
        self.punched_bytes = []
        self.pending_punch_bytes = []
        self.visual_phase = 0.0
        self._visual_feed_accumulator = 0.0
        self.visual_activity_ticks = 0
        self._ptp_observed_size = 0
        self._ptp_poll_ticks = 0
        self._close_ptp_poll()
        return True

    @staticmethod
    def _simh_quoted_path(path: str) -> str:
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def detach_ptp(self) -> None:
        """Detach SIMH PTP and continue the PDP-8."""
        if not self.ptp_attached:
            return
        if self.backend is not None and hasattr(self.backend, "send_data"):
            self.backend.send_data(b"\x05")
            QTimer.singleShot(500, lambda: self.backend.send_data(b"detach ptp\r"))
            QTimer.singleShot(1000, lambda: self.backend.send_data(b"cont\r"))
        self.ptp_attached = False
        self.active = False
        self.output_name = ""
        self.output_path = ""
        self._ptp_observed_size = 0
        self._ptp_poll_ticks = 0
        self.visual_activity_ticks = 0
        self.pending_punch_bytes = []
        self._visual_feed_accumulator = 0.0
        self._close_ptp_poll()

    def _ensure_ptp_poll_sftp(self):
        if self._ptp_poll_sftp is not None:
            return self._ptp_poll_sftp
        if not self.remote_host or not self.remote_username:
            return None
        cached_password = None
        if self.backend is not None and hasattr(self.backend, "get_cached_ssh_password"):
            cached_password = self.backend.get_cached_ssh_password()
        file_password = self._read_password_file(self.remote_password_file)
        self._ptp_poll_ssh = paramiko.SSHClient()
        self._ptp_poll_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ptp_poll_ssh.connect(
            hostname=self.remote_host,
            username=self.remote_username,
            port=self.remote_port,
            password=self.remote_password or cached_password or file_password,
            key_filename=self.remote_key_filename,
            timeout=4,
            look_for_keys=self.remote_look_for_keys,
            allow_agent=self.remote_use_agent,
        )
        self._ptp_poll_sftp = self._ptp_poll_ssh.open_sftp()
        return self._ptp_poll_sftp

    def _close_ptp_poll(self) -> None:
        try:
            if self._ptp_poll_sftp is not None:
                self._ptp_poll_sftp.close()
            if self._ptp_poll_ssh is not None:
                self._ptp_poll_ssh.close()
        finally:
            self._ptp_poll_sftp = None
            self._ptp_poll_ssh = None

def load_terminal_font(config) -> QFont:
    """Load the configured ASR-33 font for the Qt text surface."""
    font_path = config.terminal.config.get("font_path", default=None) or DEFAULT_FONT_PATH
    font_size = config.terminal.config.get("font_size", default=DEFAULT_FONT_SIZE)
    family = None
    font_path = str(Path(font_path).expanduser().resolve())

    font_id = QFontDatabase.addApplicationFont(font_path)
    if font_id >= 0:
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            family = families[0]
    else:
        print(f"Warning: font not found: {font_path}")

    return QFont(family or "Menlo", font_size)


class PaperTapeChooser(QDialog):
    """Graphical paper tape chooser for remote reader tapes."""

    def __init__(self, parent, tapes: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Load Paper Tape")
        self.selected_tape = None
        self.delete_requested = False

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.IconMode)
        self.list_widget.setResizeMode(QListWidget.Adjust)
        self.list_widget.setMovement(QListWidget.Static)
        self.list_widget.setIconSize(QSize(118, 78))
        self.list_widget.setGridSize(QSize(172, 124))
        self.list_widget.itemDoubleClicked.connect(self._load_item)

        icon = self._paper_roll_icon()
        for tape in tapes:
            item = QListWidgetItem(icon, os.path.basename(tape))
            item.setData(Qt.UserRole, tape)
            self.list_widget.addItem(item)

        self.load_button = QPushButton("LOAD")
        self.delete_button = QPushButton("DELETE")
        self.cancel_button = QPushButton("CANCEL")
        self.load_button.clicked.connect(self._load_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.cancel_button.clicked.connect(self.reject)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.load_button)
        button_row.addWidget(self.delete_button)
        button_row.addWidget(self.cancel_button)

        layout = QVBoxLayout()
        layout.addWidget(self.list_widget)
        layout.addLayout(button_row)
        self.setLayout(layout)
        self.resize(620, 420)

    def _current_tape(self) -> str | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _load_item(self, item: QListWidgetItem) -> None:
        self.selected_tape = item.data(Qt.UserRole)
        self.delete_requested = False
        self.accept()

    def _load_selected(self) -> None:
        tape = self._current_tape()
        if not tape:
            return
        self.selected_tape = tape
        self.delete_requested = False
        self.accept()

    def _delete_selected(self) -> None:
        tape = self._current_tape()
        if not tape:
            return
        self.selected_tape = tape
        self.delete_requested = True
        self.accept()

    @staticmethod
    def _paper_roll_icon() -> QIcon:
        pixmap = QPixmap(132, 88)
        pixmap.fill(QColor("#f2ead5"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 26))
        painter.drawRoundedRect(QRectF(18, 22, 94, 44), 8, 8)

        painter.setPen(QPen(QColor("#8d8068"), 1))
        painter.setBrush(QColor("#d8c79f"))
        painter.drawEllipse(QRectF(18, 22, 42, 42))
        painter.setBrush(QColor("#f4e4ba"))
        painter.drawEllipse(QRectF(27, 31, 24, 24))
        painter.setBrush(QColor("#8c7a5d"))
        painter.drawEllipse(QRectF(35, 39, 8, 8))

        strip = QPainterPath()
        strip.moveTo(48, 27)
        strip.cubicTo(64, 19, 88, 19, 108, 27)
        strip.lineTo(108, 59)
        strip.cubicTo(88, 67, 64, 67, 48, 59)
        strip.closeSubpath()
        painter.setPen(QPen(QColor("#aa9b7a"), 1))
        painter.setBrush(QColor("#ead6aa"))
        painter.drawPath(strip)

        painter.setPen(QPen(QColor("#bca77c"), 1))
        for x in range(56, 104, 10):
            painter.drawLine(x, 30, x - 2, 58)

        painter.setBrush(QColor("#2b2b28"))
        painter.setPen(Qt.NoPen)
        for x in range(54, 108, 9):
            painter.drawEllipse(QRectF(x, 40, 4, 4))
        for x in range(58, 104, 12):
            painter.drawEllipse(QRectF(x, 32, 4, 4))
            painter.drawEllipse(QRectF(x + 3, 51, 4, 4))

        painter.end()
        return QIcon(pixmap)


class TU56TapeChooser(QDialog):
    """Graphical reel chooser for remote TU56 images."""

    def __init__(self, parent, unit_name: str, images: list[str]):
        super().__init__(parent)
        self.setWindowTitle(f"{unit_name} DECtape")
        self.selected_image = None
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.IconMode)
        self.list_widget.setResizeMode(QListWidget.Adjust)
        self.list_widget.setMovement(QListWidget.Static)
        self.list_widget.setIconSize(QSize(96, 72))
        self.list_widget.setGridSize(QSize(150, 118))
        self.list_widget.itemClicked.connect(self._accept_item)
        self.list_widget.itemDoubleClicked.connect(self._accept_item)

        icon = self._reel_icon()
        for image in images:
            item = QListWidgetItem(icon, os.path.basename(image))
            item.setData(Qt.UserRole, image)
            self.list_widget.addItem(item)

        layout = QVBoxLayout()
        layout.addWidget(self.list_widget)
        self.setLayout(layout)
        self.resize(520, 360)

    def _accept_item(self, item: QListWidgetItem) -> None:
        self.selected_image = item.data(Qt.UserRole)
        self.accept()

    @staticmethod
    def _reel_icon() -> QIcon:
        path = Path("images/bobine.jpg")
        if path.exists():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                return QIcon(pixmap)

        pixmap = QPixmap(120, 90)
        pixmap.fill(QColor("#d8cfb8"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#4b453c"), 2))
        painter.setBrush(QColor("#2f2c27"))
        painter.drawEllipse(QRectF(18, 18, 54, 54))
        painter.setBrush(QColor("#c9bea2"))
        painter.drawEllipse(QRectF(39, 39, 12, 12))
        painter.end()
        return QIcon(pixmap)


class RK05PackChooser(QDialog):
    """Graphical removable cartridge chooser for RK05 images."""

    def __init__(self, parent, unit_name: str, images: list[str]):
        super().__init__(parent)
        self.setWindowTitle(f"{unit_name} RK05 DECpack")
        self.selected_image = None
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.IconMode)
        self.list_widget.setResizeMode(QListWidget.Adjust)
        self.list_widget.setMovement(QListWidget.Static)
        self.list_widget.setIconSize(QSize(112, 84))
        self.list_widget.setGridSize(QSize(170, 122))
        self.list_widget.itemClicked.connect(self._accept_item)
        self.list_widget.itemDoubleClicked.connect(self._accept_item)

        icon = self._pack_icon()
        for image in images:
            item = QListWidgetItem(icon, os.path.basename(image))
            item.setData(Qt.UserRole, image)
            self.list_widget.addItem(item)

        layout = QVBoxLayout()
        layout.addWidget(self.list_widget)
        self.setLayout(layout)
        self.resize(560, 380)

    def _accept_item(self, item: QListWidgetItem) -> None:
        self.selected_image = item.data(Qt.UserRole)
        self.accept()

    @staticmethod
    def _pack_icon() -> QIcon:
        path = Path("images/disqueRK05.jpg")
        if path.exists():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                return QIcon(pixmap)

        pixmap = QPixmap(128, 96)
        pixmap.fill(QColor("#d8cfb8"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#4b453c"), 2))
        painter.setBrush(QColor("#cfd4d2"))
        painter.drawEllipse(QRectF(16, 10, 92, 76))
        painter.setBrush(QColor("#9b1f2d"))
        painter.drawPie(QRectF(22, 16, 80, 64), 205 * 16, 95 * 16)
        painter.end()
        return QIcon(pixmap)


class SimhRKController:
    """Small SIMH RK command helper using the existing interactive SSH channel."""

    def __init__(self, frontend):
        self.frontend = frontend

    def simh_command(self, command: str, return_delay_ms: int = 500) -> None:
        backend = self.frontend._backend
        if backend is None or not hasattr(backend, "send_data"):
            return
        backend.send_data(b"\x05")
        QTimer.singleShot(180, lambda: backend.send_data(command.encode("ascii", errors="ignore") + b"\r"))
        QTimer.singleShot(return_delay_ms, lambda: backend.send_data(b"cont\r"))

    def attach(self, unit_number: int, path: str, readonly: bool = False) -> None:
        backend = self.frontend._backend
        if backend is None or not hasattr(backend, "send_data"):
            return
        unit = f"rk{unit_number}"
        quoted = self.frontend._simh_quoted_path(path)
        attach = f'attach {"-r " if readonly else ""}{unit} {quoted}'
        backend.send_data(b"\x05")
        QTimer.singleShot(220, lambda: backend.send_data(f"detach {unit}\r".encode("ascii")))
        QTimer.singleShot(620, lambda: backend.send_data((attach + "\r").encode("ascii", errors="ignore")))
        QTimer.singleShot(1120, lambda: backend.send_data(b"cont\r"))

    def detach(self, unit_number: int) -> None:
        self.simh_command(f"detach rk{unit_number}", return_delay_ms=650)

    def show(self) -> None:
        self.frontend._begin_rk05_show_capture()
        self.simh_command("show rk", return_delay_ms=800)
        QTimer.singleShot(950, self.frontend._finish_rk05_show_capture)

    @staticmethod
    def parse_show_rk(text: str) -> dict[int, dict]:
        parsed: dict[int, dict] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            upper = line.upper()
            if not upper.startswith("RK"):
                continue
            parts = upper.split(None, 1)
            if not parts or len(parts[0]) < 3 or not parts[0][2:].isdigit():
                continue
            unit_number = int(parts[0][2:])
            if unit_number not in (0, 1, 2, 3):
                continue
            attached = "ATTACHED TO" in upper
            not_attached = "NOT ATTACHED" in upper
            readonly = "READ ONLY" in upper or "WRITE LOCK" in upper or "WRITE PROTECT" in upper
            write_enabled = "WRITE ENABLED" in upper
            filename = ""
            if attached:
                marker = "attached to "
                lower = line.lower()
                start = lower.find(marker)
                if start >= 0:
                    filename = line[start + len(marker):].split(",", 1)[0].strip()
            parsed[unit_number] = {
                "attached": attached and not not_attached,
                "file": filename,
                "readonly": readonly and not write_enabled,
                "fault": False,
            }
        return parsed


class SimhDTController:
    """Small SIMH DECtape helper using the existing interactive SSH channel."""

    def __init__(self, frontend):
        self.frontend = frontend

    def simh_command(self, command: str, return_delay_ms: int = 500) -> None:
        backend = self.frontend._backend
        if backend is None or not hasattr(backend, "send_data"):
            return
        backend.send_data(b"\x05")
        QTimer.singleShot(180, lambda: backend.send_data(command.encode("ascii", errors="ignore") + b"\r"))
        QTimer.singleShot(return_delay_ms, lambda: backend.send_data(b"cont\r"))

    def show(self) -> None:
        self.frontend._begin_tu56_show_capture()
        self.simh_command("show dt", return_delay_ms=800)
        QTimer.singleShot(950, self.frontend._finish_tu56_show_capture)

    @staticmethod
    def parse_show_dt(text: str) -> dict[int, dict]:
        parsed: dict[int, dict] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = re.search(r"\bDT\s*([0-7])\b|\bDT([0-7])\s*:", line, re.IGNORECASE)
            if not match:
                continue
            unit_number = int(match.group(1) or match.group(2))
            lower = line.lower()
            active_boot = "12b format" in lower or "buffering file in memory" in lower
            not_attached = "not attached" in lower
            attached = ("attached" in lower and not not_attached) or active_boot
            filename = ""
            marker = "attached to "
            start = lower.find(marker)
            if start >= 0:
                filename = line[start + len(marker):].split(",", 1)[0].strip()
            if active_boot and not filename:
                filename = "tc08 system"
            parsed[unit_number] = {
                "attached": attached,
                "file": filename,
                "active": active_boot,
                "status": "tc08 system" if active_boot else "refreshed",
            }
        return parsed


class RK05DetailDialog(QDialog):
    """Zoomed operator panel for one RK05 unit."""

    def __init__(self, frontend, unit_number: int):
        super().__init__(frontend)
        self.frontend = frontend
        self.unit_number = unit_number
        self.setWindowTitle(f"RK{unit_number} RK05 DECpack")
        self.panel = RK05DetailPanel(frontend, unit_number)
        self.status_label = QLabel("")
        self.run_button = QPushButton()
        self.wtprot_button = QPushButton()
        self.pack_button = QPushButton("Pack / Load")
        self.eject_button = QPushButton("Eject")
        self.refresh_button = QPushButton("Refresh")
        self.close_button = QPushButton("Close")

        self.run_button.clicked.connect(lambda: self.frontend.toggle_rk05_run(unit_number))
        self.wtprot_button.clicked.connect(lambda: self.frontend.toggle_rk05_write_protect(unit_number))
        self.pack_button.clicked.connect(lambda: self.frontend.select_rk05_pack(unit_number))
        self.eject_button.clicked.connect(lambda: self.frontend.eject_rk05_pack(unit_number))
        self.refresh_button.clicked.connect(self.frontend.refresh_rk05_state)
        self.close_button.clicked.connect(self.accept)

        controls = QHBoxLayout()
        controls.addWidget(self.run_button)
        controls.addWidget(self.wtprot_button)
        controls.addWidget(self.pack_button)
        controls.addWidget(self.eject_button)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.close_button)

        layout = QVBoxLayout()
        layout.addWidget(self.panel)
        layout.addWidget(self.status_label)
        layout.addLayout(controls)
        self.setLayout(layout)
        self.resize(920, 560)
        self.refresh()

    def refresh(self) -> None:
        unit = self.frontend.rk05_unit(self.unit_number)
        self.run_button.setText("RUN ON" if unit.get("run") else "RUN OFF")
        self.wtprot_button.setText("WT PROT ON" if unit.get("readonly") else "WT PROT OFF")
        self.pack_button.setEnabled(bool(unit.get("run")))
        status = unit.get("status") or ""
        file_path = unit.get("file") or "no pack"
        self.status_label.setText(f"RK{self.unit_number}: {file_path} {status}")
        self.panel.update()


def draw_handwritten_pack_label(painter: QPainter, rect: QRectF, text: str, size: int) -> None:
    label = text
    if len(label) > 22:
        stem, ext = os.path.splitext(label)
        label = stem[:16] + ".." + ext
    font = QFont("Marker Felt", size)
    font.setItalic(True)
    painter.save()
    painter.translate(rect.center())
    painter.rotate(-2.5)
    painter.translate(-rect.center())
    painter.setFont(font)
    painter.setPen(QPen(QColor("#a7434d"), 2))
    painter.drawText(rect.adjusted(12, 0, -12, 0), Qt.AlignCenter, label)
    painter.setPen(QPen(QColor(130, 60, 65, 120), 1))
    painter.drawText(rect.adjusted(14, 2, -10, -2), Qt.AlignCenter, label)
    painter.restore()


class RK05DetailPanel(QWidget):
    """Large painted RK05 faceplate inspired by the DECpack front panel."""

    def __init__(self, frontend, unit_number: int):
        super().__init__()
        self.frontend = frontend
        self.unit_number = unit_number
        self.setMinimumSize(860, 440)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        unit = self.frontend.rk05_unit(self.unit_number)
        self._draw_panel(painter, self.rect(), unit)

    def _draw_panel(self, painter: QPainter, raw_rect, unit: dict) -> None:
        rect = QRectF(raw_rect).adjusted(10, 10, -10, -10)
        attached = bool(unit.get("attached"))
        active = bool(unit.get("active")) and self.frontend.rk05_blink_on()
        load_active = bool(unit.get("load"))
        fault = bool(unit.get("fault"))
        readonly = bool(unit.get("readonly"))
        run = bool(unit.get("run"))
        ready = attached and run and not fault

        painter.fillRect(raw_rect, QColor("#0f100f"))
        painter.setPen(QPen(QColor("#d7d2c7"), 4))
        painter.setBrush(QColor("#191a18"))
        painter.drawRoundedRect(rect, 18, 18)

        window = QRectF(rect.left() + 28, rect.top() + 28, rect.width() - 56, rect.height() * 0.44)
        painter.setPen(QPen(QColor("#0b0c0b"), 4))
        painter.setBrush(QColor("#050606"))
        painter.drawRect(window)
        painter.setBrush(QColor(75, 94, 95, 95))
        painter.drawRect(window.adjusted(12, window.height() * 0.48, -12, -16))

        if attached:
            self._draw_pack_window(painter, window, os.path.basename(unit.get("file") or "mounted"))
        else:
            painter.setFont(QFont("Helvetica", 16))
            painter.setPen(QPen(QColor("#4d493f"), 1))
            painter.drawText(window, Qt.AlignCenter, "EMPTY")

        lower = QRectF(rect.left() + 28, window.bottom() + 22, rect.width() - 56, rect.bottom() - window.bottom() - 42)
        painter.setPen(QPen(QColor("#d7d2c7"), 2))
        painter.setBrush(QColor("#101110"))
        painter.drawRect(lower)

        lamps = [
            ("PWR", run, QColor("#38c65c")),
            ("RDY", ready, QColor("#f3f5e7")),
            ("ONCYL", ready and not active, QColor("#f3f5e7")),
            ("FAULT", fault, QColor("#b52825")),
            ("WTPROT", readonly, QColor("#f3f5e7")),
            ("LOAD", load_active, QColor("#f3f5e7")),
            ("WT", bool(unit.get("wt")) and self.frontend.rk05_blink_on(), QColor("#f3f5e7")),
            ("RD", active, QColor("#f3f5e7")),
        ]
        self._draw_decpack_label(painter, QRectF(lower.left() + 18, lower.top() + 22, 185, 76))
        controls = QRectF(lower.left() + lower.width() * 0.45, lower.top() + 28, lower.width() * 0.42, 112)
        self._draw_rk05_controls(painter, controls, run, readonly, lamps)

        unit_box = QRectF(lower.right() - 74, lower.top() + 42, 48, 64)
        painter.setPen(QPen(QColor("#20211f"), 2))
        painter.setBrush(QColor("#050606"))
        painter.drawRect(unit_box)
        unit_font = QFont("Helvetica", 34)
        unit_font.setBold(True)
        painter.setFont(unit_font)
        painter.setPen(QPen(QColor("#f2f2ec"), 1))
        painter.drawText(unit_box, Qt.AlignCenter, str(self.unit_number))

    def _draw_pack_window(self, painter: QPainter, rect: QRectF, filename: str) -> None:
        tape = QRectF(rect.left() + 90, rect.bottom() - 100, rect.width() - 180, 60)
        painter.setPen(QPen(QColor("#c7c0aa"), 1))
        painter.setBrush(QColor("#d8cfb8"))
        painter.drawRoundedRect(tape, 4, 4)
        draw_handwritten_pack_label(painter, tape, filename or "DECpack", 20)
        painter.setPen(QPen(QColor(230, 235, 232, 90), 3))
        painter.drawLine(int(rect.left() + 20), int(rect.bottom() - 120), int(rect.right() - 20), int(rect.bottom() - 122))

    def _draw_decpack_label(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QPen(QColor("#e8e8df"), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)
        painter.setFont(QFont("Helvetica", 28, QFont.Bold))
        painter.setPen(QPen(QColor("#f0f0ea"), 1))
        painter.drawText(QRectF(rect.left() + 8, rect.top() + 7, rect.width() - 16, 34), Qt.AlignLeft, "decpack")
        painter.setFont(QFont("Helvetica", 8, QFont.Bold))
        painter.drawText(QRectF(rect.left() + 18, rect.top() + 43, 50, 12), Qt.AlignLeft, "RK05")
        painter.drawText(QRectF(rect.right() - 68, rect.top() + 43, 58, 12), Qt.AlignRight, "2200 BPI")
        painter.setFont(QFont("Helvetica", 10))
        painter.drawText(QRectF(rect.left() + 8, rect.bottom() - 20, rect.width() - 16, 16), Qt.AlignCenter, "digital equipment corporation")

    def _draw_rk05_controls(
            self, painter: QPainter, rect: QRectF, run: bool, readonly: bool,
            lamps: list[tuple[str, bool, QColor]]) -> None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#101110"))
        painter.drawRect(rect.adjusted(-8, -8, 8, 8))

        switch_w = 38
        switch_h = 92
        switch_top = rect.top() + 18
        run_rect = QRectF(rect.left() + 2, switch_top, switch_w, switch_h)
        prot_rect = QRectF(run_rect.right() + 8, switch_top, switch_w, switch_h)
        self._draw_rk05_paddle_switch(painter, run_rect, "RUN", run, bottom_label="LOAD")
        self._draw_rk05_paddle_switch(painter, prot_rect, "WT PROT", readonly)

        lamp_left = prot_rect.right() + 48
        lamp_top = rect.top() + 20
        lamp_size = 36
        gap_x = 12
        gap_y = 40
        for idx, (label, lit, color) in enumerate(lamps):
            col = idx % 4
            row = idx // 4
            self._draw_square_lamp(
                painter,
                QRectF(lamp_left + col * (lamp_size + gap_x), lamp_top + row * (lamp_size + gap_y), lamp_size, lamp_size),
                label,
                color,
                lit,
            )

    def _draw_rk05_paddle_switch(
            self, painter: QPainter, rect: QRectF, label: str, on: bool,
            bottom_label: str | None = None) -> None:
        painter.setFont(QFont("Helvetica", 8, QFont.Bold))
        painter.setPen(QPen(QColor("#f0f0e8"), 1))
        painter.drawText(QRectF(rect.left() - 8, rect.top() - 18, rect.width() + 16, 13), Qt.AlignCenter, label)

        well = rect.adjusted(-4, -2, 4, 2)
        painter.setPen(QPen(QColor("#050605"), 2))
        painter.setBrush(QColor("#060706"))
        painter.drawRoundedRect(well, 3, 3)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#111611"))
        if on:
            painter.drawRect(QRectF(well.left() + 3, well.bottom() - 23, well.width() - 6, 20))
        else:
            painter.drawRect(QRectF(well.left() + 3, well.top() + 3, well.width() - 6, 20))

        body = rect.adjusted(1, 1 if on else 19, -1, -18 if on else -1)
        path = QPainterPath()
        path.moveTo(body.left() + 4, body.bottom() - 4)
        path.lineTo(body.left() + 9, body.top() + 8)
        path.lineTo(body.left() + body.width() * 0.43, body.top())
        path.lineTo(body.right() - 3, body.top() + 9)
        path.lineTo(body.right() - 9, body.bottom() - 6)
        path.lineTo(body.left() + body.width() * 0.52, body.bottom())
        path.closeSubpath()
        painter.setPen(QPen(QColor("#1b211d"), 1))
        painter.setBrush(QColor("#607064") if on else QColor("#334038"))
        painter.drawPath(path)

        top_face = QPainterPath()
        top_face.moveTo(body.left() + 9, body.top() + 8)
        top_face.lineTo(body.left() + body.width() * 0.43, body.top())
        top_face.lineTo(body.right() - 3, body.top() + 9)
        top_face.lineTo(body.right() - 12, body.top() + 21)
        top_face.lineTo(body.left() + 12, body.top() + 20)
        top_face.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(148, 162, 147, 145 if on else 85))
        painter.drawPath(top_face)

        painter.setPen(QPen(QColor("#9aa894") if on else QColor("#657467"), 2))
        painter.drawLine(int(body.left() + 10), int(body.top() + 10), int(body.right() - 8), int(body.top() + 5))
        painter.setPen(QPen(QColor("#222821"), 2))
        painter.drawLine(int(body.left() + 6), int(body.bottom() - 8), int(body.right() - 10), int(body.bottom() - 3))

        marker_y = well.top() + 6 if on else well.bottom() - 11
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#d5ddcf") if on else QColor("#343b34"))
        painter.drawEllipse(QRectF(well.right() - 8, marker_y, 4, 4))

        if bottom_label:
            painter.setFont(QFont("Helvetica", 8, QFont.Bold))
            painter.setPen(QPen(QColor("#f0f0e8"), 1))
            painter.drawText(
                QRectF(rect.left() - 8, rect.bottom() + 3, rect.width() + 16, 13),
                Qt.AlignCenter,
                bottom_label,
            )

    def _draw_square_lamp(self, painter: QPainter, rect: QRectF, label: str, color: QColor, lit: bool) -> None:
        painter.setFont(QFont("Helvetica", 8, QFont.Bold))
        painter.setPen(QPen(QColor("#f0f0e8"), 1))
        painter.drawText(QRectF(rect.left() - 8, rect.top() - 17, rect.width() + 16, 13), Qt.AlignCenter, label)
        if lit:
            glow = QColor(color)
            glow.setAlpha(90)
            painter.setPen(Qt.NoPen)
            painter.setBrush(glow)
            painter.drawRoundedRect(rect.adjusted(-5, -5, 5, 5), 5, 5)
        bezel = rect.adjusted(-2, -2, 2, 2)
        painter.setPen(QPen(QColor("#25251f"), 1))
        painter.setBrush(QColor("#d7d7ce"))
        painter.drawRect(bezel)
        lens = rect.adjusted(4, 4, -4, -4)
        painter.setPen(QPen(QColor("#b8b8ad"), 1))
        painter.setBrush(color if lit else QColor("#c7c7be"))
        painter.drawEllipse(lens)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 75 if lit else 42))
        painter.drawEllipse(lens.adjusted(4, 3, -11, -12))


class TU56PanelWidget(QWidget):
    """Painted DEC TU56 panel representing two DECtape transports."""

    def __init__(self, frontend, unit_numbers: tuple[int, int]):
        super().__init__()
        self.frontend = frontend
        self.unit_numbers = unit_numbers
        self._reel_buttons: dict[int, QRectF] = {}
        self._eject_buttons: dict[int, QRectF] = {}
        self._refresh_rect = QRectF()
        self.setMinimumSize(380, 180)
        self.setMaximumHeight(200)
        self.setFocusPolicy(Qt.StrongFocus)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._reel_buttons = {}
        self._eject_buttons = {}

        panel = QRectF(0, 0, self.width(), self.height()).adjusted(4, 4, -4, -4)
        painter.setPen(QPen(QColor("#1b1b19"), 2))
        painter.setBrush(QColor("#20211f"))
        painter.drawRoundedRect(panel, 8, 8)

        title = f"TU56 DECtape DT{self.unit_numbers[0]}-DT{self.unit_numbers[1]}"
        font = QFont("Helvetica", 8)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#d4c8a6"), 1))
        painter.drawText(panel.adjusted(12, 8, -12, 0), Qt.AlignLeft | Qt.AlignTop, title)
        self._refresh_rect = QRectF(panel.right() - 72, panel.top() + 8, 58, 18)
        self._draw_button(painter, self._refresh_rect, "Refresh", False)

        units = {unit["unit"]: unit for unit in self.frontend.tu56_units()}
        drive_top = panel.top() + 30
        drive_h = (panel.height() - 42) / 2
        for idx, number in enumerate(self.unit_numbers):
            rect = QRectF(panel.left() + 10, drive_top + idx * drive_h, panel.width() - 20, drive_h - 8)
            self._draw_drive(painter, rect, units[number])

    def mousePressEvent(self, event) -> None:
        pos = event.position()
        if self._refresh_rect.contains(pos):
            self.frontend.refresh_dt_state_from_simh()
            return
        for unit, rect in self._reel_buttons.items():
            if rect.contains(pos):
                self.frontend.select_tu56_tape(unit)
                return
        for unit, rect in self._eject_buttons.items():
            if rect.contains(pos):
                self.frontend.eject_tu56_tape(unit)
                return
        super().mousePressEvent(event)

    def _draw_drive(self, painter: QPainter, rect: QRectF, unit: dict) -> None:
        attached = bool(unit.get("attached"))
        active = bool(unit.get("active")) and self.frontend.tu56_blink_on()
        unit_number = int(unit["unit"])

        painter.setPen(QPen(QColor("#555044"), 1))
        painter.setBrush(QColor("#111211"))
        painter.drawRoundedRect(rect, 5, 5)

        control = QRectF(rect.left() + 8, rect.top() + 7, 96, rect.height() - 14)
        painter.setPen(QPen(QColor("#3d3932"), 1))
        painter.setBrush(QColor("#2b2c29"))
        painter.drawRoundedRect(control, 4, 4)

        button_h = min(18.0, max(15.0, (control.height() - 18.0) / 2.0))
        button_gap = 5.0
        button_top = control.center().y() - button_h - button_gap / 2.0
        reel_button = QRectF(control.left() + 10, button_top, control.width() - 20, button_h)
        eject_button = QRectF(control.left() + 10, reel_button.bottom() + button_gap, control.width() - 20, button_h)
        self._reel_buttons[unit_number] = QRectF(reel_button)
        self._eject_buttons[unit_number] = QRectF(eject_button)
        self._draw_button(painter, reel_button, "Bobine", attached)
        self._draw_button(painter, eject_button, "Eject", attached)

        reels = QRectF(control.right() + 12, rect.top() + 8, 118, rect.height() - 16)
        if attached:
            self._draw_reels(painter, reels, os.path.basename(unit.get("file") or ""))
        else:
            self._draw_empty_transport(painter, reels)

        name_font = QFont("Helvetica", 8)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(QPen(QColor("#d4c8a6"), 1))
        painter.drawText(QRectF(reels.right() + 8, rect.top() + 8, 36, 15), Qt.AlignLeft, unit["name"])

        self._draw_lamp(
            painter,
            QRectF(reels.right() + 12, rect.top() + 28, 9, 9),
            QColor("#31c15b") if attached else QColor("#473f38"),
            attached,
        )
        self._draw_lamp(
            painter,
            QRectF(reels.right() + 30, rect.top() + 28, 9, 9),
            QColor("#d83325") if active else QColor("#473f38"),
            active,
        )

    def _draw_reels(self, painter: QPainter, rect: QRectF, filename: str) -> None:
        painter.setPen(QPen(QColor("#676052"), 1))
        painter.setBrush(QColor("#d7ccb2"))
        left = QRectF(rect.left() + 6, rect.top() + 2, 44, 44)
        right = QRectF(rect.left() + 68, rect.top() + 2, 44, 44)
        painter.drawEllipse(left)
        painter.drawEllipse(right)
        painter.setPen(QPen(QColor("#2f2c27"), 3))
        painter.drawLine(int(left.center().x()), int(left.center().y()),
                         int(right.center().x()), int(right.center().y()))
        painter.setBrush(QColor("#2f2c27"))
        painter.drawEllipse(QRectF(left.center().x() - 8, left.center().y() - 8, 16, 16))
        painter.drawEllipse(QRectF(right.center().x() - 8, right.center().y() - 8, 16, 16))
        painter.setBrush(QColor("#d4c8a6"))
        painter.drawEllipse(QRectF(left.center().x() - 3, left.center().y() - 3, 6, 6))
        painter.drawEllipse(QRectF(right.center().x() - 3, right.center().y() - 3, 6, 6))

        painter.setFont(QFont("Helvetica", 8))
        painter.setPen(QPen(QColor("#e8ddbf"), 1))
        painter.drawText(QRectF(rect.left(), rect.bottom() - 13, rect.width(), 12),
                         Qt.AlignCenter, filename or "mounted")

    def _draw_empty_transport(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QPen(QColor("#514b40"), 1))
        painter.setBrush(QColor("#191a18"))
        painter.drawEllipse(QRectF(rect.left() + 6, rect.top() + 2, 44, 44))
        painter.drawEllipse(QRectF(rect.left() + 68, rect.top() + 2, 44, 44))
        painter.setFont(QFont("Helvetica", 7))
        painter.setPen(QPen(QColor("#807665"), 1))
        painter.drawText(QRectF(rect.left(), rect.bottom() - 13, rect.width(), 12),
                         Qt.AlignCenter, "empty")

    def _draw_button(self, painter: QPainter, rect: QRectF, label: str, active: bool) -> None:
        painter.setPen(QPen(QColor("#5b5448"), 1))
        painter.setBrush(QColor("#d2c8b5") if active else QColor("#8f887b"))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setFont(QFont("Helvetica", 7))
        painter.setPen(QPen(QColor("#211f1b"), 1))
        painter.drawText(rect, Qt.AlignCenter, label)

    def _draw_lamp(self, painter: QPainter, rect: QRectF, color: QColor, lit: bool) -> None:
        if lit:
            glow = QColor(color)
            glow.setAlpha(70)
            painter.setPen(Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(rect.adjusted(-3, -3, 3, 3))
        painter.setPen(QPen(QColor("#111"), 1))
        painter.setBrush(color)
        painter.drawEllipse(rect)


class RK05PanelWidget(QWidget):
    """Painted RK05 removable disk panel with four RK8E units."""

    def __init__(self, frontend):
        super().__init__()
        self.frontend = frontend
        self._pack_buttons: dict[int, QRectF] = {}
        self._eject_buttons: dict[int, QRectF] = {}
        self._unit_rects: dict[int, QRectF] = {}
        self.setMinimumSize(756, 368)
        self.setMaximumHeight(400)
        self.setFocusPolicy(Qt.StrongFocus)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._pack_buttons = {}
        self._eject_buttons = {}
        self._unit_rects = {}

        panel = QRectF(0, 0, self.width(), self.height()).adjusted(4, 4, -4, -4)
        painter.setPen(QPen(QColor("#c9c3b7"), 2))
        painter.setBrush(QColor("#171817"))
        painter.drawRoundedRect(panel, 8, 8)

        title_font = QFont("Helvetica", 10)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QPen(QColor("#d4c8a6"), 1))
        painter.drawText(panel.adjusted(14, 10, -14, 0), Qt.AlignLeft | Qt.AlignTop, "RK8E / RK05 DECpack")

        units = self.frontend.rk05_units()
        cols = 2
        cell_w = (panel.width() - 34) / cols
        cell_h = (panel.height() - 48) / 2
        for unit in units:
            idx = int(unit["unit"])
            col = idx % 2
            row = idx // 2
            rect = QRectF(
                panel.left() + 12 + col * (cell_w + 10),
                panel.top() + 36 + row * (cell_h + 8),
                cell_w,
                cell_h,
            )
            self._unit_rects[idx] = QRectF(rect)
            self._draw_drive(painter, rect, unit)

    def mousePressEvent(self, event) -> None:
        pos = event.position()
        for unit, rect in self._pack_buttons.items():
            if rect.contains(pos):
                self.frontend.select_rk05_pack(unit)
                return
        for unit, rect in self._eject_buttons.items():
            if rect.contains(pos):
                self.frontend.eject_rk05_pack(unit)
                return
        for unit, rect in self._unit_rects.items():
            if rect.contains(pos):
                self.frontend.open_rk05_detail(unit)
                return
        super().mousePressEvent(event)

    def _draw_drive(self, painter: QPainter, rect: QRectF, unit: dict) -> None:
        attached = bool(unit.get("attached"))
        active = bool(unit.get("active")) and self.frontend.rk05_blink_on()
        unit_number = int(unit["unit"])
        ready = attached and bool(unit.get("run")) and not bool(unit.get("fault"))

        painter.setPen(QPen(QColor("#545047"), 1))
        painter.setBrush(QColor("#20211f"))
        painter.drawRoundedRect(rect, 6, 6)

        window = QRectF(rect.left() + 10, rect.top() + 10, rect.width() - 20, rect.height() * 0.44)
        painter.setPen(QPen(QColor("#0c0d0c"), 2))
        painter.setBrush(QColor("#050606"))
        painter.drawRoundedRect(window, 5, 5)
        painter.setBrush(QColor(70, 90, 95, 92))
        painter.drawRect(window.adjusted(8, window.height() * 0.35, -8, -10))

        if attached:
            self._draw_pack_in_window(painter, window, os.path.basename(unit.get("file") or ""))
        else:
            painter.setFont(QFont("Helvetica", 8))
            painter.setPen(QPen(QColor("#6f675b"), 1))
            painter.drawText(window, Qt.AlignCenter, "empty")

        lower = QRectF(rect.left() + 10, window.bottom() + 8, rect.width() - 20, rect.bottom() - window.bottom() - 18)
        painter.setPen(QPen(QColor("#d8cfb8"), 1))
        painter.setBrush(QColor("#111211"))
        painter.drawRect(lower)

        painter.setFont(QFont("Helvetica", 9))
        painter.setPen(QPen(QColor("#e6e0d0"), 1))
        painter.drawText(QRectF(lower.left() + 12, lower.top() + 10, 96, 20), Qt.AlignLeft, "decpack")
        painter.setFont(QFont("Helvetica", 7))
        painter.drawText(QRectF(lower.left() + 14, lower.top() + 28, 112, 14), Qt.AlignLeft, "RK05 2200 BPI")
        painter.drawText(QRectF(lower.left() + 14, lower.top() + 42, 132, 14), Qt.AlignLeft, "digital equipment corporation")

        name_font = QFont("Helvetica", 14)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(QPen(QColor("#f2f2ec"), 1))
        painter.drawText(QRectF(lower.right() - 54, lower.top() + 14, 38, 32), Qt.AlignCenter, str(unit_number))

        button_w = 62
        pack_button = QRectF(lower.left() + lower.width() * 0.46, lower.top() + 12, button_w, 20)
        eject_button = QRectF(pack_button.right() + 8, pack_button.top(), button_w, 20)
        self._pack_buttons[unit_number] = QRectF(pack_button)
        self._eject_buttons[unit_number] = QRectF(eject_button)
        self._draw_button(painter, pack_button, "Pack", attached)
        self._draw_button(painter, eject_button, "Eject", attached)

        lamp_y = lower.top() + 44
        lamps = [
            ("PWR", bool(unit.get("run")), QColor("#31c15b")),
            ("RDY", ready, QColor("#eeeede")),
            ("FLT", bool(unit.get("fault")), QColor("#b52825")),
            ("RD", active, QColor("#eeeede")),
        ]
        for idx, (label, lit, color) in enumerate(lamps):
            lx = pack_button.left() + idx * 25
            self._draw_lamp(painter, QRectF(lx, lamp_y, 10, 10), color if lit else QColor("#473f38"), lit)
            painter.setFont(QFont("Helvetica", 5))
            painter.setPen(QPen(QColor("#d4c8a6"), 1))
            painter.drawText(QRectF(lx - 6, lamp_y + 11, 24, 8), Qt.AlignCenter, label)

    def _draw_pack_in_window(self, painter: QPainter, rect: QRectF, filename: str) -> None:
        pack = QRectF(rect.left() + 28, rect.top() + rect.height() * 0.34, rect.width() - 56, rect.height() * 0.52)
        painter.setPen(QPen(QColor("#afa994"), 1))
        painter.setBrush(QColor("#d1d5d2"))
        painter.drawRoundedRect(pack, 10, 10)
        painter.setBrush(QColor(160, 26, 42))
        painter.drawPie(pack.adjusted(10, 8, -10, -8), 205 * 16, 100 * 16)
        painter.setBrush(QColor("#191a18"))
        painter.drawEllipse(QRectF(pack.center().x() - 14, pack.center().y() - 14, 28, 28))
        label = QRectF(pack.left() + 16, pack.top() + 12, pack.width() - 32, 24)
        painter.setPen(QPen(QColor("#c5b894"), 1))
        painter.setBrush(QColor("#dacda7"))
        painter.drawRoundedRect(label, 3, 3)
        draw_handwritten_pack_label(painter, label, filename or "mounted", 10)

    def _draw_button(self, painter: QPainter, rect: QRectF, label: str, active: bool) -> None:
        painter.setPen(QPen(QColor("#5b5448"), 1))
        painter.setBrush(QColor("#d2c8b5") if active else QColor("#8f887b"))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setFont(QFont("Helvetica", 7))
        painter.setPen(QPen(QColor("#211f1b"), 1))
        painter.drawText(rect, Qt.AlignCenter, label)

    def _draw_lamp(self, painter: QPainter, rect: QRectF, color: QColor, lit: bool) -> None:
        if lit:
            glow = QColor(color)
            glow.setAlpha(72)
            painter.setPen(Qt.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(rect.adjusted(-4, -4, 4, 4))
        painter.setPen(QPen(QColor("#111"), 1))
        painter.setBrush(color)
        painter.drawEllipse(rect)


class TeletypeWidget(QWidget):
    """Painted Model 33 inspired terminal with paper and machine body."""

    def __init__(self, frontend, font: QFont, terminal_columns: int = 72):
        super().__init__()
        self.frontend = frontend
        self.terminal_font = font
        self.terminal_columns = max(40, int(terminal_columns))
        self._lines = [""]
        self._scroll_offset_lines = 0
        self.reader = None
        self.punch = None
        self.show_tape_codes = False
        self._reader_load_rect = QRectF()
        self._reader_codes_rect = QRectF()
        self._reader_start_rect = QRectF()
        self._reader_stop_rect = QRectF()
        self._reader_free_rect = QRectF()
        self._reader_tape_window_rect = QRectF()
        self._punch_file_rect = QRectF()
        self._punch_mode_rect = QRectF()
        self._punch_on_rect = QRectF()
        self._punch_off_rect = QRectF()
        self._scroll_wheel_rect = QRectF()
        self._scroll_end_rect = QRectF()
        self._dragging_scroll = False
        self._last_scroll_drag_y = 0.0
        self._dragging_reader_tape = False
        self._reader_drag_start_y = 0.0
        self._reader_drag_start_position = 0
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(self.recommended_window_width(), 760)

    def recommended_window_width(self) -> int:
        """Return a width large enough to show the full printable paper."""
        side_modules = 250 + 280
        side_gaps = 188
        return int(side_modules + side_gaps + self._required_paper_width())

    def _required_printable_width(self) -> int:
        metrics = QFontMetrics(self.terminal_font)
        char_w = max(metrics.horizontalAdvance("M"), metrics.averageCharWidth(), 10)
        return int(char_w * self.terminal_columns)

    def _required_paper_width(self) -> int:
        return self._required_printable_width() + 100

    def set_reader(self, reader: QtPaperTapeReader) -> None:
        """Attach the visual paper tape reader to its backend."""
        self.reader = reader
        self.show_tape_codes = reader.config.get("show_codes", default=False)
        self.update()

    def set_punch(self, punch: QtPaperTapePunch) -> None:
        """Attach the visual paper tape punch to its backend."""
        self.punch = punch
        self.update()

    def append_text(self, text: str) -> None:
        """Append terminal text to the paper buffer."""
        old_count = len(self._lines)
        for ch in text:
            if ch == "\r":
                continue
            if ch == "\n":
                self._lines.append("")
            elif ch == "\b":
                self._lines[-1] = self._lines[-1][:-1]
            elif ch.isprintable():
                self._lines[-1] += ch
        self._preserve_or_follow_bottom(old_count)
        self.update()

    def set_lines(self, lines: list[str]) -> None:
        """Replace the paper buffer with terminal history lines."""
        if not lines:
            lines = [""]
        if self._lines == lines:
            return
        old_count = len(self._lines)
        self._lines = lines[:]
        self._preserve_or_follow_bottom(old_count)
        self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_PageUp:
            self._scroll_by(self._visible_line_count() - 1)
            return
        if event.key() == Qt.Key_PageDown:
            self._scroll_by(-(self._visible_line_count() - 1))
            return
        if event.key() == Qt.Key_Home:
            self._scroll_offset_lines = self._max_scroll_offset()
            self.update()
            return
        if event.key() == Qt.Key_End:
            self._scroll_offset_lines = 0
            self.update()
            return
        if self.frontend.handle_key_event(event):
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() // 120
        if steps:
            self._scroll_by(steps * 3)
        event.accept()

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#26241f"))

        paper_rect, printable_rect, cap_rect = self._layout_rects()
        self._draw_paper(painter, paper_rect)
        self._draw_text(painter, printable_rect)
        self._draw_machine_body(painter, cap_rect)
        self._draw_platen_scroll(painter, paper_rect, cap_rect)
        self._draw_reader(painter, cap_rect)
        self._draw_punch(painter, cap_rect)

    def mousePressEvent(self, event) -> None:
        pos = event.position()
        self.setFocus(Qt.MouseFocusReason)
        if self._can_manually_move_reader_tape() and self._reader_tape_window_rect.contains(pos):
            self._dragging_reader_tape = True
            self._reader_drag_start_y = pos.y()
            self._reader_drag_start_position = self.reader.position
            event.accept()
            return
        if self._reader_load_rect.contains(pos):
            self.frontend.load_reader_tape()
            return
        if self._reader_codes_rect.contains(pos):
            self.show_tape_codes = not self.show_tape_codes
            self.update()
            return
        if self._reader_start_rect.contains(pos):
            self.frontend.reader_start()
            return
        if self._reader_stop_rect.contains(pos):
            self.frontend.reader_stop()
            return
        if self._reader_free_rect.contains(pos):
            self.frontend.reader_free()
            return
        if self._punch_file_rect.contains(pos):
            self.frontend.punch_select_output()
            return
        if self._punch_off_rect.contains(pos):
            self.frontend.punch_off()
            return
        if self._scroll_end_rect.contains(pos):
            self._scroll_to_bottom()
            return
        _, printable_rect, _ = self._layout_rects()
        if self._scroll_wheel_rect.contains(pos) or printable_rect.contains(pos):
            self._dragging_scroll = True
            self._last_scroll_drag_y = pos.y()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        pos = event.position()
        if self._can_manually_move_reader_tape() and self._reader_tape_window_rect.contains(pos):
            self.frontend.reader_unload()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging_reader_tape:
            y = event.position().y()
            dy = self._reader_drag_start_y - y
            threshold = self._reader_manual_row_pitch()
            delta = int(dy / threshold)
            if self.reader is not None:
                self.reader.set_position(self._reader_drag_start_position + delta)
                self.update()
            event.accept()
            return
        if self._dragging_scroll:
            y = event.position().y()
            dy = y - self._last_scroll_drag_y
            threshold = max(6, int(self._line_height() * 0.35))
            if abs(dy) >= threshold:
                self._scroll_by(int(dy / threshold))
                self._last_scroll_drag_y = y
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging_reader_tape:
            self._dragging_reader_tape = False
            event.accept()
            return
        if self._dragging_scroll:
            self._dragging_scroll = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _can_manually_move_reader_tape(self) -> bool:
        return bool(
            self.reader is not None and
            self.reader.tape_loaded and
            self.reader.state == "FREE"
        )

    def _reader_manual_row_pitch(self) -> float:
        if self._reader_tape_window_rect.height() > 0:
            return max(13.0, self._reader_tape_window_rect.height() / 18.0)
        return 14.0

    def _layout_rects(self) -> tuple[QRectF, QRectF, QRectF]:
        w = self.width()
        h = self.height()
        cap_h = max(210, int(h * 0.32))
        cap_top = h - cap_h
        paper_w = self._required_paper_width()
        reader_w = min(250, max(220, int(w * 0.22)))
        punch_w = min(280, max(240, int(w * 0.18)))
        device_w = 0
        paper_x = max(reader_w + 76, (w - paper_w) / 2)
        max_paper_x = w - punch_w - device_w - 96 - paper_w
        if paper_x > max_paper_x:
            paper_x = max(reader_w + 76, max_paper_x)
        paper_y = -max(480, int(h * 0.88))
        paper_h = cap_top - paper_y + cap_h * 0.42
        paper_rect = QRectF(paper_x, paper_y, paper_w, paper_h)
        visible_paper_top = 22
        printable_rect = QRectF(
            paper_rect.left() + 50,
            visible_paper_top + 18,
            paper_rect.width() - 100,
            cap_top - visible_paper_top - 66,
        )
        cap_rect = QRectF(0, cap_top, w, cap_h + 24)
        return paper_rect, printable_rect, cap_rect

    def _draw_paper(self, painter: QPainter, paper_rect: QRectF) -> None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 70))
        painter.drawRect(paper_rect.translated(7, 12))
        painter.setBrush(QColor("#e5c18a"))
        painter.drawRect(paper_rect)

        painter.setPen(QPen(QColor("#c99f69"), 1))
        margin_left = paper_rect.left() + 32
        margin_right = paper_rect.right() - 32
        painter.drawLine(margin_left, 0, margin_left, paper_rect.bottom() - 18)
        painter.drawLine(margin_right, 0, margin_right, paper_rect.bottom() - 18)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 244, 208, 45))
        painter.drawRect(paper_rect.adjusted(10, 8, -10, -paper_rect.height() * 0.68))
        self._draw_paper_age(painter, paper_rect)

    def _draw_paper_age(self, painter: QPainter, paper_rect: QRectF) -> None:
        """Add faint folds and age marks to the paper surface."""
        painter.save()
        painter.setClipRect(self.rect())

        fold_pen = QPen(QColor(139, 121, 82, 42), 1)
        highlight_pen = QPen(QColor(255, 250, 225, 52), 1)
        fold_y_values = [
            paper_rect.top() + paper_rect.height() * 0.24,
            paper_rect.top() + paper_rect.height() * 0.43,
            paper_rect.top() + paper_rect.height() * 0.69,
        ]
        for index, y in enumerate(fold_y_values):
            path = QPainterPath()
            path.moveTo(paper_rect.left() + 18, y)
            path.cubicTo(
                paper_rect.left() + paper_rect.width() * 0.30,
                y + (5 if index % 2 == 0 else -4),
                paper_rect.left() + paper_rect.width() * 0.62,
                y + (-4 if index % 2 == 0 else 5),
                paper_rect.right() - 18,
                y + 1,
            )
            painter.setPen(fold_pen)
            painter.drawPath(path)
            painter.setPen(highlight_pen)
            painter.drawPath(path.translated(0, -1))

        painter.setPen(QPen(QColor(118, 93, 52, 26), 1))
        for i in range(34):
            x = paper_rect.left() + 24 + ((i * 47) % int(max(1, paper_rect.width() - 48)))
            y = paper_rect.top() + 40 + ((i * 83) % int(max(1, paper_rect.height() - 80)))
            painter.drawPoint(int(x), int(y))

        painter.setPen(QPen(QColor(120, 95, 60, 34), 1))
        for offset in (0, 1):
            painter.drawLine(
                int(paper_rect.left() + 8 + offset),
                int(max(0, paper_rect.top())),
                int(paper_rect.left() + 8 + offset),
                int(paper_rect.bottom() - 28),
            )
            painter.drawLine(
                int(paper_rect.right() - 8 - offset),
                int(max(0, paper_rect.top())),
                int(paper_rect.right() - 8 - offset),
                int(paper_rect.bottom() - 28),
            )
        painter.restore()

    def _draw_machine_body(self, painter: QPainter, cap_rect: QRectF) -> None:
        w = cap_rect.width()
        body = QRectF(cap_rect.left() + 46, cap_rect.top() + 18, w - 92, cap_rect.height() * 0.56)
        shadow = body.translated(0, 10)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 58))
        painter.drawRoundedRect(shadow, 34, 34)

        painter.setBrush(QColor("#f1e7c8"))
        painter.drawRoundedRect(body, 34, 34)
        painter.setBrush(QColor(255, 250, 224, 70))
        painter.drawRoundedRect(body.adjusted(12, 8, -12, -body.height() * 0.52), 28, 28)

        slot = QRectF(body.left() + 72, body.top() - 3, body.width() - 144, 18)
        painter.setBrush(QColor("#5e574b"))
        painter.drawRoundedRect(slot, 7, 7)

        throat = QRectF(body.left() + body.width() * 0.18, body.top() - 28, body.width() * 0.64, 26)
        painter.setBrush(QColor("#b9b1a2"))
        painter.drawRoundedRect(throat, 8, 8)
        painter.setBrush(QColor("#3f3a32"))
        painter.drawRoundedRect(throat.adjusted(8, 7, -8, -6), 5, 5)

        for x in (throat.left() + 18, throat.right() - 18):
            guide = QRectF(x - 5, throat.top() - 62, 10, 72)
            painter.setBrush(QColor("#a9a99f"))
            painter.drawRoundedRect(guide, 4, 4)
            painter.setBrush(QColor(255, 255, 245, 80))
            painter.drawRect(guide.adjusted(2, 2, -5, -2))

        reader_clearance = min(250, max(220, self.width() * 0.22)) + 74
        logo_x = max(body.left() + reader_clearance, body.left() + body.width() * 0.22)
        self._draw_teletype_logo(painter, QRectF(logo_x, body.top() + 42, 172, 72))

        key_bar = QRectF(body.left() + 130, body.bottom() - 10, body.width() - 260, 18)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#6c6255"))
        painter.drawRoundedRect(key_bar, 6, 6)

    def _draw_teletype_logo(self, painter: QPainter, rect: QRectF) -> None:
        painter.save()
        ink = QColor("#34373a")
        body_color = QColor("#f1e7c8")

        logo_font = QFont("Helvetica", 9)
        logo_font.setBold(True)
        logo_font.setLetterSpacing(QFont.PercentageSpacing, 165)
        painter.setFont(logo_font)
        painter.setPen(QPen(ink, 1))
        painter.drawText(
            QRectF(rect.left() + 4, rect.top(), rect.width() - 8, 16),
            Qt.AlignCenter | Qt.AlignTop,
            "TELETYPE",
        )

        mark = QRectF(rect.left() + 8, rect.top() + 23, rect.width() - 20, 36)
        path = QPainterPath()
        path.moveTo(mark.left(), mark.top() + 32)
        path.lineTo(mark.left() + 5, mark.top() + 8)
        path.lineTo(mark.left() + 55, mark.top() + 8)
        path.lineTo(mark.left() + 55, mark.top())
        path.lineTo(mark.right() - 55, mark.top())
        path.lineTo(mark.right() - 55, mark.top() + 8)
        path.lineTo(mark.right() - 5, mark.top() + 8)
        path.lineTo(mark.right(), mark.top() + 32)
        path.lineTo(mark.right() - 16, mark.top() + 32)
        path.cubicTo(
            mark.right() - 24, mark.top() + 15,
            mark.right() - 45, mark.top() + 15,
            mark.right() - 52, mark.top() + 32,
        )
        path.lineTo(mark.left() + 52, mark.top() + 32)
        path.cubicTo(
            mark.left() + 45, mark.top() + 15,
            mark.left() + 24, mark.top() + 15,
            mark.left() + 16, mark.top() + 32,
        )
        path.closeSubpath()

        painter.setPen(Qt.NoPen)
        painter.setBrush(ink)
        painter.drawPath(path)

        painter.setBrush(body_color)
        painter.drawEllipse(QRectF(mark.left() + 17, mark.top() + 13, 34, 34))
        painter.drawEllipse(QRectF(mark.right() - 51, mark.top() + 13, 34, 34))

        emblem = QRectF(mark.center().x() - 19, mark.top() + 5, 38, 38)
        painter.setBrush(body_color)
        painter.drawEllipse(emblem)

        painter.setPen(QPen(ink, 3))
        waveform = QPainterPath()
        waveform.moveTo(emblem.left() + 9, emblem.center().y() + 8)
        waveform.lineTo(emblem.left() + 13, emblem.center().y() + 8)
        waveform.lineTo(emblem.left() + 13, emblem.center().y() - 8)
        waveform.lineTo(emblem.center().x() - 1, emblem.center().y() - 8)
        waveform.lineTo(emblem.center().x() - 1, emblem.center().y() + 8)
        waveform.lineTo(emblem.right() - 13, emblem.center().y() + 8)
        waveform.lineTo(emblem.right() - 13, emblem.center().y() - 8)
        waveform.lineTo(emblem.right() - 9, emblem.center().y() - 8)
        painter.drawPath(waveform)

        reg = QRectF(mark.right() + 3, mark.bottom() - 12, 10, 10)
        painter.setPen(QPen(ink, 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(reg)
        reg_font = QFont("Helvetica", 6)
        reg_font.setBold(True)
        painter.setFont(reg_font)
        painter.drawText(reg, Qt.AlignCenter, "R")
        painter.restore()

    def _draw_platen_scroll(self, painter: QPainter, paper_rect: QRectF, cap_rect: QRectF) -> None:
        """Draw a typewriter-like paper advance wheel and return-to-end control."""
        wheel_size = 70
        wheel_x = max(10, paper_rect.left() - wheel_size - 28)
        wheel_y = cap_rect.top() + 34
        self._scroll_wheel_rect = QRectF(wheel_x, wheel_y, wheel_size, wheel_size)
        self._scroll_end_rect = QRectF(wheel_x + 8, wheel_y + wheel_size + 10, wheel_size - 16, 22)

        painter.save()
        bracket = QRectF(
            self._scroll_wheel_rect.center().x() - 10,
            self._scroll_wheel_rect.center().y() - 5,
            paper_rect.left() - self._scroll_wheel_rect.center().x() + 12,
            10,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#a79f90"))
        painter.drawRoundedRect(bracket, 4, 4)

        painter.setPen(QPen(QColor("#6e675c"), 2))
        painter.setBrush(QColor("#d8d0bd"))
        painter.drawEllipse(self._scroll_wheel_rect)

        inner = self._scroll_wheel_rect.adjusted(8, 8, -8, -8)
        painter.setPen(QPen(QColor("#b8af9c"), 1))
        painter.setBrush(QColor("#eee5d1"))
        painter.drawEllipse(inner)

        notch_count = 28
        center = self._scroll_wheel_rect.center()
        radius_outer = wheel_size * 0.48
        radius_inner = wheel_size * 0.37
        phase = self._scroll_offset_lines % notch_count
        for i in range(notch_count):
            angle = ((i + phase * 0.18) / notch_count) * 6.283185307
            x1 = center.x() + math.cos(angle) * radius_inner
            y1 = center.y() + math.sin(angle) * radius_inner
            x2 = center.x() + math.cos(angle) * radius_outer
            y2 = center.y() + math.sin(angle) * radius_outer
            painter.setPen(QPen(QColor("#81796d"), 2 if i % 2 == 0 else 1))
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))

        painter.setPen(QPen(QColor("#6e675c"), 2))
        painter.setBrush(QColor("#c8bfac"))
        painter.drawEllipse(QRectF(center.x() - 13, center.y() - 13, 26, 26))
        painter.setBrush(QColor("#81786b"))
        painter.setPen(QPen(QColor("#5c554b"), 1))
        painter.drawEllipse(QRectF(center.x() - 5, center.y() - 5, 10, 10))

        active = self._scroll_offset_lines > 0
        painter.setPen(QPen(QColor("#6b6255"), 1))
        painter.setBrush(QColor("#e1d8c5") if active else QColor("#b4ac9d"))
        painter.drawRoundedRect(self._scroll_end_rect, 5, 5)
        painter.setPen(QPen(QColor("#2f2b25"), 1))
        font = QFont("Helvetica", 7)
        font.setBold(active)
        painter.setFont(font)
        painter.drawText(self._scroll_end_rect, Qt.AlignCenter, "FIN")
        painter.restore()

    def _draw_reader(self, painter: QPainter, cap_rect: QRectF) -> None:
        reader_rect = QRectF(
            cap_rect.left() + 24,
            58,
            min(250, max(220, self.width() * 0.22)),
            cap_rect.bottom() - 112,
        )
        painter.setPen(QPen(QColor("#777064"), 1))
        painter.setBrush(QColor("#bdb6a7"))
        painter.drawRoundedRect(reader_rect, 12, 12)

        button_w = (reader_rect.width() - 48) / 3
        y = reader_rect.bottom() - 48
        label_top = y - 126
        window_bottom = label_top - 14

        painter.setBrush(QColor("#948c7d"))
        throat = QRectF(
            reader_rect.left() + 22,
            reader_rect.top() + 24,
            reader_rect.width() - 44,
            max(310, window_bottom - reader_rect.top() - 14),
        )
        painter.drawRoundedRect(throat, 6, 6)

        window = QRectF(
            reader_rect.left() + 22,
            reader_rect.top() + 34,
            reader_rect.width() - 44,
            max(290, window_bottom - reader_rect.top() - 34),
        )
        self._reader_tape_window_rect = QRectF(window)
        painter.setBrush(QColor(50, 58, 60, 135))
        painter.drawRoundedRect(window, 4, 4)
        loaded = bool(self.reader and self.reader.tape_loaded)
        reader_state = self.reader.state if self.reader is not None else "FREE"
        if loaded:
            self._draw_reader_tape(painter, window)
        else:
            painter.setPen(QPen(QColor("#d4c8a6"), 1))
            painter.setFont(QFont("Helvetica", 8))
            painter.drawText(window, Qt.AlignCenter, "NO TAPE")
        self._draw_reader_clamp(painter, window, loaded, reader_state)

        painter.setPen(QPen(QColor("#514c43"), 1))
        label_font = QFont("Helvetica", 9)
        label_font.setBold(True)
        painter.setFont(label_font)
        painter.drawText(
            QRectF(reader_rect.left() + 18, label_top, reader_rect.width() - 36, 36),
            Qt.AlignLeft | Qt.AlignTop,
            "PAPER TAPE\nREADER",
        )
        if loaded:
            info_font = QFont("Helvetica", 8)
            painter.setFont(info_font)
            painter.setPen(QPen(QColor("#5f5749"), 1))
            name = self.reader.tape_name
            if len(name) > 20:
                name = name[:17] + "..."
            pct = int(self.reader.progress() * 100)
            note = "FREE: DRAG / DBL-CLICK OUT" if reader_state == "FREE" else f"{pct}% READ"
            painter.drawText(
                QRectF(reader_rect.left() + 18, label_top + 42, reader_rect.width() - 62, 42),
                Qt.AlignLeft | Qt.AlignTop,
                f"{name}\n{note}",
            )

        self._reader_load_rect = QRectF(reader_rect.left() + 18, y - 34, reader_rect.width() - 36, 24)
        self._reader_codes_rect = QRectF(reader_rect.left() + 18, y - 64, reader_rect.width() - 36, 24)
        self._reader_start_rect = QRectF(reader_rect.left() + 18, y, button_w, 26)
        self._reader_stop_rect = QRectF(self._reader_start_rect.right() + 6, y, button_w, 26)
        self._reader_free_rect = QRectF(self._reader_stop_rect.right() + 6, y, button_w, 26)
        self._draw_codes_button(painter, self._reader_codes_rect)
        self._draw_load_button(painter, self._reader_load_rect, enabled=reader_state == "FREE", loaded=loaded)
        self._draw_reader_button(painter, self._reader_start_rect, "START")
        self._draw_reader_button(painter, self._reader_stop_rect, "STOP")
        self._draw_reader_button(painter, self._reader_free_rect, "FREE")

    def _draw_reader_clamp(
        self,
        painter: QPainter,
        window: QRectF,
        loaded: bool,
        reader_state: str,
    ) -> None:
        """Draw the transparent paper tape hold-down from the ASR-33 reader."""
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        base = QRectF(
            window.left() + 16,
            window.bottom() - 70,
            window.width() - 32,
            48,
        )
        hinge_y = base.bottom() + 3
        active = bool(self.reader and self.reader.active)
        animation_tick = getattr(self, "_sound_tick", 0)
        jitter = 1.5 if active and int(animation_tick / 4) % 2 else 0.0

        painter.setPen(QPen(QColor("#161613"), 1))
        painter.setBrush(QColor("#25231e"))
        painter.drawRoundedRect(base.adjusted(0, 20, 0, 4), 3, 3)

        painter.setPen(QPen(QColor("#5a554b"), 2))
        painter.drawLine(
            int(base.left() + 8),
            int(hinge_y),
            int(base.right() - 8),
            int(hinge_y),
        )

        clamped = loaded and reader_state != "FREE"
        if clamped:
            cover = base.adjusted(5, 6 + jitter, -5, -7 + jitter)
            painter.setPen(QPen(QColor(220, 224, 216, 120), 1))
            painter.setBrush(QColor(205, 218, 215, 86))
            painter.drawRoundedRect(cover, 5, 5)
            painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
            painter.drawLine(
                int(cover.left() + 8),
                int(cover.top() + 8),
                int(cover.right() - 8),
                int(cover.top() + 2),
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#d8d1bd"))
            for x in (cover.left() + 16, cover.right() - 22):
                painter.drawEllipse(QRectF(x, cover.bottom() - 11, 7, 7))
            painter.setPen(QPen(QColor("#4e493f"), 1))
            painter.setBrush(QColor("#776f60"))
            painter.drawRoundedRect(QRectF(cover.right() - 18, cover.top() + 8, 12, 22), 3, 3)
        else:
            lift = 8 if loaded else 0
            cover = QPainterPath()
            cover.moveTo(base.left() + 4, base.bottom() - 8 - lift)
            cover.lineTo(base.right() - 4, base.bottom() - 32 - lift)
            cover.lineTo(base.right() - 15, base.bottom() - 47 - lift)
            cover.lineTo(base.left() - 2, base.bottom() - 18 - lift)
            cover.closeSubpath()
            painter.setPen(QPen(QColor(220, 224, 216, 125), 1))
            painter.setBrush(QColor(205, 218, 215, 70))
            painter.drawPath(cover)
            painter.setPen(QPen(QColor(255, 255, 255, 80), 1))
            painter.drawLine(
                int(base.left() + 12),
                int(base.bottom() - 17 - lift),
                int(base.right() - 20),
                int(base.bottom() - 41 - lift),
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#776f60"))
            painter.drawEllipse(QRectF(base.right() - 26, base.bottom() - 41 - lift, 11, 11))
            if loaded:
                painter.setPen(QPen(QColor("#d6ccb3"), 1))
                painter.setFont(QFont("Helvetica", 7))
                painter.drawText(
                    QRectF(base.left() + 6, base.top() + 1, base.width() - 12, 14),
                    Qt.AlignCenter,
                    "TAPE FREE",
                )

        painter.restore()

    def _draw_reader_button(self, painter: QPainter, rect: QRectF, label: str) -> None:
        state = self.reader.state if self.reader is not None else "FREE"
        active = state == label
        painter.setPen(QPen(QColor("#6b6255"), 1))
        painter.setBrush(QColor("#e1d8c5") if active else QColor("#a79f90"))
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QPen(QColor("#2f2b25"), 1))
        font = QFont("Helvetica", 7)
        font.setBold(active)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, label)

    def _draw_load_button(self, painter: QPainter, rect: QRectF, enabled: bool, loaded: bool) -> None:
        painter.setPen(QPen(QColor("#6b6255"), 1))
        painter.setBrush(QColor("#d2c8b5") if enabled else QColor("#8f887b"))
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QPen(QColor("#2f2b25") if enabled else QColor("#5b554a"), 1))
        font = QFont("Helvetica", 8)
        font.setBold(True)
        painter.setFont(font)
        if not enabled:
            label = "LOCKED"
        elif loaded:
            label = "CHANGE TAPE"
        else:
            label = "LOAD TAPE"
        painter.drawText(rect, Qt.AlignCenter, label)

    def _draw_codes_button(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QPen(QColor("#6b6255"), 1))
        painter.setBrush(QColor("#e7ddc8") if self.show_tape_codes else QColor("#a79f90"))
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QPen(QColor("#2f2b25"), 1))
        font = QFont("Helvetica", 8)
        font.setBold(self.show_tape_codes)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, "CODES")

    def _draw_reader_tape(self, painter: QPainter, window: QRectF) -> None:
        code_w = 66 if self.show_tape_codes else 0
        tape = window.adjusted(12, 8, -(12 + code_w), -8)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#eadcb5"))
        painter.drawRoundedRect(tape, 2, 2)
        painter.setPen(QPen(QColor("#bcae86"), 1))
        painter.drawLine(int(tape.left() + 4), int(tape.top()), int(tape.left() + 4), int(tape.bottom()))
        painter.drawLine(int(tape.right() - 4), int(tape.top()), int(tape.right() - 4), int(tape.bottom()))

        if self.reader is None or not self.reader.tape_data:
            return

        col_map = {0: 0, 1: 1, 2: 2, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8}
        sprocket_col = 3
        pitch_y = max(13.0, tape.height() / 18.0)
        first_col_x = tape.left() + tape.width() * 0.11
        pitch_x = tape.width() * 0.096
        bit_radius = max(2.4, pitch_x * 0.42)
        sprocket_radius = max(1.7, bit_radius * 0.63)
        row_start = max(0, self.reader.position - 8)
        row_count = int(tape.height() // pitch_y) + 4
        phase = (self.reader.position % 1) * pitch_y

        painter.save()
        painter.setClipRect(window)
        code_font = QFont("Courier", 7)
        code_x = tape.right() + 7
        for visible_row in range(row_count):
            byte_index = row_start + visible_row
            if byte_index >= len(self.reader.tape_data):
                break
            byte = self.reader.tape_data[byte_index]
            y = tape.top() + visible_row * pitch_y - phase
            if y < tape.top() - pitch_y or y > tape.bottom() + pitch_y:
                continue

            sx = first_col_x + sprocket_col * pitch_x
            painter.setPen(QPen(QColor("#9f987f"), 1))
            painter.setBrush(QColor("#2f2c27"))
            painter.drawEllipse(QRectF(sx - sprocket_radius, y - sprocket_radius,
                                       sprocket_radius * 2, sprocket_radius * 2))

            for bit in range(8):
                x = first_col_x + col_map[bit] * pitch_x
                punched = bool((byte >> bit) & 1)
                if punched:
                    painter.setPen(QPen(QColor("#a6a08b"), 1))
                    painter.setBrush(QColor("#302c26"))
                    painter.drawEllipse(QRectF(x - bit_radius, y - bit_radius,
                                               bit_radius * 2, bit_radius * 2))
                else:
                    painter.setPen(QPen(QColor(150, 142, 116, 85), 1))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawEllipse(QRectF(x - bit_radius, y - bit_radius,
                                               bit_radius * 2, bit_radius * 2))
            if self.show_tape_codes:
                chrval = byte & 0x7F
                ch = chr(chrval) if 32 <= chrval < 127 else "."
                painter.setFont(code_font)
                painter.setPen(QPen(QColor("#2f2c27"), 1))
                painter.drawText(
                    QRectF(code_x, y - pitch_y * 0.42, 62, pitch_y),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    f"{ch} {byte:02X} {byte:03o}",
                )
        painter.restore()

    def _draw_punch(self, painter: QPainter, cap_rect: QRectF) -> None:
        punch_w = min(280, max(240, self.width() * 0.18))
        punch_rect = QRectF(
            self.width() - punch_w - 24,
            58,
            punch_w,
            cap_rect.bottom() - 112,
        )
        painter.setPen(QPen(QColor("#777064"), 1))
        painter.setBrush(QColor("#c3bba9"))
        painter.drawRoundedRect(punch_rect, 12, 12)

        throat = QRectF(
            punch_rect.left() + 22,
            punch_rect.top() + 24,
            punch_rect.width() - 44,
            punch_rect.height() - 200,
        )
        painter.setBrush(QColor("#8f887b"))
        painter.drawRoundedRect(throat, 6, 6)

        tape_window = QRectF(
            punch_rect.left() + 22,
            punch_rect.top() + 34,
            punch_rect.width() - 44,
            punch_rect.height() - 220,
        )
        painter.setBrush(QColor(50, 58, 60, 135))
        painter.drawRoundedRect(tape_window, 4, 4)
        if self.punch is not None and self.punch.has_output():
            self._draw_punch_tape(painter, tape_window)
        else:
            painter.setPen(QPen(QColor("#d4c8a6"), 1))
            painter.setFont(QFont("Helvetica", 8))
            painter.drawText(tape_window, Qt.AlignCenter, "NO OUTPUT")

        painter.setPen(QPen(QColor("#443e36"), 3))
        punch_head_y = tape_window.top() + tape_window.height() * 0.42
        painter.drawLine(
            int(tape_window.left() + 6),
            int(punch_head_y),
            int(tape_window.right() - 6),
            int(punch_head_y),
        )

        painter.setPen(QPen(QColor("#514c43"), 1))
        label_font = QFont("Helvetica", 9)
        label_font.setBold(True)
        painter.setFont(label_font)
        painter.drawText(
            punch_rect.adjusted(18, punch_rect.height() - 158, -18, 0),
            Qt.AlignLeft | Qt.AlignTop,
            "PAPER TAPE\nPUNCH PTP",
        )

        if self.punch is not None and self.punch.has_output():
            name = self.punch.output_name
            if len(name) > 22:
                name = name[:19] + "..."
            painter.setFont(QFont("Helvetica", 8))
            painter.setPen(QPen(QColor("#5f5749"), 1))
            painter.drawText(
                punch_rect.adjusted(18, punch_rect.height() - 118, -18, 0),
                Qt.AlignLeft | Qt.AlignTop,
                f"{name}\n{self.punch.byte_count} BYTES PTP",
            )

        y = punch_rect.bottom() - 82
        self._punch_mode_rect = QRectF()
        self._punch_on_rect = QRectF()
        self._punch_file_rect = QRectF(punch_rect.left() + 18, y - 18, punch_rect.width() - 36, 28)
        self._punch_off_rect = QRectF(punch_rect.left() + 18, y + 24, punch_rect.width() - 36, 28)
        self._draw_punch_file_button(painter, self._punch_file_rect)
        self._draw_punch_button(
            painter,
            self._punch_off_rect,
            "DETACH",
        )

    def _draw_device_rack(self, painter: QPainter, cap_rect: QRectF) -> None:
        rack_rect = QRectF(self.width() - 242, 58, 218, cap_rect.bottom() - 112)
        painter.setPen(QPen(QColor("#777064"), 1))
        painter.setBrush(QColor("#b7b0a2"))
        painter.drawRoundedRect(rack_rect, 12, 12)

        title_font = QFont("Helvetica", 8)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QPen(QColor("#514c43"), 1))
        painter.drawText(rack_rect.adjusted(14, 14, -14, 0), Qt.AlignLeft | Qt.AlignTop, "RK8E")

        units = self.frontend.rk05_units()
        self._rk05_unit_rects = {}
        unit_y = rack_rect.top() + 46
        for unit in units:
            unit_rect = QRectF(rack_rect.left() + 14, unit_y, rack_rect.width() - 28, 78)
            self._rk05_unit_rects[int(unit["unit"])] = QRectF(unit_rect)
            self._draw_rk05_unit(painter, unit_rect, unit)
            unit_y += 88
    def _draw_rk05_unit(self, painter: QPainter, rect: QRectF, unit: dict) -> None:
        attached = bool(unit.get("attached"))
        active = bool(unit.get("active")) and self.frontend.rk05_blink_on()

        painter.setPen(QPen(QColor("#6f675b"), 1))
        painter.setBrush(QColor("#d2c8b5") if attached else QColor("#aaa294"))
        painter.drawRoundedRect(rect, 7, 7)

        drive_rect = QRectF(rect.left() + 8, rect.top() + 10, 42, 42)
        painter.setPen(QPen(QColor("#4b453c"), 1))
        painter.setBrush(QColor("#d8cfb8") if attached else QColor("#8e8678"))
        painter.drawRoundedRect(drive_rect, 6, 6)
        painter.setBrush(QColor("#2f2c27"))
        painter.drawEllipse(QRectF(drive_rect.center().x() - 13, drive_rect.center().y() - 13, 26, 26))
        painter.setBrush(QColor("#c9bea2"))
        painter.drawEllipse(QRectF(drive_rect.center().x() - 5, drive_rect.center().y() - 5, 10, 10))
        painter.setPen(QPen(QColor("#7d735f"), 1))
        painter.drawLine(int(drive_rect.left() + 8), int(drive_rect.top() + 8),
                         int(drive_rect.right() - 8), int(drive_rect.top() + 8))

        label_font = QFont("Helvetica", 9)
        label_font.setBold(True)
        painter.setFont(label_font)
        painter.setPen(QPen(QColor("#2f2b25"), 1))
        painter.drawText(QRectF(rect.left() + 58, rect.top() + 7, 42, 18), Qt.AlignLeft, unit["name"])

        file_font = QFont("Helvetica", 6)
        painter.setFont(file_font)
        painter.setPen(QPen(QColor("#5f5749"), 1))
        filename = os.path.basename(unit.get("file") or "") or "empty"
        painter.drawText(QRectF(rect.left() + 58, rect.top() + 24, 80, 14), Qt.AlignLeft, filename)

        pack_font = QFont("Helvetica", 5)
        painter.setFont(pack_font)
        painter.setPen(QPen(QColor("#5f5749"), 1))
        painter.drawText(QRectF(rect.left() + 58, rect.top() + 40, 116, 11), Qt.AlignLeft, "DECpack")
        painter.drawText(QRectF(rect.left() + 58, rect.top() + 51, 116, 11), Qt.AlignLeft, "RK05 2200 BPI")
        painter.drawText(
            QRectF(rect.left() + 58, rect.top() + 62, 118, 11),
            Qt.AlignLeft,
            "digital equipment corporation",
        )

        self._draw_status_lamp(
            painter,
            QRectF(rect.right() - 20, rect.top() + 13, 10, 10),
            QColor("#31c15b") if attached else QColor("#473f38"),
            attached,
        )
        self._draw_status_lamp(
            painter,
            QRectF(rect.right() - 20, rect.top() + 36, 10, 10),
            QColor("#d83325") if active else QColor("#473f38"),
            active,
        )

        painter.setFont(QFont("Helvetica", 6))
        painter.setPen(QPen(QColor("#5f5749"), 1))
        painter.drawText(QRectF(rect.right() - 56, rect.top() + 8, 28, 14), Qt.AlignRight, "ATT")
        painter.drawText(QRectF(rect.right() - 56, rect.top() + 31, 28, 14), Qt.AlignRight, "ACT")

    def _draw_status_lamp(self, painter: QPainter, rect: QRectF, color: QColor, lit: bool) -> None:
        if lit:
            painter.setPen(Qt.NoPen)
            glow = QColor(color)
            glow.setAlpha(75)
            painter.setBrush(glow)
            painter.drawEllipse(rect.adjusted(-4, -4, 4, 4))
        painter.setPen(QPen(QColor("#3c372f"), 1))
        painter.setBrush(color)
        painter.drawEllipse(rect)

    def _draw_punch_file_button(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QPen(QColor("#6b6255"), 1))
        painter.setBrush(QColor("#d2c8b5"))
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QPen(QColor("#2f2b25"), 1))
        font = QFont("Helvetica", 8)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, "NEW TAPE")

    def _draw_punch_button(self, painter: QPainter, rect: QRectF, label: str) -> None:
        active = (
            self.punch is not None and
            self.punch.ptp_attached and label == "DETACH"
        )
        painter.setPen(QPen(QColor("#6b6255"), 1))
        painter.setBrush(QColor("#e1d8c5") if active else QColor("#a79f90"))
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QPen(QColor("#2f2b25"), 1))
        font = QFont("Helvetica", 7)
        font.setBold(active)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, label)

    def _draw_punch_tape(self, painter: QPainter, window: QRectF) -> None:
        tape = window.adjusted(12, 8, -12, -8)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#eadcb5"))
        painter.drawRoundedRect(tape, 2, 2)
        if self.punch is None:
            return
        painter.setPen(QPen(QColor("#bcae86"), 1))
        painter.drawLine(int(tape.left() + 4), int(tape.top()), int(tape.left() + 4), int(tape.bottom()))
        painter.drawLine(int(tape.right() - 4), int(tape.top()), int(tape.right() - 4), int(tape.bottom()))

        col_map = {0: 0, 1: 1, 2: 2, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8}
        sprocket_col = 3
        pitch_y = max(13.0, tape.height() / 18.0)
        first_col_x = tape.left() + tape.width() * 0.11
        pitch_x = tape.width() * 0.096
        bit_radius = max(2.4, pitch_x * 0.42)
        sprocket_radius = max(1.5, bit_radius * 0.63)
        head_y = tape.top() + tape.height() * 0.42

        painter.save()
        painter.setClipRect(window)

        scroll_offset = (self.punch.visual_phase % 1.0) * pitch_y
        self._draw_punch_sprocket_track(
            painter,
            first_col_x + sprocket_col * pitch_x,
            tape,
            pitch_y,
            sprocket_radius,
            scroll_offset,
        )
        if self.punch.output_name:
            label_y = tape.top() + 8 + self.punch.visual_phase * pitch_y
            if tape.top() - 28 <= label_y <= tape.bottom() + 8:
                label_rect = QRectF(tape.left() + 12, label_y, tape.width() - 24, 24)
                painter.setPen(QPen(QColor("#835d4d"), 1))
                label_font = QFont("Marker Felt", 12)
                label_font.setItalic(True)
                painter.setFont(label_font)
                painter.drawText(label_rect, Qt.AlignCenter, self.punch.output_name)

        blank_rows = int((head_y - tape.top()) // pitch_y) + 2
        for row in range(blank_rows):
            y = head_y - (blank_rows - row) * pitch_y + scroll_offset
            self._draw_punch_byte_row(
                painter, first_col_x, y, pitch_x, 0, col_map, sprocket_col,
                bit_radius, sprocket_radius, punched=False, ghost_bits=True, draw_sprocket=False,
            )

        visible = int((tape.bottom() - head_y) // pitch_y) + 3
        display_bytes = self._punch_display_bytes(visible)
        recent = display_bytes[-visible:]
        for i, byte in enumerate(reversed(recent)):
            y = head_y + i * pitch_y + scroll_offset
            self._draw_punch_byte_row(
                painter, first_col_x, y, pitch_x, byte, col_map, sprocket_col,
                bit_radius, sprocket_radius, punched=True, ghost_bits=True, draw_sprocket=False,
            )

        if self.punch.visual_active():
            pulse = 80 + (self.punch.visual_activity_ticks % 8) * 16
            head_glow = QColor(255, 235, 180, min(190, pulse))
            painter.setPen(QPen(QColor("#2c2721"), 2))
            painter.setBrush(head_glow)
            painter.drawRoundedRect(
                QRectF(tape.left() + 6, head_y - 5, tape.width() - 12, 10),
                4,
                4,
            )
        painter.restore()

    def _draw_punch_sprocket_track(
            self, painter: QPainter, sx_x: float, tape: QRectF,
            pitch_y: float, sprocket_radius: float, scroll_offset: float) -> None:
        row_count = int(tape.height() // pitch_y) + 4
        for row in range(row_count):
            y = tape.top() - pitch_y + row * pitch_y + scroll_offset
            if y < tape.top() - pitch_y or y > tape.bottom() + pitch_y:
                continue
            painter.setPen(QPen(QColor("#9f987f"), 1))
            painter.setBrush(QColor("#2f2c27"))
            painter.drawEllipse(QRectF(
                sx_x - sprocket_radius,
                y - sprocket_radius,
                sprocket_radius * 2,
                sprocket_radius * 2,
            ))

    def _punch_display_bytes(self, visible_rows: int) -> list[int]:
        if self.punch is None or not self.punch.punched_bytes:
            return []

        data = self.punch.punched_bytes
        if self.punch.visual_active():
            return data[-visible_rows:]

        first = 0
        while first < len(data) and data[first] == 0:
            first += 1

        last = len(data)
        while last > first and data[last - 1] == 0:
            last -= 1

        if first >= last:
            return data[-visible_rows:]

        trimmed = data[first:last]
        lead = [0] * min(4, first)
        tail = [0] * min(3, len(data) - last)
        return lead + trimmed + tail

    def _draw_punch_byte_row(
            self, painter: QPainter, first_x: float, y: float, pitch_x: float,
            byte: int, col_map: dict[int, int], sprocket_col: int,
            bit_radius: float, sprocket_radius: float, punched: bool, ghost_bits: bool,
            draw_sprocket: bool = True) -> None:
        sx_x = first_x + sprocket_col * pitch_x
        if draw_sprocket:
            painter.setPen(QPen(QColor("#9f987f"), 1))
            painter.setBrush(QColor("#2f2c27"))
            painter.drawEllipse(QRectF(sx_x - sprocket_radius, y - sprocket_radius,
                                       sprocket_radius * 2, sprocket_radius * 2))

        for bit in range(8):
            x = first_x + col_map[bit] * pitch_x
            if punched and ((byte >> bit) & 1):
                painter.setPen(QPen(QColor("#7d735f"), 1))
                painter.setBrush(QColor("#171411"))
                painter.drawEllipse(QRectF(x - bit_radius, y - bit_radius,
                                           bit_radius * 2, bit_radius * 2))
            elif ghost_bits:
                painter.setPen(QPen(QColor(150, 142, 116, 85), 1))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QRectF(x - bit_radius, y - bit_radius,
                                           bit_radius * 2, bit_radius * 2))

    def _draw_text(self, painter: QPainter, printable_rect: QRectF) -> None:
        painter.setFont(self.terminal_font)
        painter.setPen(QPen(QColor("#34312c"), 1))
        metrics = QFontMetrics(self.terminal_font)
        line_h = self._line_height()
        visible = self._visible_line_count()
        total = len(self._lines)
        end = total - self._scroll_offset_lines
        start = max(0, end - visible)
        visible_lines = self._lines[start:end]
        y = printable_rect.bottom() - (len(visible_lines) - 1) * line_h
        for line in visible_lines:
            painter.drawText(
                int(printable_rect.left()),
                int(y - metrics.descent()),
                line,
            )
            y += line_h
        if self._scroll_offset_lines:
            painter.setPen(QPen(QColor("#6d6250"), 1))
            painter.drawText(
                printable_rect.adjusted(0, 0, 0, 0),
                Qt.AlignRight | Qt.AlignTop,
                "HISTORY",
            )

    def _line_height(self) -> int:
        return max(18, int(QFontMetrics(self.terminal_font).height() * 1.25))

    def _visible_line_count(self) -> int:
        _, printable_rect, _ = self._layout_rects()
        return max(1, int(printable_rect.height() // self._line_height()))

    def _max_scroll_offset(self) -> int:
        return max(0, len(self._lines) - self._visible_line_count())

    def _scroll_by(self, delta_lines: int) -> None:
        self._scroll_offset_lines = max(
            0,
            min(self._scroll_offset_lines + delta_lines, self._max_scroll_offset()),
        )
        self.update()

    def _scroll_to_bottom(self) -> None:
        self._scroll_offset_lines = 0
        self.update()

    def _preserve_or_follow_bottom(self, old_count: int) -> None:
        if self._scroll_offset_lines:
            self._scroll_offset_lines += max(0, len(self._lines) - old_count)
        self._scroll_offset_lines = min(self._scroll_offset_lines, self._max_scroll_offset())


class ASR33QtFrontend(QMainWindow):
    """First Qt step: terminal display, SSH input, status controls, and sound."""

    display_signal = Signal()
    tu56_boot_signal = Signal()

    def __init__(self, terminal, backend, config, sound=None):
        self.app = QApplication.instance() or QApplication(sys.argv)
        super().__init__()

        self._term = terminal
        self._backend = backend
        self.cfg = config
        self._sounds = sound
        self._data_rate = self.cfg.data_throttle.config.get("mode", default="throttled")
        self._loopback_state = self.cfg.terminal.config.get("mode", default="line")
        self._printer_state = "off" if self.cfg.terminal.config.get("no_print", default=False) else "on"
        self._lid_state = self.cfg.get("sound", "config", "lid", default="up")
        self._sound_mute_state = self.cfg.get("sound", "config", "mute_state", default="unmuted")
        self.keyboard_uppercase_only = self.cfg.terminal.config.get(
            "keyboard_uppercase_only",
            default=KEYBOARD_UPPERCASE_ONLY,
        )
        self.keyboard_parity_mode = self.cfg.terminal.config.get(
            "keyboard_parity_mode",
            default=KEYBOARD_PARITY_MODE,
        )
        self.display_update_needed = True
        self.tape_running_state = False
        self._rk05_units = [
            {"unit": 0, "name": "RK0", "file": "os8/v3d.rk05", "attached": True, "readonly": False, "run": True, "fault": False, "load": False, "rd": False, "wt": False, "active": False, "status": ""},
            {"unit": 1, "name": "RK1", "file": "", "attached": False, "readonly": False, "run": False, "fault": False, "load": False, "rd": False, "wt": False, "active": False, "status": ""},
            {"unit": 2, "name": "RK2", "file": "", "attached": False, "readonly": False, "run": False, "fault": False, "load": False, "rd": False, "wt": False, "active": False, "status": ""},
            {"unit": 3, "name": "RK3", "file": "", "attached": False, "readonly": False, "run": False, "fault": False, "load": False, "rd": False, "wt": False, "active": False, "status": ""},
        ]
        self._tu56_units = [
            {"unit": i, "name": f"DT{i}", "file": "", "attached": False, "active": False}
            for i in range(8)
        ]
        self._rk05_activity_ticks = 0
        self._rk05_active_unit = 0
        self._rk05_show_capture = False
        self._rk05_show_buffer = ""
        self._rk05_detail_dialogs: dict[int, RK05DetailDialog] = {}
        self._tu56_show_capture = False
        self._tu56_show_buffer = ""
        self._tu56_boot_refresh_queued = False
        self._boot_detection_buffer = ""
        self._tu56_activity_ticks = 0
        self._tu56_active_unit = 0
        self._blink_ticks = 0

        self.setWindowTitle(f"ASR-33 Qt using {self._backend.get_info_string()}")

        self.paper_tape_reader = QtPaperTapeReader(
            backend=self._backend,
            config=self.cfg.tape_reader.config,
            ssh_config=self.cfg.backend.ssh_config,
        )
        self.paper_tape_punch = QtPaperTapePunch(
            backend=self._backend,
            config=self.cfg.tape_punch.config,
            ssh_config=self.cfg.backend.ssh_config,
        )
        terminal_columns = getattr(self._term, "width", 72)
        self.paper = TeletypeWidget(self, load_terminal_font(self.cfg), terminal_columns)
        self.paper.set_reader(self.paper_tape_reader)
        self.paper.set_punch(self.paper_tape_punch)
        self.rk_controller = SimhRKController(self)
        self.dt_controller = SimhDTController(self)
        self.rk05_panel = RK05PanelWidget(self)
        self.tu56_panels = [
            TU56PanelWidget(self, (0, 1)),
            TU56PanelWidget(self, (2, 3)),
            TU56PanelWidget(self, (4, 5)),
            TU56PanelWidget(self, (6, 7)),
        ]
        self.resize(self.paper.recommended_window_width(), 1130)
        self.status_label = QLabel("")
        self.throttle_button = QPushButton()
        self.mute_button = QPushButton()
        self.lid_button = QPushButton()
        self.loopback_button = QPushButton()
        self.printer_button = QPushButton()

        self.throttle_button.clicked.connect(self._toggle_throttle)
        self.mute_button.clicked.connect(self._toggle_mute)
        self.lid_button.clicked.connect(self._toggle_lid)
        self.loopback_button.clicked.connect(self._toggle_loopback)
        self.printer_button.clicked.connect(self._toggle_printer)

        controls = QHBoxLayout()
        controls.addWidget(self.status_label, 1)
        controls.addWidget(self.throttle_button)
        controls.addWidget(self.mute_button)
        controls.addWidget(self.lid_button)
        controls.addWidget(self.loopback_button)
        controls.addWidget(self.printer_button)

        tu56_grid_left = QVBoxLayout()
        tu56_grid_left.setContentsMargins(0, 0, 0, 0)
        tu56_grid_left.setSpacing(6)
        tu56_grid_left.addWidget(self.tu56_panels[0])
        tu56_grid_left.addWidget(self.tu56_panels[1])

        tu56_grid_right = QVBoxLayout()
        tu56_grid_right.setContentsMargins(0, 0, 0, 0)
        tu56_grid_right.setSpacing(6)
        tu56_grid_right.addWidget(self.tu56_panels[2])
        tu56_grid_right.addWidget(self.tu56_panels[3])

        peripherals = QHBoxLayout()
        peripherals.setContentsMargins(6, 6, 6, 6)
        peripherals.setSpacing(8)
        peripherals.addWidget(self.rk05_panel, 2)
        peripherals.addLayout(tu56_grid_left, 1)
        peripherals.addLayout(tu56_grid_right, 1)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.paper, 1)
        layout.addLayout(peripherals)
        layout.addLayout(controls)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.display_signal.connect(self._mark_display_dirty)
        self.tu56_boot_signal.connect(self._handle_tu56_boot_detected)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._periodic_tasks)
        self.timer.setInterval(20)

        self._refresh_buttons()

    def receive_data(self, data: bytes) -> None:
        """Handle incoming terminal data from the backend thread."""
        if data:
            text = data.decode("utf-8", errors="ignore")
            if self._rk05_show_capture:
                self._rk05_show_buffer += text
            if self._tu56_show_capture:
                self._tu56_show_buffer += text
            self._boot_detection_buffer = (self._boot_detection_buffer + text)[-500:]
            if self._detect_tu56_boot_text(self._boot_detection_buffer):
                self._boot_detection_buffer = ""
                self.tu56_boot_signal.emit()
            self._rk05_activity_ticks = 35
            if any(unit["attached"] for unit in self._tu56_units):
                self._tu56_activity_ticks = 35
        self.display_signal.emit()

    def run(self) -> None:
        """Run the Qt main loop."""
        self._sound_manage_lid()
        self._sound_manage_mute()
        self._manage_throttle()
        self._manage_loopback()
        self._manage_printer()

        if self._backend is not None and hasattr(self._backend, "start"):
            self._backend.start()
        if self._sounds is not None and hasattr(self._sounds, "start"):
            self._sounds.start()

        self.show()
        self.paper.setFocus(Qt.ActiveWindowFocusReason)
        self.timer.start()
        QTimer.singleShot(2500, self.refresh_rk05_state)
        QTimer.singleShot(3200, self.refresh_dt_state_from_simh)
        self.app.exec()

        self.timer.stop()
        self.paper_tape_punch.close_output()
        if self._sounds is not None and hasattr(self._sounds, "stop"):
            self._sounds.stop()
        if self._backend is not None and hasattr(self._backend, "close"):
            self._backend.close()

    def handle_key_event(self, event: QKeyEvent) -> bool:
        """Encode Qt key events as ASR-33 terminal bytes."""
        key = event.key()
        modifiers = event.modifiers()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._send_key_bytes(b"\r")
            return True
        if key in (Qt.Key_Backspace, Qt.Key_Delete):
            self._send_key_bytes(b"\x7f")
            return True
        if modifiers & Qt.ControlModifier:
            text = event.text()
            if text and text.isalpha():
                self._send_key_bytes(bytes([ord(text.upper()) - ord("@")]))
                return True
            if key == Qt.Key_BracketLeft:
                self._send_key_bytes(b"\x1b")
                return True

        text = event.text()
        if not text:
            return False
        if self.keyboard_uppercase_only:
            text = text.upper()
        try:
            byte = text.encode("ascii")
        except UnicodeEncodeError:
            return True
        if not byte:
            return True
        if self.keyboard_parity_mode == "even":
            byte = self._term.encode_even_parity(byte[:1])
        elif self.keyboard_parity_mode == "mark":
            byte = bytes([byte[0] | 0x80])
        elif self.keyboard_parity_mode == "space":
            byte = bytes([byte[0] & 0x7F])
        else:
            byte = byte[:1]
        self._send_key_bytes(byte)
        return True

    def _send_key_bytes(self, data: bytes) -> None:
        self._backend.send_data(data)
        if self._sounds is not None and hasattr(self._sounds, "keypress"):
            self._sounds.keypress()

    def rk05_attached(self) -> bool:
        return any(unit["attached"] for unit in self._rk05_units)

    def rk05_active(self) -> bool:
        return self._rk05_activity_ticks > 0

    def rk05_blink_on(self) -> bool:
        return (self._blink_ticks // 8) % 2 == 0

    def tu56_blink_on(self) -> bool:
        return (self._blink_ticks // 8) % 2 == 0

    def rk05_units(self) -> list[dict]:
        units = [dict(unit) for unit in self._rk05_units]
        for unit in units:
            unit["active"] = unit["unit"] == self._rk05_active_unit and self.rk05_active()
        return units

    def rk05_unit(self, unit_number: int) -> dict:
        for unit in self.rk05_units():
            if int(unit["unit"]) == unit_number:
                return unit
        return {"unit": unit_number, "name": f"RK{unit_number}", "file": "", "attached": False}

    def open_rk05_detail(self, unit_number: int) -> None:
        dialog = self._rk05_detail_dialogs.get(unit_number)
        if dialog is None:
            dialog = RK05DetailDialog(self, unit_number)
            self._rk05_detail_dialogs[unit_number] = dialog
        dialog.refresh()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def refresh_rk05_state(self) -> None:
        self.rk_controller.show()

    def select_rk05_pack(self, unit_number: int) -> None:
        if not self._rk05_units[unit_number].get("run"):
            QMessageBox.information(
                self,
                f"RK{unit_number} Power Off",
                "Set RUN to ON before loading a DECpack.",
            )
            return
        selected = self._choose_rk05_image(unit_number)
        if not selected:
            return
        if unit_number == 0 and self._rk05_units[0].get("attached"):
            if not self._confirm_rk0_risky_action("Changing RK0 may stop or crash the running OS/8 system."):
                return
        selected = self._resolve_remote_media_selection(selected, ".rk05")
        readonly = bool(self._rk05_units[unit_number].get("readonly"))
        self._set_rk05_load(unit_number, True, "attaching")
        self._attach_rk05_image(unit_number, selected, readonly=readonly)
        self._rk05_active_unit = unit_number
        for unit in self._rk05_units:
            if unit["unit"] == unit_number:
                unit["file"] = selected
                unit["attached"] = True
                unit["fault"] = False
                unit["status"] = "attached"
                break
        self._pulse_rk05_lamp(unit_number, "rd")
        if not readonly:
            self._pulse_rk05_lamp(unit_number, "wt")
        QTimer.singleShot(1400, lambda: self._set_rk05_load(unit_number, False, ""))
        QTimer.singleShot(1600, self.refresh_rk05_state)
        self._refresh_rk05_panel()

    def eject_rk05_pack(self, unit_number: int) -> None:
        if unit_number == 0:
            if not self._confirm_rk0_risky_action("RK0 is probably the OS/8 system disk. Detaching it may stop or crash the running system."):
                return
        self._set_rk05_load(unit_number, True, "detaching")
        self.rk_controller.detach(unit_number)
        for unit in self._rk05_units:
            if unit["unit"] == unit_number:
                unit["file"] = ""
                unit["attached"] = False
                unit["run"] = False
                unit["active"] = False
                unit["status"] = "detached"
                break
        if self._rk05_active_unit == unit_number:
            self._rk05_activity_ticks = 0
        QTimer.singleShot(1000, lambda: self._set_rk05_load(unit_number, False, ""))
        QTimer.singleShot(1200, self.refresh_rk05_state)
        self._refresh_rk05_panel()

    def toggle_rk05_run(self, unit_number: int) -> None:
        unit = self._rk05_units[unit_number]
        unit["run"] = not bool(unit.get("run"))
        unit["status"] = "run" if unit["run"] else "stopped"
        self._refresh_rk05_panel()

    def toggle_rk05_write_protect(self, unit_number: int) -> None:
        unit = self._rk05_units[unit_number]
        if unit_number == 0 and unit.get("attached"):
            if not self._confirm_rk0_risky_action("Changing RK0 write protect requires detach/attach and may disturb OS/8."):
                return
        unit["readonly"] = not bool(unit.get("readonly"))
        unit["status"] = "write protected" if unit["readonly"] else "write enabled"
        if unit.get("attached") and unit.get("file"):
            self._set_rk05_load(unit_number, True, "reattaching")
            self._attach_rk05_image(unit_number, unit["file"], readonly=unit["readonly"])
            self._pulse_rk05_lamp(unit_number, "rd")
            QTimer.singleShot(1400, lambda: self._set_rk05_load(unit_number, False, ""))
            QTimer.singleShot(1600, self.refresh_rk05_state)
        self._refresh_rk05_panel()

    def _confirm_rk0_risky_action(self, message: str) -> bool:
        answer = QMessageBox.warning(
            self,
            "RK0 System Disk",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def _set_rk05_load(self, unit_number: int, active: bool, status: str) -> None:
        unit = self._rk05_units[unit_number]
        unit["load"] = active
        if status:
            unit["status"] = status
        self._rk05_active_unit = unit_number
        self._rk05_activity_ticks = 35 if active else self._rk05_activity_ticks
        self._refresh_rk05_panel()

    def _pulse_rk05_lamp(self, unit_number: int, lamp: str) -> None:
        if lamp not in ("rd", "wt"):
            return
        self._rk05_units[unit_number][lamp] = True
        QTimer.singleShot(700, lambda: self._clear_rk05_lamp(unit_number, lamp))

    def _clear_rk05_lamp(self, unit_number: int, lamp: str) -> None:
        if 0 <= unit_number < len(self._rk05_units):
            self._rk05_units[unit_number][lamp] = False
            self._refresh_rk05_panel()

    def _begin_rk05_show_capture(self) -> None:
        self._rk05_show_buffer = ""
        self._rk05_show_capture = True

    def _finish_rk05_show_capture(self) -> None:
        self._rk05_show_capture = False
        parsed = SimhRKController.parse_show_rk(self._rk05_show_buffer)
        if not parsed:
            return
        for unit_number, state in parsed.items():
            unit = self._rk05_units[unit_number]
            unit["attached"] = state.get("attached", False)
            unit["file"] = state.get("file") or (unit.get("file") if unit["attached"] else "")
            unit["readonly"] = state.get("readonly", False)
            unit["fault"] = state.get("fault", False)
            if unit["attached"] and not unit.get("run"):
                unit["run"] = True
            if not unit["attached"]:
                unit["run"] = False
            unit["load"] = False
            unit["status"] = "refreshed"
        self._refresh_rk05_panel()

    def tu56_units(self) -> list[dict]:
        units = [dict(unit) for unit in self._tu56_units]
        for unit in units:
            unit["active"] = unit["unit"] == self._tu56_active_unit and self._tu56_activity_ticks > 0
        return units

    def refresh_dt_state_from_simh(self) -> None:
        self.dt_controller.show()

    def _detect_tu56_boot_text(self, text: str) -> bool:
        lower = text.lower()
        if "loading os/8 from dectape" not in lower and "dt0: 12b format" not in lower:
            return False
        return True

    def _handle_tu56_boot_detected(self) -> None:
        self._mark_tu56_unit_loaded(0, "tc08 system", active=True, status="tc08 system")
        if not self._tu56_boot_refresh_queued:
            self._tu56_boot_refresh_queued = True
            QTimer.singleShot(1800, self._refresh_dt_after_boot)

    def _refresh_dt_after_boot(self) -> None:
        self._tu56_boot_refresh_queued = False
        self.refresh_dt_state_from_simh()

    def _mark_tu56_unit_loaded(self, unit_number: int, filename: str, active: bool, status: str = "") -> None:
        unit = self._tu56_units[unit_number]
        unit["file"] = filename
        unit["attached"] = True
        unit["status"] = status
        self._tu56_active_unit = unit_number
        if active:
            self._tu56_activity_ticks = 60
        self._refresh_tu56_panels()

    def _begin_tu56_show_capture(self) -> None:
        self._tu56_show_buffer = ""
        self._tu56_show_capture = True

    def _finish_tu56_show_capture(self) -> None:
        self._tu56_show_capture = False
        parsed = SimhDTController.parse_show_dt(self._tu56_show_buffer)
        if not parsed:
            return
        for unit_number, state in parsed.items():
            unit = self._tu56_units[unit_number]
            if state.get("attached"):
                unit["attached"] = True
                unit["file"] = state.get("file") or unit.get("file") or "system DECtape"
                unit["status"] = state.get("status", "refreshed")
                if state.get("active"):
                    self._tu56_active_unit = unit_number
                    self._tu56_activity_ticks = 60
            else:
                unit["attached"] = False
                unit["file"] = ""
                unit["status"] = "not attached"
                if self._tu56_active_unit == unit_number:
                    self._tu56_activity_ticks = 0
        self._refresh_tu56_panels()

    def select_tu56_tape(self, unit_number: int) -> None:
        selected = self._choose_tu56_image(unit_number)
        if not selected:
            return
        self._attach_tu56_image(unit_number, selected)
        self._tu56_active_unit = unit_number
        for unit in self._tu56_units:
            if unit["unit"] == unit_number:
                unit["file"] = selected
                unit["attached"] = True
                break
        QTimer.singleShot(1200, self.refresh_dt_state_from_simh)
        self._refresh_tu56_panels()

    def eject_tu56_tape(self, unit_number: int) -> None:
        self._detach_simh_media("dt", unit_number)
        for unit in self._tu56_units:
            if unit["unit"] == unit_number:
                unit["file"] = ""
                unit["attached"] = False
                unit["active"] = False
                break
        if self._tu56_active_unit == unit_number:
            self._tu56_activity_ticks = 0
        QTimer.singleShot(900, self.refresh_dt_state_from_simh)
        self._refresh_tu56_panels()

    def _choose_rk05_image(self, unit_number: int) -> str | None:
        current = self._rk05_units[unit_number]["file"] or "/media/RK05/v3d.rk05"
        images = self._list_remote_rk05_images()
        if images:
            chooser = RK05PackChooser(self, f"RK{unit_number}", images)
            if chooser.exec() == QDialog.Accepted:
                return chooser.selected_image
            return None

        selected, ok = QInputDialog.getText(
            self,
            f"RK{unit_number} DECpack",
            "Remote RK05 image:",
            text=current,
        )
        selected = selected.strip() if selected else ""
        if not ok or not selected:
            return None
        if not selected.lower().endswith(".rk05"):
            selected += ".rk05"
        return selected

    def _choose_tu56_image(self, unit_number: int) -> str | None:
        current = self._tu56_units[unit_number]["file"] or f"/media/TU56/dt{unit_number}.tu56"
        images = self._list_remote_media_images(".tu56")
        if images:
            chooser = TU56TapeChooser(self, f"DT{unit_number}", images)
            if chooser.exec() == QDialog.Accepted:
                return chooser.selected_image
            return None

        selected, ok = QInputDialog.getText(
            self,
            f"DT{unit_number} TU56",
            "Remote TU56 image:",
            text=current,
        )
        selected = selected.strip() if selected else ""
        if not ok or not selected:
            return None
        if not selected.lower().endswith(".tu56"):
            selected += ".tu56"
        return selected

    def _list_remote_rk05_images(self) -> list[str]:
        return self._list_remote_media_images(".rk05")

    def _list_remote_media_images(self, extension: str) -> list[str]:
        ssh_config = self.cfg.backend.ssh_config
        host = ssh_config.get("host", default=None)
        username = ssh_config.get("username", default=None)
        if not host or not username:
            return []
        cached_password = None
        if self._backend is not None and hasattr(self._backend, "get_cached_ssh_password"):
            cached_password = self._backend.get_cached_ssh_password()
        password = ssh_config.get("password", default=None) or cached_password
        password_file = ssh_config.get("password_file", default=None)
        if not password and password_file:
            password = QtPaperTapePunch._read_password_file(password_file)
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                hostname=host,
                username=username,
                port=ssh_config.get("port", default=22),
                password=password,
                key_filename=ssh_config.get("key_filename", default=None),
                timeout=5,
                look_for_keys=ssh_config.get("look_for_keys", default=True),
                allow_agent=ssh_config.get("use_agent", default=True),
            )
            roots = self._remote_media_roots(extension)
            quoted_roots = " ".join(self._shell_quote(path) for path in roots)
            pattern = "*" + extension
            cmd = f"find {quoted_roots} -iname '{pattern}' 2>/dev/null | sort -u"
            _, stdout, _ = ssh.exec_command(cmd)
            images = [
                line.strip()
                for line in stdout.read().decode("utf-8", errors="ignore").splitlines()
                if line.strip().lower().endswith(extension)
            ]
            ssh.close()
            return images
        except Exception as e:
            print(f"Could not list remote {extension} images: {e}")
            return []

    @staticmethod
    def _remote_media_roots(extension: str) -> list[str]:
        if extension == ".rk05":
            return ["/media/RK05", "/opt/pidp8i/share/media/os8"]
        if extension == ".tu56":
            return ["/media/TU56", "/opt/pidp8i/share/media", "os8"]
        return ["/media", "os8", "/opt/pidp8i/share/media"]

    @staticmethod
    def _shell_quote(value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"

    def _attach_rk05_image(self, unit_number: int, image: str, readonly: bool = False) -> None:
        image = self._resolve_remote_media_selection(image, ".rk05")
        self.rk_controller.attach(unit_number, image, readonly=readonly)

    def _attach_tu56_image(self, unit_number: int, image: str) -> None:
        image = self._resolve_remote_media_selection(image, ".tu56")
        self._attach_simh_media("dt", unit_number, image)

    def _resolve_remote_media_selection(self, image: str, extension: str) -> str:
        image = image.strip()
        if not image:
            return image
        if image.startswith("/") or "/" in image:
            return image

        ssh_config = self.cfg.backend.ssh_config
        host = ssh_config.get("host", default=None)
        username = ssh_config.get("username", default=None)
        if not host or not username:
            return image

        cached_password = None
        if self._backend is not None and hasattr(self._backend, "get_cached_ssh_password"):
            cached_password = self._backend.get_cached_ssh_password()
        password = ssh_config.get("password", default=None) or cached_password
        password_file = ssh_config.get("password_file", default=None)
        if not password and password_file:
            password = QtPaperTapePunch._read_password_file(password_file)

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                hostname=host,
                username=username,
                port=ssh_config.get("port", default=22),
                password=password,
                key_filename=ssh_config.get("key_filename", default=None),
                timeout=5,
                look_for_keys=ssh_config.get("look_for_keys", default=True),
                allow_agent=ssh_config.get("use_agent", default=True),
            )
            quoted_name = self._shell_quote(image)
            for root in self._remote_media_roots(extension):
                candidate = f"{root.rstrip('/')}/{image}"
                cmd = f"test -f {self._shell_quote(candidate)} && printf %s {quoted_name}"
                _, stdout, _ = ssh.exec_command(cmd)
                if stdout.read().decode("utf-8", errors="ignore").strip() == image:
                    ssh.close()
                    return candidate
            ssh.close()
        except Exception as e:
            print(f"Could not resolve remote {extension} image {image}: {e}")
        return image

    def _attach_simh_media(self, device_prefix: str, unit_number: int, image: str) -> None:
        if self._backend is None or not hasattr(self._backend, "send_data"):
            return
        unit = f"{device_prefix}{unit_number}".encode("ascii")
        image_bytes = self._simh_quoted_path(image).encode("ascii", errors="ignore")
        self._backend.send_data(b"\x05")
        QTimer.singleShot(500, lambda: self._backend.send_data(b"detach " + unit + b"\r"))
        QTimer.singleShot(1000, lambda: self._backend.send_data(b"attach " + unit + b" " + image_bytes + b"\r"))
        QTimer.singleShot(1500, lambda: self._backend.send_data(b"cont\r"))

    def _detach_simh_media(self, device_prefix: str, unit_number: int) -> None:
        if self._backend is None or not hasattr(self._backend, "send_data"):
            return
        unit = f"{device_prefix}{unit_number}".encode("ascii")
        self._backend.send_data(b"\x05")
        QTimer.singleShot(500, lambda: self._backend.send_data(b"detach " + unit + b"\r"))
        QTimer.singleShot(1000, lambda: self._backend.send_data(b"cont\r"))

    def _refresh_tu56_panels(self) -> None:
        for panel in getattr(self, "tu56_panels", []):
            panel.update()

    def _refresh_rk05_panel(self) -> None:
        if hasattr(self, "rk05_panel"):
            self.rk05_panel.update()
        for dialog in getattr(self, "_rk05_detail_dialogs", {}).values():
            dialog.refresh()

    @staticmethod
    def _simh_quoted_path(path: str) -> str:
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _mark_display_dirty(self) -> None:
        self.display_update_needed = True

    def _periodic_tasks(self) -> None:
        self._blink_ticks += 1
        if self._rk05_activity_ticks > 0:
            self._rk05_activity_ticks -= 1
            self._refresh_rk05_panel()
        if self._tu56_activity_ticks > 0:
            self._tu56_activity_ticks -= 1
            self._refresh_tu56_panels()
        self.paper_tape_reader.process()
        if self.paper_tape_reader.active_status():
            self.paper.update()
        self.paper_tape_punch.advance_visual_feed()
        if self.paper_tape_punch.active or self.paper_tape_punch.visual_active():
            self.paper.update()
        if self.paper_tape_punch.poll_ptp_activity():
            self.paper.update()

        if self.display_update_needed:
            self.display_update_needed = False
            self._update_display()

        while self._term.sound_queue_len() > 0:
            item = self._term.pop_char_from_sound_queue()
            if item is None:
                break
            ch, col = item
            if self._sounds is not None and hasattr(self._sounds, "print_char"):
                self._sounds.print_char(ch)
            if col == 62 and self._sounds is not None and hasattr(self._sounds, "column_bell"):
                self._sounds.column_bell()
        if self._sounds is not None and hasattr(self._sounds, "tape_reader_running"):
            tape_running_status = (
                self.paper_tape_reader.active_status() or
                self.paper_tape_punch.visual_active()
            )
            if self.tape_running_state != tape_running_status:
                self.tape_running_state = tape_running_status
                self._sounds.tape_reader_running(tape_running_status)

    def _update_display(self) -> None:
        lines = []
        history_len = len(self._term.line_history)
        for row in range(history_len):
            lines.append(repr(self._term.line_history.get_line(row)).rstrip())
        self.paper.set_lines(lines)

    def _refresh_buttons(self) -> None:
        self.status_label.setText(
            f"LINE: {self._loopback_state.upper()}  "
            f"RATE: {self._data_rate.upper()}  "
            f"PRINTER: {self._printer_state.upper()}"
        )
        self.throttle_button.setText("Unthrottle" if self._data_rate == "throttled" else "Throttle")
        self.mute_button.setText("Mute" if self._sound_mute_state == "unmuted" else "Unmute")
        self.lid_button.setText("Lower Lid" if self._lid_state == "up" else "Raise Lid")
        self.loopback_button.setText("Local" if self._loopback_state == "line" else "Line")
        self.printer_button.setText("Printer Off" if self._printer_state == "on" else "Printer On")

    def _toggle_throttle(self) -> None:
        self._data_rate = "unthrottled" if self._data_rate == "throttled" else "throttled"
        self._manage_throttle()
        self._refresh_buttons()

    def _toggle_mute(self) -> None:
        self._sound_mute_state = "muted" if self._sound_mute_state == "unmuted" else "unmuted"
        self._sound_manage_mute()
        self._refresh_buttons()

    def _toggle_lid(self) -> None:
        self._lid_state = "down" if self._lid_state == "up" else "up"
        self._sound_manage_lid()
        self._refresh_buttons()

    def _toggle_loopback(self) -> None:
        self._loopback_state = "local" if self._loopback_state == "line" else "line"
        self._manage_loopback()
        self._refresh_buttons()

    def _toggle_printer(self) -> None:
        self._printer_state = "off" if self._printer_state == "on" else "on"
        self._manage_printer()
        self._refresh_buttons()

    def load_reader_tape(self) -> None:
        if self.paper_tape_reader.state != "FREE":
            QMessageBox.information(
                self,
                "Paper Tape Reader Locked",
                "Set the paper tape reader lever to FREE before loading or changing a tape.",
            )
            self.paper.update()
            return
        if self.paper_tape_reader.load_tape(self):
            self.paper.update()

    def reader_start(self) -> None:
        self.paper_tape_reader.start()
        self.paper.update()

    def reader_stop(self) -> None:
        self.paper_tape_reader.stop()
        self.paper.update()

    def reader_free(self) -> None:
        self.paper_tape_reader.free()
        self.paper.update()

    def reader_unload(self) -> None:
        if self.paper_tape_reader.state != "FREE":
            return
        self.paper_tape_reader.unload()
        self.paper.update()

    def punch_select_output(self) -> None:
        if self.paper_tape_punch.select_output(self):
            self.paper.update()

    def punch_on(self) -> None:
        if self.paper_tape_punch.start():
            self.paper.update()

    def punch_off(self) -> None:
        self.paper_tape_punch.stop()
        self.paper.update()

    def _sound_manage_lid(self) -> None:
        if self._sounds is not None and hasattr(self._sounds, "lid"):
            self._sounds.lid(set_lid_to_up=self._lid_state == "up")

    def _sound_manage_mute(self) -> None:
        if self._sounds is not None and hasattr(self._sounds, "mute"):
            self._sounds.mute(self._sound_mute_state == "muted")

    def _manage_throttle(self) -> None:
        if self._data_rate == "throttled":
            self._backend.enable_throttling()
        else:
            self._backend.disable_throttling()

    def _manage_loopback(self) -> None:
        if self._loopback_state == "local":
            self._backend.enable_loopback()
        else:
            self._backend.disable_loopback()

    def _manage_printer(self) -> None:
        if self._printer_state == "on":
            self._term.enable_printing()
        else:
            self._term.disable_printing()
