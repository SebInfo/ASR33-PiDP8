#!/usr/bin/env python3

"""Simulated papertape reader and punch front-end components
   for ASR-33 terminal emulator.
"""

import os
import threading
import time
import paramiko
import tkinter as tk
from typing import BinaryIO
from tkinter import filedialog, simpledialog
from asr33_pt_animate_tk import PapertapeViewer

# Default configuration constants

# if True, the tape reader will automatically skip past all leading
# 000 bytes at the start of the tape when the reader is turned on.
READER_AUTO_SKIP_LEADING_NULLS = True

# if True, sets msb to 1 on all bytes read from tape. This is useful
# If you created an ASCII tape file with an editor on a modern system
# for use on systems that expect 7-bit ASCII with mark parity like some
# versions of OS/8. This is also useful when creating FOCAL-69 source tapes.
READER_SET_MSB = False

class HexViewer:
    "Hex dumper front-end component (streaming one byte at a time)"
    def __init__(self):
        self.offset = 0

    def dump_byte(self, byte_data: bytes):
        "Dump byte to the console in hex format, 16 per line"

        for byte in byte_data:
            # Print offset at the start of each line
            if self.offset % 16 == 0:
                print(f'{self.offset:08X}  ', end='')

            # Print the byte in hex format
            print(f'{byte:02X} ', end='')

            self.offset += 1

            # End the line after 16 bytes
            if self.offset % 16 == 0:
                print()

PAPER_TAPE_VIEWER_SCALE = 150
MAX_VIEWER_ROWS = 1024
PAPER_TAPE_EXTENSIONS = (
    ".pt", ".pb", ".pa", ".pr", ".bpt", ".apt", ".rpt", ".tap", ".rim", ".bin", ".bn"
)

class PapertapeReader():
    "Papertape reader front-end component"

    def __init__(self, master, backend, config, ssh_config=None):
        self.master = master
        self.backend = backend
        self.tape_loaded = False
        self.init_window_pos = True
        self.active = False  # initially stopped
        self.stop_cause = ""
        self.pt_name_path = None
        self.init_name_path = config.get("initial_file_path", default=None)

        # Remote Raspberry paper tape reader support.
        # If enabled, Load reads from the configured remote tape directory
        # instead of opening a local file dialog.
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

        self.tape_data = b''
        self.position = 0
        self.trailing_o000_idx = None
        self.trailing_o200_idx = None
        self.skip_leading_nulls = config.get(
            "skip_leading_nulls",
            default=READER_AUTO_SKIP_LEADING_NULLS
        )
        self.set_msb = config.get(
            "set_msb",
            default=READER_SET_MSB
        )
        self.parent_x = 200
        self.parent_y = 500
        self.parent_h = 600

        self.papertape_viewer = PapertapeViewer(
            outer=self,
            master=self.master,
            mode="reader",
            config=config,
            window_title="Papertape Reader",
            scale=PAPER_TAPE_VIEWER_SCALE,
            max_rows=config.get("max_rows", default=MAX_VIEWER_ROWS),
            x_org=100,
            y_org=100,
            height=100
        )

        if self.papertape_viewer is None:
            raise RuntimeError("Could not create papertape reader viewer")

        # Threading attributes
        self.thread_running = False
        self._thread = threading.Thread(target=self._tape_reader_worker, daemon=True)
        self._start_thread()

    @staticmethod
    def _read_password_file(path: str | None) -> str | None:
        """Read a local password file, returning None if it is absent or empty."""
        if not path:
            return None
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
                password = f.read().strip()
        except OSError:
            return None
        return password or None

    def _select_remote_tape_name(self, tape_names: list[str]) -> str | None:
        """Ask the operator which compatible remote paper tape to mount."""
        if not tape_names or self.papertape_viewer is None:
            return None

        dialog = tk.Toplevel(self.papertape_viewer)
        dialog.title("Load Remote Paper Tape")
        dialog.transient(self.papertape_viewer)
        dialog.grab_set()
        dialog.resizable(False, False)

        selected_name = {"value": None}

        frame = tk.Frame(dialog, padx=10, pady=10)
        frame.pack(fill="both", expand=True)

        listbox = tk.Listbox(frame, width=48, height=min(max(len(tape_names), 6), 18))
        scrollbar = tk.Scrollbar(frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.grid(row=0, column=0, columnspan=2, sticky="nsew")
        scrollbar.grid(row=0, column=2, sticky="ns")

        for name in tape_names:
            listbox.insert(tk.END, name)
        listbox.selection_set(0)
        listbox.activate(0)

        def accept(_event=None):
            selection = listbox.curselection()
            if selection:
                selected_name["value"] = tape_names[selection[0]]
            dialog.destroy()

        def cancel(_event=None):
            selected_name["value"] = None
            dialog.destroy()

        load_button = tk.Button(frame, text="Load", width=10, command=accept)
        cancel_button = tk.Button(frame, text="Cancel", width=10, command=cancel)
        load_button.grid(row=1, column=0, sticky="e", padx=(0, 6), pady=(10, 0))
        cancel_button.grid(row=1, column=1, sticky="w", pady=(10, 0))

        listbox.bind("<Double-Button-1>", accept)
        listbox.bind("<Return>", accept)
        dialog.bind("<Escape>", cancel)
        dialog.protocol("WM_DELETE_WINDOW", cancel)

        dialog.update_idletasks()
        parent_x = self.papertape_viewer.winfo_rootx()
        parent_y = self.papertape_viewer.winfo_rooty()
        parent_w = self.papertape_viewer.winfo_width()
        parent_h = self.papertape_viewer.winfo_height()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = parent_x + max((parent_w - width) // 2, 0)
        y = parent_y + max((parent_h - height) // 2, 0)
        dialog.geometry(f"+{x}+{y}")

        listbox.focus_set()
        dialog.wait_window()
        return selected_name["value"]

    def _start_thread(self) -> None:
        """Start the papertape reader worker thread."""
        if not self.thread_running:
            self.thread_running = True
            self._thread.start()

    def _stop_thread(self) -> None:
        """Stop the papertape reader worker thread."""
        self.thread_running = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def close_viewer_event(self) -> None:
        "Close the papertape reader"
        self.unload_tape()
        if self.papertape_viewer is not None:
            self.papertape_viewer.withdraw()
        if self.master is not None:
            self.master.after_idle(self.master.focus_force)

    def _end_check(self, position: int) -> bool:
        if position >= len(self.tape_data):
            self.stop_cause = "end_of_tape"
            return True

        # If there is no viewer or autostop is disabled, do not auto-stop.
        if self.papertape_viewer is None or not getattr(self.papertape_viewer, "autostop", False):
            self.stop_cause = ""
            return False

        # Check for autostop trailing o200 and o000 bytes if enabled
        if self.trailing_o200_idx is not None:
            if position > self.trailing_o200_idx:
                self.stop_cause = "trailing_o200"
                return True
            return False

        if self.trailing_o000_idx is not None:
            if position > self.trailing_o000_idx:
                self.stop_cause = "trailing_o000"
                return True
        return False

    def _tape_reader_worker(self) -> None:
        "Perform papertape reading background tasks"
        while self.thread_running:
            sleep_time = 0.050  # 50mS default sleep time
            if self.active:
                if self._end_check(self.position):
                    self.active = False
                    if self.papertape_viewer is not None:
                        self.papertape_viewer.set_to_off_state()
                    continue

                data_byte = bytes(self.tape_data[self.position:self.position+1])
                if self.set_msb:
                    data_byte = bytes([data_byte[0] | 0x80])  # set msb

                # Send 8-bit byte directly to backend for transmission
                if self.backend is not None:
                    self.backend.send_data(data_byte)

                if self.papertape_viewer is not None:
                    self.papertape_viewer.add_byte(data_byte)

                self.position += 1
                sleep_time = 0.003 # Faster reading speed while data is available

            time.sleep(sleep_time)

    def _load_tapefile(self, name_path: str) -> None:
        with open(name_path, 'br') as f:
            # if a tape is already loaded, remove it
            if self.tape_loaded:
                self.unload_tape()

            # Read the new tape file
            self.tape_data = f.read()

            # Locate first 0o200 and 0o000 trailer bytes at end of file data
            n = len(self.tape_data)
            i = n - 1
            # count consecutive trailing 0o000 bytes
            while i >= 0 and self.tape_data[i] == 0o000:
                i -= 1
            self.trailing_o000_idx = i + 1 if i < n - 1 else None

            # Then count consecutive 0o200 immediately before that
            j = i
            while j >= 0 and self.tape_data[j] == 0o200:
                j -= 1
            self.trailing_o200_idx = j + 1 if j < i else None

            self.position = 0
            self.tape_loaded = True
            self.active = False  # initially stopped

            # Preview mode: display the full tape immediately when loaded,
            # without sending it to the PDP-8/backend.
            if self.papertape_viewer is not None:
                self.papertape_viewer.unload_tape()
                self.papertape_viewer.add_byte(self.tape_data[::-1])
                self.papertape_viewer.process_viewer(True)

    def stop(self) -> None:
        "Shutdown the papertape reader viewer"
        self.unload_tape()
        if self.papertape_viewer is not None:
            self.papertape_viewer.close()
            self.papertape_viewer = None
        self._stop_thread()

    def process(self) -> None:
        "Process viewer's enqueued data."
        if self.papertape_viewer is not None:
            self.papertape_viewer.process_viewer(self.tape_loaded)
        self._update_file_status()

    def active_status(self) -> bool:
        "Return true if papertape reader is active"
        return self.active

    def show(self, parent_x=100, parent_y=100, parent_h=500) -> None:
        "Show the papertape reader viewer"
        if self.papertape_viewer is None:
            return

        self.parent_x = parent_x
        self.parent_y = parent_y
        self.parent_h = parent_h

        if self.init_window_pos:
            # Set initial window position
            xoffset = 10  # pixels gap
            child_w = self.papertape_viewer.winfo_width()  # keep current width
            child_h = self.parent_h // 2 # half the height of parent
            # Position at the bottom left of parent window
            child_x = self.parent_x - child_w - xoffset
            child_x = max(10, child_x)
            child_y = self.parent_y
            self.papertape_viewer.geometry(f"{child_w}x{child_h}+{child_x}+{child_y}")
            self.init_window_pos = False
            self.papertape_viewer.transient(self.master)

        self.papertape_viewer.bring_to_front()
        self._update_file_status()

    def hide(self) -> None:
        "Hide the papertape reader viewer"
        if self.papertape_viewer is None:
            return
        self.papertape_viewer.withdraw()
        if self.master is not None:
            self.master.after_idle(self.master.focus_force)

    def on(self) -> bool:
        "Turn on the papertape reader"
        if self.papertape_viewer is None:
            return False
        if not self.tape_loaded:
            return False

        # Auto-skip leading nulls if enabled
        if self.skip_leading_nulls:
            n = len(self.tape_data)
            while self.position < n and self.tape_data[self.position] == 0o000:
                self.position += 1

        self.active = True
        self.stop_cause = ""
        return True

    def off(self) -> bool:
        "Turn off the papertape reader"
        if self.papertape_viewer is None:
            return False
        self.active = False
        return True

    def rewind_tape(self) -> None:
        "Rewind the papertape to the beginning"
        if self.tape_loaded and not self.active and self.position>0:
            self.position = 0  # Reset tape read position to the beginning.
            if self.papertape_viewer is not None:
                self.papertape_viewer.unload_tape()

    def _load_remote_tapefile(self) -> str:
        """Load the first paper tape file from the Raspberry over SSH/SFTP.

        The remote directory must be a mounted filesystem. This keeps the
        behavior close to the physical idea: no mounted medium, no tape.
        """
        if self.papertape_viewer is None:
            return "error"

        ssh = None
        sftp = None

        try:
            if not self.remote_host or not self.remote_username or not self.remote_dir:
                print("Remote paper tape is enabled but host, username, or dir is not configured")
                return "error"

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cached_password = None
            if self.backend is not None and hasattr(self.backend, "get_cached_ssh_password"):
                cached_password = self.backend.get_cached_ssh_password()
            file_password = self._read_password_file(self.remote_password_file)

            connect_kwargs = {
                "hostname": self.remote_host,
                "username": self.remote_username,
                "port": self.remote_port,
                "password": self.remote_password or cached_password or file_password,
                "key_filename": self.remote_key_filename,
                "timeout": 8,
                "look_for_keys": self.remote_look_for_keys,
                "allow_agent": self.remote_use_agent,
            }
            try:
                ssh.connect(**connect_kwargs)
            except paramiko.AuthenticationException:
                print(
                    "Could not authenticate remote paper tape connection. "
                    "Wait until the main SSH terminal is authenticated, or configure SSH keys."
                )
                return "error"

            # Only load if the Raspberry really has the tape medium mounted.
            cmd = f"mountpoint -q {self.remote_dir}; echo $?"
            _, stdout, _ = ssh.exec_command(cmd)
            mounted = stdout.read().decode("ascii", errors="ignore").strip()

            if mounted != "0":
                print(f"Remote paper tape directory is not mounted: {self.remote_dir}")
                return "error"

            sftp = ssh.open_sftp()
            names = sftp.listdir(self.remote_dir)

            tape_names = [
                n for n in names
                if not n.startswith("._")
                and n.lower().endswith(PAPER_TAPE_EXTENSIONS)
            ]

            if not tape_names:
                print(f"No paper tape file found in {self.remote_dir}")
                return "error"

            tape_names.sort()
            selected_name = self._select_remote_tape_name(tape_names)
            if selected_name is None:
                return "cancelled"
            remote_path = self.remote_dir.rstrip("/") + "/" + selected_name

            if self.tape_loaded:
                self.unload_tape()

            with sftp.open(remote_path, "rb") as f:
                self.tape_data = f.read()

            # Locate first 0o200 and 0o000 trailer bytes at end of file data
            n = len(self.tape_data)
            i = n - 1
            while i >= 0 and self.tape_data[i] == 0o000:
                i -= 1
            self.trailing_o000_idx = i + 1 if i < n - 1 else None

            j = i
            while j >= 0 and self.tape_data[j] == 0o200:
                j -= 1
            self.trailing_o200_idx = j + 1 if j < i else None

            self.position = 0
            self.tape_loaded = True
            self.active = False

            # Preview mode: display the full remote tape immediately when loaded,
            # without sending it to the PDP-8/backend.
            if self.papertape_viewer is not None:
                self.papertape_viewer.unload_tape()
                self.papertape_viewer.add_byte(self.tape_data[::-1])
                self.papertape_viewer.process_viewer(True)

            self.pt_name_path = remote_path
            self.init_name_path = remote_path
            self._update_file_status()

            print(f"Remote paper tape loaded: {remote_path} ({len(self.tape_data)} bytes)")
            return "loaded"

        except Exception as e:
            print(f"Could not load remote paper tape: {e}")
            return "error"

        finally:
            try:
                if sftp is not None:
                    sftp.close()
            finally:
                if ssh is not None:
                    ssh.close()

    def load_tape(self) -> str:
        "Load a tape file"
        if self.papertape_viewer is None:
            return "error"

        if self.active:
            self.stop()  # if active, turn off reader
        saved_active = self.active

        if self.remote_enabled:
            result = self._load_remote_tapefile()
            if result == "loaded":
                return result
            self.active = saved_active
            return result

        initial_dir = os.path.dirname(self.init_name_path) if self.init_name_path else "."
        name_path = get_reader_file_selection(self.papertape_viewer, initial_dir=initial_dir)

        if name_path is None:
            self.active = saved_active
            return "cancelled"
        try:
            self._load_tapefile(name_path)
        except (FileNotFoundError, PermissionError, OSError) as e:
            print(f"Could not load tape file: {e}")
            self.active = saved_active
            return "error"
        self.pt_name_path = name_path
        self.init_name_path = name_path
        self._update_file_status()
        return "loaded"

    def unload_tape(self) -> bool:
        "Remove tape from reader"
        if self.papertape_viewer is None:
            return False
        self.off()  #ensure reader is stopped
        if self.tape_loaded:
            self.tape_loaded = False
            self.pt_name_path = None
        self.papertape_viewer.unload_tape()
        self.tape_data = b''
        self.position = 0
        self._update_file_status()
        return True

    def _update_file_status(self) -> None:
        """Update the file status in the papertape viewer."""
        if self.papertape_viewer is None:
            return

        if self.pt_name_path is None:
            fileinfo = "Unloaded"
            status = ""
        else:
            filename = os.path.basename(self.pt_name_path)
            filelen = len(self.tape_data)
            fileinfo = f"{filename} ({filelen} bytes)"
            percent = ((self.position / filelen) * 100) if filelen > 0 else 0
            if self.active:
                status = f"{percent:.1f}% read"
            elif self.stop_cause == "trailing_o200":
                status = "Stopped: auto-stop (200)"
            elif self.stop_cause == "trailing_o000":
                status = "Stopped: auto-stop (null)"
            elif self.stop_cause == "end_of_tape":
                status = f"{percent:.1f}% read (end)"
            else:
                status = f"{percent:.1f}% read"

        self.papertape_viewer.set_file_status(
            f"File: {fileinfo}", f"{status}")


class PapertapePunch():
    "Papertape punch front-end component"

    def __init__(self, master, config, backend=None, ssh_config=None):
        self.master = master
        self.backend = backend
        self.tape_loaded = False
        self.init_window_pos = True
        self.active = False  # initially stopped
        self.tape_file = None
        self.remote_ssh = None
        self.remote_sftp = None
        self.pt_name_path = None
        self.ptp_attached = False
        self.init_name_path = config.get("initial_file_path", default=None)
        self.file_write_mode = config.get("mode", default="overwrite")
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
        self.parent_x = 200
        self.parent_y = 200
        self.parent_h = 600

        self.papertape_viewer = PapertapeViewer(
            outer=self,
            master=self.master,
            mode="punch",
            config=config,
            window_title="Papertape Punch",
            scale=PAPER_TAPE_VIEWER_SCALE,
            max_rows=config.get("max_rows", default=MAX_VIEWER_ROWS),
            x_org=100,
            y_org=100,
            height=100
        )

        if self.papertape_viewer is None:
            raise RuntimeError("Could not create papertape reader viewer")

    @staticmethod
    def _read_password_file(path: str | None) -> str | None:
        """Read a local password file, returning None if it is absent or empty."""
        if not path:
            return None
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
                password = f.read().strip()
        except OSError:
            return None
        return password or None

    def _connect_remote(self):
        """Open an SSH/SFTP connection for punching remote tapes."""
        if not self.remote_host or not self.remote_username or not self.remote_dir:
            print("Remote paper tape punch is enabled but host, username, or dir is not configured")
            return None, None

        cached_password = None
        if self.backend is not None and hasattr(self.backend, "get_cached_ssh_password"):
            cached_password = self.backend.get_cached_ssh_password()
        file_password = self._read_password_file(self.remote_password_file)

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {
            "hostname": self.remote_host,
            "username": self.remote_username,
            "port": self.remote_port,
            "password": self.remote_password or cached_password or file_password,
            "key_filename": self.remote_key_filename,
            "timeout": 8,
            "look_for_keys": self.remote_look_for_keys,
            "allow_agent": self.remote_use_agent,
        }
        try:
            ssh.connect(**connect_kwargs)
        except paramiko.AuthenticationException:
            print(
                "Could not authenticate remote paper tape punch connection. "
                "Wait until the main SSH terminal is authenticated, or configure SSH keys."
            )
            ssh.close()
            return None, None

        cmd = f"mountpoint -q {self.remote_dir}; echo $?"
        _, stdout, _ = ssh.exec_command(cmd)
        mounted = stdout.read().decode("ascii", errors="ignore").strip()
        if mounted != "0":
            print(f"Remote paper tape punch directory is not mounted: {self.remote_dir}")
            ssh.close()
            return None, None

        return ssh, ssh.open_sftp()

    def _select_remote_punch_name(self, existing_names: list[str]) -> str | None:
        """Ask for a compatible remote paper tape filename to punch."""
        if self.papertape_viewer is None:
            return None

        initial = ""
        if self.init_name_path:
            initial = os.path.basename(self.init_name_path)
        if not initial:
            initial = "punch.pt"

        if self.file_write_mode == "append" and existing_names:
            selected = self._select_remote_tape_name(existing_names)
            if selected:
                return selected

        name = simpledialog.askstring(
            "Paper Tape Punch",
            "Remote paper tape filename:",
            initialvalue=initial,
            parent=self.papertape_viewer,
        )
        if not name:
            return None
        name = os.path.basename(name.strip())
        if not name:
            return None
        if not name.lower().endswith(PAPER_TAPE_EXTENSIONS):
            name += ".pt"
        return name

    @staticmethod
    def _normalize_ptp_attach_name(name: str) -> str | None:
        """Return a safe host filename for SIMH PTP output."""
        name = os.path.basename(name.strip())
        if not name:
            return None
        if "." not in name:
            name += ".pt"
        return name

    def attach_ptp(self) -> bool:
        """Attach SIMH PTP to a remote file and continue the PDP-8."""
        if self.papertape_viewer is None:
            return False
        if self.backend is None or not hasattr(self.backend, "send_data"):
            return False

        initial = ""
        if self.init_name_path:
            initial = os.path.basename(self.init_name_path)
        if not initial:
            initial = "punch.pt"

        name = simpledialog.askstring(
            "SIMH PTP Attach",
            "PTP output filename:",
            initialvalue=initial,
            parent=self.papertape_viewer,
        )
        if not name:
            return False

        name = self._normalize_ptp_attach_name(name)
        if name is None:
            return False

        remote_dir = self.remote_dir or "."
        remote_path = remote_dir.rstrip("/") + "/" + name

        # PTP is a SIMH/OS-8 device path, not the ASR-33 raw punch.
        # Ensure the raw punch capture is off so terminal output does not
        # contaminate the PTP output file.
        self.off()
        if self.tape_file is not None:
            self.tape_file.close()
            self.tape_file = None
        if self.remote_sftp is not None:
            self.remote_sftp.close()
            self.remote_sftp = None
        if self.remote_ssh is not None:
            self.remote_ssh.close()
            self.remote_ssh = None
        self.tape_loaded = False
        if self.papertape_viewer is not None:
            self.papertape_viewer.unload_tape()

        self.backend.send_data(b"\x05")

        def send_detach():
            self.backend.send_data(b"detach ptp\r")

        def send_attach():
            self.backend.send_data(f"attach -n ptp {remote_path}\r".encode("ascii", "ignore"))

        def send_continue():
            self.backend.send_data(b"cont\r")

        if self.master is not None and hasattr(self.master, "after"):
            self.master.after(500, send_detach)
            self.master.after(1000, send_attach)
            self.master.after(1500, send_continue)
        else:
            send_detach()
            send_attach()
            send_continue()

        self.pt_name_path = remote_path
        self.init_name_path = remote_path
        self.ptp_attached = True
        self.active = False
        self._update_file_status()
        return True

    def detach_ptp(self) -> None:
        """Detach SIMH PTP and continue the PDP-8."""
        if self.backend is None or not hasattr(self.backend, "send_data"):
            return
        self.backend.send_data(b"\x05")

        def send_detach():
            self.backend.send_data(b"detach ptp\r")

        def send_continue():
            self.backend.send_data(b"cont\r")

        if self.master is not None and hasattr(self.master, "after"):
            self.master.after(500, send_detach)
            self.master.after(1000, send_continue)
        else:
            send_detach()
            send_continue()

    def _select_remote_tape_name(self, tape_names: list[str]) -> str | None:
        """Ask which compatible remote paper tape to append to."""
        if not tape_names or self.papertape_viewer is None:
            return None

        dialog = tk.Toplevel(self.papertape_viewer)
        dialog.title("Append Remote Paper Tape")
        dialog.transient(self.papertape_viewer)
        dialog.grab_set()
        dialog.resizable(False, False)

        selected_name = {"value": None}
        frame = tk.Frame(dialog, padx=10, pady=10)
        frame.pack(fill="both", expand=True)
        listbox = tk.Listbox(frame, width=48, height=min(max(len(tape_names), 6), 18))
        scrollbar = tk.Scrollbar(frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.grid(row=0, column=0, columnspan=2, sticky="nsew")
        scrollbar.grid(row=0, column=2, sticky="ns")
        for name in tape_names:
            listbox.insert(tk.END, name)
        listbox.selection_set(0)
        listbox.activate(0)

        def accept(_event=None):
            selection = listbox.curselection()
            if selection:
                selected_name["value"] = tape_names[selection[0]]
            dialog.destroy()

        def cancel(_event=None):
            selected_name["value"] = None
            dialog.destroy()

        tk.Button(frame, text="Append", width=10, command=accept).grid(
            row=1, column=0, sticky="e", padx=(0, 6), pady=(10, 0)
        )
        tk.Button(frame, text="Cancel", width=10, command=cancel).grid(
            row=1, column=1, sticky="w", pady=(10, 0)
        )
        listbox.bind("<Double-Button-1>", accept)
        listbox.bind("<Return>", accept)
        dialog.bind("<Escape>", cancel)
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.update_idletasks()
        dialog.geometry(f"+{self.papertape_viewer.winfo_rootx()}+{self.papertape_viewer.winfo_rooty()}")
        listbox.focus_set()
        dialog.wait_window()
        return selected_name["value"]

    def close_viewer_event(self) -> None:
        "Close the papertape reader"    
        self.unload_tape()
        if self.papertape_viewer is not None:
            self.papertape_viewer.withdraw()
        if self.master is not None:
            self.master.after_idle(self.master.focus_force)

    def process(self):
        "Process viewer's enqueued data and update the display."
        if self.papertape_viewer is not None:
            self.papertape_viewer.process_viewer(self.tape_loaded)
        self._update_file_status()

    def stop(self) -> None:
        "Shutdown the papertape punch viewer"
        self.unload_tape()
        if self.papertape_viewer is not None:
            self.papertape_viewer.close()
            self.papertape_viewer = None

    def show(self, parent_x=100, parent_y=100, parent_h=500) -> None:
        "Show the papertape reader viewer"
        if self.papertape_viewer is None:
            return

        self.parent_x = parent_x
        self.parent_y = parent_y
        self.parent_h = parent_h

        if self.init_window_pos:
            # Set initial window position
            xoffset = 10  # pixels gap
            child_w = self.papertape_viewer.winfo_width()  # keep current width
            child_h = self.parent_h // 2 # half the height of parent
            # Position at the bottom left of parent window
            child_x = self.parent_x - child_w - xoffset
            child_x = max(10, child_x)
            child_y = self.parent_y
            self.papertape_viewer.geometry(f"{child_w}x{child_h}+{child_x}+{child_y}")
            self.init_window_pos = False
            self.papertape_viewer.update_idletasks()
            self.papertape_viewer.transient(self.master)

        self.papertape_viewer.bring_to_front()
        self._update_file_status()

    def hide(self) -> None:
        "Hide the papertape reader viewer"
        if self.papertape_viewer is None:
            return
        self.papertape_viewer.withdraw()
        if self.master is not None:
            self.master.after_idle(self.master.focus_force)

    def toggle_file_write_mode(self) -> None:
        "Toggle the papertape punch file write mode"
        self.file_write_mode = (
            "append" if self.file_write_mode == "overwrite" else "overwrite"
        )
        self._update_file_status()

    def on(self) -> bool:
        "Turn on the papertape punch"
        if self.papertape_viewer is None:
            return False
        if self.tape_file is None:
            return False  # No tape file not open
        self.active = True  # Enable punching
        return True

    def off(self) -> bool:
        "Turn off the papertape punch but keep tape loaded"
        if self.papertape_viewer is None:
            return False
        self.active = False  # Disable punching
        return True

    def load_tape(self) -> str:
        "Load a tape file for punching"
        if self.papertape_viewer is None:
            return "error"

        # if a tape file is already open, close it
        if self.tape_file is not None:
            self.tape_file.close()
        if self.remote_sftp is not None:
            self.remote_sftp.close()
            self.remote_sftp = None
        if self.remote_ssh is not None:
            self.remote_ssh.close()
            self.remote_ssh = None

        if self.remote_enabled:
            return self._load_remote_tape()

        initial_dir = os.path.dirname(self.init_name_path) if self.init_name_path else "."
        name_path = get_reader_file_selection(self.papertape_viewer, initial_dir=initial_dir)

        if name_path is None:
            return "canceled"
        try:
            if self.file_write_mode == "append":
                self.tape_file = self._open_for_append_with_preview(
                    name_path,
                    self.papertape_viewer
                )
            else:
                self.tape_file = open(name_path, "wb")  # overwrite mode

            self.tape_file.seek(0, os.SEEK_END)
        except (FileNotFoundError, PermissionError, OSError) as _:
            return "error"

        self.pt_name_path = name_path
        self.init_name_path = name_path
        self.tape_loaded = True
        self.active = False  # initially stopped
        self._update_file_status()
        return "loaded"

    def _load_remote_tape(self) -> str:
        """Open a remote tape file for punching over SFTP."""
        ssh, sftp = self._connect_remote()
        if ssh is None or sftp is None:
            return "error"

        try:
            names = sftp.listdir(self.remote_dir)
            tape_names = [
                n for n in names
                if not n.startswith("._")
                and n.lower().endswith(PAPER_TAPE_EXTENSIONS)
            ]
            tape_names.sort()

            selected_name = self._select_remote_punch_name(tape_names)
            if selected_name is None:
                return "cancelled"

            remote_path = self.remote_dir.rstrip("/") + "/" + selected_name
            if self.file_write_mode == "append":
                try:
                    with sftp.open(remote_path, "rb") as preview:
                        contents = preview.read()
                    self.papertape_viewer.add_byte(contents)
                except OSError:
                    pass
                self.tape_file = sftp.open(remote_path, "ab")
            else:
                self.tape_file = sftp.open(remote_path, "wb")

            self.remote_ssh = ssh
            self.remote_sftp = sftp
            self.pt_name_path = remote_path
            self.init_name_path = remote_path
            self.tape_loaded = True
            self.active = False
            self._update_file_status()
            return "loaded"
        except (OSError, IOError, paramiko.SSHException) as e:
            print(f"Could not open remote paper tape punch file: {e}")
            return "error"
        finally:
            if self.tape_file is None:
                sftp.close()
                ssh.close()

    def unload_tape(self) -> bool:
        "Remove tape from punch"
        if self.papertape_viewer is None:
            return False
        self.off()
        if self.ptp_attached:
            self.detach_ptp()
        if self.tape_loaded:
            self.tape_loaded = False
        self.ptp_attached = False
        if self.tape_file is not None:
            self.tape_file.close()  # Close tape file if open
            self.tape_file = None
            if self.remote_sftp is not None:
                self.remote_sftp.close()
                self.remote_sftp = None
            if self.remote_ssh is not None:
                self.remote_ssh.close()
                self.remote_ssh = None
        self.pt_name_path = None
        self.papertape_viewer.unload_tape()
        self._update_file_status()
        return True

    def punch_bytes(self, data: str | bytes) -> None:
        """Punch one or more bytes onto the tape."""
        if self.papertape_viewer is None:
            return
        if not self.active or self.tape_file is None:
            return

        if isinstance(data, str):
            bytes_data = data.encode("ascii")
        else:
            bytes_data = data

        self.tape_file.write(bytes_data)
        if hasattr(self.tape_file, "flush"):
            self.tape_file.flush()
        self.papertape_viewer.add_byte(bytes_data)
        self._update_file_status()

    def _open_for_append_with_preview(self, filename, pt_viewer) -> BinaryIO:
        "Open tape file for appending, and load existing contents into viewer"
        file = open(filename, "ab+")  # open for read and append
        file.seek(0)
        contents = file.read()  # read existing contents
        pt_viewer.add_byte(contents)  # load existing contents into viewer
        return file  # caller must close this

    def _update_file_status(self) -> None:
        """Update the file status in the papertape viewer."""
        if self.papertape_viewer is None:
            return

        mode = self.file_write_mode.capitalize()
        if self.pt_name_path is None:
            filename = "Unloaded"
            status = f"Mode: {mode}"
        elif self.ptp_attached:
            filename = os.path.basename(self.pt_name_path)
            status = "SIMH PTP attached"
        else:
            filename = os.path.basename(self.pt_name_path)
            mode = self.file_write_mode.capitalize()
            file_size = self.tape_file.tell() if self.tape_file is not None else 0
            status = f"{mode}, {file_size} bytes"

        self.papertape_viewer.set_file_status(f"File: {filename}", f"{status}")


def get_file_types():
    """Return file types for the file dialog."""
    return [
        ("Tape files", ("*.pt", "*.pb", "*.pa", "*.pr", "*.bpt", "*.apt", "*.rpt", "*.tap")),
        ("Source files", ("*.pa", "*.ba", "*.ft", "*.fc", "*.tx")),
        ("Misc files", ("*.raw", "*.asc", "*.s19", "*.S29", "*.srec")),
        ("All files", "*.*")
    ]

def get_reader_file_selection(master, initial_dir="."):
    """Open a file dialog for selecting a reader file."""
    filename = filedialog.askopenfilename(
        parent=master,
        title="Select file for paper tape reader to read",
        initialdir=initial_dir,
        filetypes=get_file_types()
    )
    # Restore focus to parent
    if master is not None:
        master.after_idle(master.focus_force)
    return filename if filename else None

def get_punch_file_selection(master, initial_dir="."):
    """Open a file dialog for selecting a punch file."""
    filename = filedialog.asksaveasfilename(
        parent=master,
        title="Select file for paper tape punch to write",
        initialdir=initial_dir,
        filetypes=get_file_types(),
        confirmoverwrite=False,
        defaultextension=".pt"
    )
    # Restore focus to parent
    if master is not None:
        master.after_idle(master.focus_force)
    return filename if filename else None
