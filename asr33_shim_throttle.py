#!/usr/bin/env python3

"""ASR-33 Data Throttle Module"""
import threading
import queue
import time
from typing import Any

class DataThrottle:
    """Controls data rate between the communication backend and terminal emulator."""

    PERCEPTION_THRESHOLD_MS = 20

    def __init__(
            self,
            lower_layer: Any,
            upper_layer: Any,
            config,
            send_queue_size: int = 8,  # keep tape reader from running too far ahead
            receive_queue_size: int = 8  # keep from running too far ahead of terminal
        ):
        self._lower_layer = lower_layer
        self.upper_layer = upper_layer
        send_rate_cps = config.get("send_rate_cps", default=10)
        receive_rate_cps = config.get("receive_rate_cps", default=10)
        self._send_rate = send_rate_cps
        self._receive_rate = receive_rate_cps
        self._throttling_enabled = True
        self._loopback_enabled = False
        self._lock = threading.Lock() # Add a lock for thread safety


        self._send_queue = queue.Queue(send_queue_size)
        self._receive_queue = queue.Queue(receive_queue_size)
        self._loopback_queue = queue.Queue(8)  # Small queue for loopback data

        self._running = True
        self._tx_thread = None
        self._rx_thread = None

    def get_info_string(self) -> str:
        """Return lower layer info string."""
        if hasattr(self._lower_layer, 'get_info_string'):
            return self._lower_layer.get_info_string()
        return ""

    def get_cached_ssh_password(self):
        """Return lower layer cached SSH password if the backend exposes one."""
        if hasattr(self._lower_layer, 'get_cached_ssh_password'):
            return self._lower_layer.get_cached_ssh_password()
        return None

    def send_data(self, data: bytes) -> None:
        """Queues data received from upper layer to be sent to
           the lower layer (or looped back to the upper layer).
        """
        if data:
            if self._loopback_enabled:
                self._loopback_queue.put(data)
            else:
                self._send_queue.put(data)

    def receive_data(self, data: bytes) -> None:
        """Queues data received from the lower layer to be sent up to higher layer."""
        if data:
            if self._loopback_enabled:
                return  # Ignore received data in loopback mode
            self._receive_queue.put(data)

    def set_send_rate(self, cps: int):
        """Sets rate in characters per second
           at which data received from higher layers
           will be sent to backend.
        """
        self._send_rate = cps

    def set_receive_rate(self, cps: int):
        """Sets rate in characters per second
           at which data received from backend
           will be sent to higher layers.
        """
        self._receive_rate = cps

    def enable_throttling(self):
        """Enables data rate throttling."""
        self._throttling_enabled = True

    def disable_throttling(self):
        """Disables data rate throttling."""
        self._throttling_enabled = False

    def enable_loopback(self):
        """Enables loopback mode."""
        with self._lock:
            # Clear stale data in loopback queue
            while not self._loopback_queue.empty():
                self._loopback_queue.get()
            self._loopback_enabled = True

    def disable_loopback(self):
        """Disables loopback mode."""
        with self._lock:
            self._loopback_enabled = False

    def _send_data_to_backend(self, data: bytes):
        """Sends data to the lower layer (comm backend)."""
        # Only send if not in loopback mode
        if self._loopback_enabled:
            return
        self._lower_layer.send_data(data)

    def _send_data_to_upper_layer(self, data: bytes):
        """Sends data to the upper layer (terminal emulator)."""
        # Only send if not in loopback mode
        if self._loopback_enabled:
            return
        if self.upper_layer is None or not hasattr(self.upper_layer, 'receive_data'):
            return
        self.upper_layer.receive_data(data)

    def _send_loopback_to_upper_layer(self, data: bytes):
        """Sends data directly to upper layer in loopback mode."""
        if not self._loopback_enabled:
            return
        if self.upper_layer is None or not hasattr(self.upper_layer, 'receive_data'):
            return
        self.upper_layer.receive_data(data)

    def _throttle_tx_worker(self):
        """Manages sending data from the send queue to the backend or loopback."""
        last_send_time = time.monotonic()
        while self._running:
            # Process data received from upper layer and send to backend
            # The data will be discarded by _send_data_to_backend if loopback is enabled
            last_send_time = self._process_queue_item(
                self._send_queue,
                self._send_rate,
                self._send_data_to_backend,
                last_send_time
            )
            time.sleep(0.0033)

    def _throttle_rx_worker(self):
        """Manages sending data from the receive queue to the upper layer or loopback."""
        # Initialize separate timestamps for loopback and receive processing
        last_receive_time = time.monotonic()
        last_receive_time_lb = last_receive_time
        while self._running:
            # Process loopback data received from upper layer
            # The data will be discarded by _send_loopback_to_upper_layer
            # if loopback is not enabled
            last_receive_time_lb = self._process_queue_item(
                self._loopback_queue,
                self._send_rate,
                self._send_loopback_to_upper_layer,
                last_receive_time_lb
            )
            # Send data received from backend and send to upper layer
            # The data will be discarded by _send_data_to_upper_layer
            # if loopback is enabled
            last_receive_time = self._process_queue_item(
                self._receive_queue,
                self._receive_rate,
                self._send_data_to_upper_layer,
                last_receive_time
            )
            time.sleep(0.0033)

    def _process_queue_item(
            self,
            q: queue.Queue,
            rate: int,
            destination_func,
            last_event_time: float
        ) -> float:
        """Processes the next item in the queue (a chunk of bytes).
           Serializes if throttling is active.
        """
        try:
            chunk = q.get_nowait()
        except queue.Empty:
            return last_event_time

        # Check throttling status immediately
        if not self._throttling_enabled or rate <= 0:
            # Throttling Disabled: Send the entire chunk immediately
            destination_func(chunk)
            return time.monotonic()

        # Throttling Enabled: Serialize bytes with delays,
        # respecting a mid-chunk flag change
        current_time = last_event_time
        delay_per_char = 1.0 / rate
        remaining_chunk = b''
        for i, byte in enumerate(chunk):
            # Break immediately if throttling changes to disabled
            if not self._throttling_enabled:
                # Save the remaining part of the chunk to send instantly below
                remaining_chunk = chunk[i:]
                break

            time_since_last_char = time.monotonic() - current_time

            if time_since_last_char < delay_per_char:
                time_to_wait = delay_per_char - time_since_last_char
                if time_to_wait > (self.PERCEPTION_THRESHOLD_MS / 1000.0):
                    time.sleep(time_to_wait)

            destination_func(bytes([byte]))
            current_time = time.monotonic()

        # If we broke the loop early due to disabling throttling,
        # send the rest instantly (if any)
        if remaining_chunk:
            destination_func(remaining_chunk)
            return time.monotonic() # Update timestamp to now, as the rest was instant

        return current_time # Return the time the *last* byte of the chunk was processed

    def start(self):
        """Starts the management threads."""
        self._running = True
        if self._tx_thread is None or not self._tx_thread.is_alive():
            self._tx_thread = threading.Thread(target=self._throttle_tx_worker, daemon=True)
            self._tx_thread.start()
        if self._rx_thread is None or not self._rx_thread.is_alive():
            self._rx_thread = threading.Thread(target=self._throttle_rx_worker, daemon=True)
            self._rx_thread.start()

        # Start lower layer after we have started
        if not self._lower_layer is None and hasattr(self._lower_layer, "start"):
            self._lower_layer.start()

    def close(self):
        """Stops the management threads."""
        if not self._lower_layer is None and hasattr(self._lower_layer, "close"):
            self._lower_layer.close()

        self._running = False
        if self._tx_thread is not None:
            self._tx_thread.join()
        if self._rx_thread is not None:
            self._rx_thread.join()
