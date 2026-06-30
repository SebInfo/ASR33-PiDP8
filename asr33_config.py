"""ASR-33 Configuration Loader"""

from typing import Any
import argparse
import yaml

class ConfigNode:
    """
    Lightweight wrapper that allows attribute-style access to dictionaries.
    Example:
        config.backend.ssh_config.host
    """
    def __init__(self, data):
        self._data = data

    def __getattr__(self, key: str) -> Any:
        if isinstance(self._data, dict) and key in self._data:
            value = self._data[key]
            # Wrap nested dicts so chaining continues
            return ConfigNode(value) if isinstance(value, dict) else value
        raise AttributeError(f"No such config key: {key}")

    def get(self, *keys, default=None):
        """
        Optional nested getter: config.get("sound", "config", "lid")
        """
        current = self._data
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current


class ASR33Config:
    """Class to load and manage ASR-33 configuration."""
    def __init__(self, description: str = "Command line options"):
        self.args = self.parse_args(description)
        config_path = self.args.config if self.args.config else "asr33_config.yaml"
        # Load YAML
        with open(config_path, 'r', encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # Merge CLI args
        merged = self.merge_with_args(raw)

        # Wrap both raw and merged configs
        self.config = ConfigNode(raw)
        self.merged_config = ConfigNode(merged)

    def parse_args(self, description: str = ""):
        """Parse command-line arguments."""
        parser = argparse.ArgumentParser(description=description)
        parser.add_argument(
            "--config", type=str,
            help="Path to custom YAML config file"
        )
        parser.add_argument(
            "--frontend", choices=["tkinter", "qt"],
            help="Frontend type"
        )
        parser.add_argument(
            "--term_mode", choices=["line", "local"],
            help="Terminal mode: line or local (loopback)"
        )
        parser.add_argument(
            "--columns", type=int,
            help="Number of terminal columns"
        )
        parser.add_argument(
            "--rows", type=int,
            help="Number of terminal rows"
        )
        parser.add_argument(
            "--scrollback", type=int,
            help="Number of terminal scrollback lines"
        )
        parser.add_argument(
            "--throttle_rate", type=int,
            help="Data throttle rate in bytes per second"
        )
        parser.add_argument(
            "--mute", action="store_true",
            help="Use --mute to disable sound"
        )
        return parser.parse_args()

    def merge_with_args(self, raw):
        """Merge command-line arguments with YAML config."""
        merged = raw.copy()

        if self.args is None:
            return merged

        # Normal CLI → config path overrides
        normal_overrides = {
            "frontend": ("frontend", "type"),
            "term_mode": ("terminal", "config", "mode"),
            "columns": ("terminal", "config", "columns"),
            "rows": ("terminal", "config", "rows"),
            "scrollback": ("terminal", "config", "scrollback"),
            "throttle_rate": [
                ("data_throttle", "config", "send_rate_cps"),
                ("data_throttle", "config", "receive_rate_cps"),
            ],
        }

        bool_overrides = {
            "mute": (("sound", "config", "mute_state"), "muted"),
        }

        def normalize_list(x):
            """Normalize a single path or list of paths into a list."""
            if isinstance(x, (list, tuple)) and x and isinstance(x[0], (list, tuple)):
                return x  # already a list of paths
            return [x]  # wrap single path

        # Apply normal overrides
        for arg_name, paths in normal_overrides.items():
            value = getattr(self.args, arg_name, None)
            if value is not None:
                for path in normalize_list(paths):
                    d = merged
                    for key in path[:-1]:
                        if key not in d:
                            d[key] = {}
                        d = d[key]
                    d[path[-1]] = value

        for arg_name, entry in bool_overrides.items():
            if getattr(self.args, arg_name, False):
                path, fixed_value = entry
                for p in normalize_list(path):
                    d = merged
                    for key in p[:-1]:
                        d = d[key]
                    d[p[-1]] = fixed_value

        return merged

    def get_yaml_config(self):
        """Return raw YAML config as a ConfigNode."""
        return self.config

    def get_merged_config(self):
        """Return merged config as a ConfigNode."""
        return self.merged_config

    def get_key(self, *keys, default=None, merged=False):
        """
        Nested key getter.
        Example:
            self.get_key("backend", "ssh_config", "host")
        """
        node = self.merged_config if merged else self.config
        return node.get(*keys, default=default)
