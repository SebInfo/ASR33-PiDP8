#!/usr/bin/env python3

"""
PiDP-8 ASR-33 terminal entry point.

Usage examples:
    python ./asr33emu.py
    python ./asr33emu.py --config asr33_strict.yaml
"""

from asr33_config import ASR33Config
from asr33_shim_throttle import DataThrottle
from asr33_terminal import Terminal


def create_sound_module():
    """Create optional ASR-33 audio without adding frontend/backend choices."""
    try:
        from asr33_sounds_sm import ASR33AudioModule
    except ModuleNotFoundError as e:
        if e.name == "pygame":
            print("Warning: pygame not installed; running without sound.")
            return None
        raise
    return ASR33AudioModule()


class EmulatorWrapper:
    """PiDP-8 SSH ASR-33 emulator wrapper."""
    def __init__(self):
        self.comm_backend = None
        self.data_throttle = None
        self.term = None
        self.frontend = None

        config = ASR33Config(description="ASR-33 Teletype Emulator")
        cfg_data = None
        try:
            if config is not None:
                cfg_data = config.get_merged_config()
        except FileNotFoundError:
            pass

        if cfg_data is None:
            raise RuntimeError("No configuration file found.")

        try:
            backend_cfg = cfg_data.backend
            data_throttle_cfg = cfg_data.data_throttle
            terminal_cfg = cfg_data.terminal
        except AttributeError as e:
            raise RuntimeError(f"Missing configuration section: {e}") from e

        from asr33_backend_ssh import SSHV2Backend

        self.comm_backend = SSHV2Backend(
            upper_layer=None,
            config=backend_cfg.ssh_config
        )

        # Comm backend feeds data to DataThrottle, which feeds data to Terminal
        cfg = data_throttle_cfg.config
        self.data_throttle = DataThrottle(
            lower_layer=self.comm_backend,
            upper_layer=None,  # Forward reference set later
            config=cfg
        )

        # Terminal
        cfg = terminal_cfg.config
        self.term = Terminal(
            comm_interface=self.data_throttle,
            frontend=None,  # Forward reference set later
            config=cfg
        )

        self.sound = create_sound_module()

        frontend_type = cfg_data.get("frontend", "type", default="tkinter")
        if frontend_type == "qt":
            from asr33_frontend_qt import ASR33QtFrontend

            self.frontend = ASR33QtFrontend(
                terminal=self.term,
                backend=self.data_throttle,
                config=cfg_data,
                sound=self.sound,
            )
        elif frontend_type == "tkinter":
            from asr33_frontend_tk import ASR33TkFrontend

            self.frontend = ASR33TkFrontend(
                terminal=self.term,
                backend=self.data_throttle,
                config=cfg_data,
                sound=self.sound,
            )
        else:
            raise ValueError(f"Unsupported frontend: {frontend_type}")

        # Assign layers that were forward referenced earlier
        if self.comm_backend is not None:
            self.comm_backend.upper_layer = self.data_throttle
        self.data_throttle.upper_layer = self.term
        self.term.frontend = self.frontend

    def run(self):
        """Run the emulator main loop."""
        if self.frontend is None:
            raise RuntimeError("Frontend not initialized")
        self.frontend.run()


if __name__ == "__main__":
    EmulatorWrapper().run()
