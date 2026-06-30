#!/usr/bin/env python3

""" ASR-33 Terminal Emulator Core"""

import threading
from typing import Any

class Line:
    """A single line in the terminal, supporting overstrike."""

    def __init__(self, width: int, logical_line_number: int | None = None):
        """Initialize a line with given width, supporting overstrike."""
        # Each column holds a list of strikes (characters overstruck in that cell)
        self.cells = [[] for _ in range(width)]
        self.width = width
        self.logical_line_number = logical_line_number  # optional logical line number
        self._lock = threading.Lock() # Add a lock for thread safety

    def add_char(self, col: int, ch: str):
        """Add a character at the given column, supporting overstrike."""
        # Acquire lock before modifying shared data (self.cells)
        with self._lock:
            if 0 <= col < self.width:
                self.cells[col].append(ch)

    def get_strike_stack(self, col: int):
        """Return the list of strikes at a given column."""
        # Acquire lock before reading shared data (self.cells)
        with self._lock:
            if 0 <= col < self.width:
                # Return a copy to prevent external modification of the internal list
                return self.cells[col][:]
            return []

    def __repr__(self):
        # Acquire lock before reading shared data (self.cells)
        with self._lock:
            # For debugging: show the top strike of each cell
            return "".join(stack[-1] if stack else " " for stack in self.cells)


class LineHistory:
    """Scrollback buffer for the terminal."""
    def __init__(self, max_lines: int, width: int, logical_line: int):
        self.max_lines = max_lines
        self.width = width
        self.lines = [Line(width, logical_line_number=logical_line)]  # start with one blank line
        self.top_index = 0  # index of the first logical line
        self._lock = threading.Lock() # Add a lock for thread safety

    def add_char(self, col: int, ch: str):
        """Add an overstrike-capable character in the last row at the given column."""
        # This method uses self.lines[-1], which can change if another thread calls add_line.
        # Lock LineHistory, then call the thread-safe Line method.
        with self._lock:
            self.lines[-1].add_char(col, ch)

    def add_line(self, logical_line_number: int | None = None):
        """Append a new blank line at the bottom, discarding the oldest if needed."""
        # Acquire lock before modifying shared data (self.lines, self.top_index)
        with self._lock:
            new_line = Line(self.width)
            if logical_line_number is not None:
                new_line.logical_line_number = logical_line_number
            self.lines.append(new_line)
            if len(self.lines) > self.max_lines:
                self.lines.pop(0)
            # Ensure index update happens under lock
            self.top_index = self.lines[0].logical_line_number if self.lines else 0

    def get_line(self, row: int) -> Line:
        """Get the row-th line from line_history."""
        # Acquire lock before reading shared data (self.lines)
        with self._lock:
            if 0 <= row < len(self.lines):
                 # Return the line object itself (which is now thread-safe internally)
                return self.lines[row]
            return Line(self.width)

    def bottom_lln(self) -> int | None:
        """Return the logical line number of the last line in history."""
        # Acquire lock before reading shared data (self.lines)
        with self._lock:
            if self.lines:
                return self.lines[-1].logical_line_number
            return 0

    def top_lln(self) -> int | None:
        """Return the logical line number of the last line in history."""
        # Acquire lock before reading shared data (self.lines, self.top_index)
        with self._lock:
            # Can return the cached top_index as it's updated under a lock
            return self.top_index

    def __len__(self) -> int:
        """Return number of lines currently in history."""
        # Acquire lock before reading shared data (self.lines)
        with self._lock:
            return len(self.lines)


class EscapeShim:
    """Simple stream-safe shim class to strip off
       CSI and OSC ANSI escape sequences.
    """
    def __init__(self):
        self.state = "GROUND"

    def feed(self, data: str) -> str:
        """
        Feed single chars or strings. Returns only printable text,
        stripping CSI and OSC escape sequences.
        """
        out = []
        for ch in data:
            if self.state == "GROUND":
                if ch == "\x1b":
                    self.state = "ESC"
                else:
                    out.append(ch)

            elif self.state == "ESC":
                if ch == "[":
                    self.state = "CSI"
                elif ch == "]":
                    self.state = "OSC"
                else:
                    # swallow single-char ESC sequences
                    self.state = "GROUND"

            elif self.state == "CSI":
                # swallow until final byte in @-~
                if "@" <= ch <= "~":
                    self.state = "GROUND"

            elif self.state == "OSC":
                # swallow until BEL or ST (ESC \)
                if ch == "\x07":
                    self.state = "GROUND"
                elif ch == "\x1b":
                    self.state = "OSC_ESC"
            elif self.state == "OSC_ESC":
                if ch == "\\":
                    self.state = "GROUND"
                else:
                    # not actually ST, stay in OSC
                    self.state = "OSC"

        return "".join(out)


class Terminal:
    """ASR-33 Terminal Emulator Core."""

    def __init__(
            self,
            comm_interface: Any,
            frontend: Any,
            config
        ) -> None:
        """
        width: number of columns per line
        height: number of visible lines (for frontend)
        max_lines: line_history capacity
        frontend_callback: function to call when screen needs refresh
        """
        self._comm_interface=comm_interface
        self.frontend = frontend
        self.esc_stripper = EscapeShim()
        self.width = config.get("columns", default=72)
        self.height = config.get("rows", default=24)
        self.autowrap = config.get("autowrap", default=False)
        self.cur_col = 0
        self.cur_line_number = 0
        self.srollback_lines = config.get("scrollback", default=200)
        self.line_history = LineHistory(
            max_lines=self.height + self.srollback_lines,
            width=self.width,
            logical_line=self.cur_line_number
        )
        self.sound_playback_queue = []
        self._print_enabled = True

    def encode_even_parity(self, byte_data: bytes) -> bytes:
        """
        Encodes even parity into the 8th (most significant) bit of each byte 
        in the input byte string.

        The input data is assumed to be 7-bit clean (MSB is 0).
        """
        # We use a bytearray for efficient modification of individual bytes
        encoded_data = bytearray(byte_data)
        for i, current_byte in enumerate(encoded_data):
            current_byte = encoded_data[i]
            parity_bit = (current_byte ^ (current_byte >> 1) ^ (current_byte >> 2) ^
                        (current_byte >> 3) ^ (current_byte >> 4) ^ (current_byte >> 5) ^
                        (current_byte >> 6) ^ (current_byte >> 7)) & 1

            if parity_bit == 1:
                encoded_data[i] = current_byte | 0x80
            else:
                encoded_data[i] = current_byte & 0x7F

        return bytes(encoded_data)

    def mask_parity_bit(self, data: bytes) -> bytes:
        """ Mask off the parity bit (8th bit) from each byte in data. """
        return bytes([b & 0x7F for b in data])

    def receive_data(self, data: bytes) -> None:
        """ Accept data (as bytes) from backend (thread) and process it."""
        masked_data = self.mask_parity_bit(data) # ASR-33 uses 7-bit ASCII (parity/8th bit ignored)
        char_data = masked_data.decode("ascii", "replace")

        # Strip out escape sequences
        # Not ASR-33 behavior, but useful for Linux terminal output
        char_data = self.esc_stripper.feed(char_data)
        if self._print_enabled:
            for ch in char_data:
                if ch == "\r":
                    # Carriage return: reset column
                    self.cur_col = 0
                elif ch == "\n" or ch == "\v":
                    # Line feed: move to next line
                    self.cur_line_number += 1  # Increment logical line number
                    self.line_history.add_line(logical_line_number=self.cur_line_number)
                elif ch == "\b":
                    # Backspace: move cursor left (not an ASR-33 behavior but useful)
                    if self.cur_col > 0:
                        self.cur_col -= 1
                elif ch == "\t":
                    # Tab: advance to next multiple of 8
                    self.cur_col = (self.cur_col + 8) & ~7
                elif ch == "\f":
                    # Form feed: move cursor left (not an ASR-33 behavior but useful)
                    if self.cur_col > 0:
                        self.cur_col -= 1
                else:
                    # Only accept printable characters
                    if ch.isprintable():
                        self.line_history.add_char(self.cur_col, ch)
                        self.cur_col += 1

                # Handle autowrap if enabled
                if self.autowrap and self.cur_col >= self.width:
                    # If autowrap enabled, move to next line
                    self.cur_col -= self.width
                    self.cur_line_number += 1  # Increment logical line number
                    self.line_history.add_line(logical_line_number=self.cur_line_number)

                # Limit cursor column, to screen width
                self.cur_col = min(self.cur_col, self.width-1)

                # Queue character for sound playback
                self.sound_playback_queue.append((ch, self.cur_col))

        # Send unmasked, unstripped 8-bit data to frontend if needed
        if len(data) > 0:
            if hasattr(self.frontend, 'receive_data'):
                self.frontend.receive_data(data)

    def send_data(self, data: bytes) -> None:
        """ Send data (as bytes) to backend. """
        if self._comm_interface:
            self._comm_interface.send_data(data)

    def get_cursor_position(self):
        """Return current cursor position as (row, col)."""
        row = self.line_history.bottom_lln()  # zero-based row index
        col = self.cur_col
        return (col, row)

    def sound_queue_len(self):
        """Return number of characters waiting for sound playback."""
        return len(self.sound_playback_queue)

    def pop_char_from_sound_queue(self):
        """
        Pop the next (char, col) tuple from the new character queue.
        Returns None if queue is empty.
        """
        if self.sound_playback_queue:
            return self.sound_playback_queue.pop(0)
        return None

    def enable_printing(self):
        """ Enable printing of received characters. """
        self._print_enabled = True

    def disable_printing(self):
        """ Disable printing of received characters. """
        self._print_enabled = False
