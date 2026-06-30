#!/usr/bin/env python3

"""
ASR-33 sound generation state machine using Pygame mixer.
"""
import threading
import time
import queue
import random
from pathlib import Path
import contextlib
import io
# Suppress the pygame welcome message
with contextlib.redirect_stdout(io.StringIO()):
    import pygame
from pygame import mixer

# --- Configuration ---
FADE_DURATION_SECONDS = 0.1          # Fast fade for character switches
INACTIVITY_TIMEOUT_SECONDS = 0.2     # 200ms timeout to switch to hum
UPDATE_INTERVAL_SECONDS = 0.050      # State machine update interval
MUTE_FADE_SECONDS = 0.2              # Fade duration for mute/unmute
DEFAULT_EFFECT_PLAYTIME_MS = 500     # Default max play time for effects

MOTOR_ON_PLAY_TIME = 1500
MOTOR_OFF_PLAY_TIME = 400
LID_PLAY_TIME = 250
BELL_PLAY_TIME = 500
PLATEN_PLAY_TIME = 100
CR_PLAY_TIME = 150
KEY_PLAY_TIME = 100

MAX_EFFECTS_QUEUE_SIZE = 4  # Max number of pending effect sounds

class TeletypeStateMachine:
    """ASR-33 sound generation state machine using Pygame mixer."""
    def __init__(self, lock):
        self.lock = lock
        self.event_queue = queue.Queue()
        self.effects_queue = queue.Queue()

        mixer.init()
        mixer.set_num_channels(5)  # 4 continuous + 1 effects channel

        self._sounds_dictionary = {}
        self.effects_key_list = [
            'key',
            'bell',
            'cr',
            'platen',
            'motor-on',
            'motor-off',
            'lid'
        ]
        self.ch_chars = mixer.Channel(0)
        self.ch_spaces = mixer.Channel(1)
        self.ch_hum = mixer.Channel(2)
        self.ch_tape_reader = mixer.Channel(3)
        self.ch_effects = mixer.Channel(4)

        with self.lock:
            self.ch_chars.set_volume(0.0)
            self.ch_spaces.set_volume(0.0)
            self.ch_hum.set_volume(0.0)
            self.ch_tape_reader.set_volume(0.0)

        self.current_state = None
        self.last_event_time = time.time()
        self.target_volumes = {
            ch: 0.0 for ch in [
                self.ch_chars,
                self.ch_spaces,
                self.ch_hum,
                self.ch_tape_reader
            ]
        }
        self.fade_start_time = None
        self.fade_start_volumes = None # type: ignore

        self.is_muted = False
        self.mute_fade_start = None
        self.mute_fade_from = None
        self.mute_fade_to = None

        self.lid_state = "up"  # 'up' or 'down'

        self.running = True
        self.actual_volumes = {
            ch: 0.0 for ch in [
                self.ch_chars,
                self.ch_spaces,
                self.ch_hum,
                self.ch_tape_reader,
                self.ch_effects
            ]
        }

    def start(self) -> None:
        """Starts the state machine."""
        with self.lock:
            self.running = True
            self._build_sounds_dictionary()
            self._start_continuous_sounds()

    def stop(self) -> None:
        """Stops the state machine."""
        with self.lock:
            self.running = False

    def _build_sounds_dictionary(self, sounds_dir: Path | str | None = None) -> None:
        """
        Start the sound module and load sounds from the given directory.
        Defaults to a 'sounds' folder next to this file.
        """
        # Normalize to a pathlib.Path. Default to the bundled `sounds` directory
        # located next to this file when `None` is provided.
        if sounds_dir is None:
            sounds_dir = Path(__file__).parent / "sounds"
        else:
            sounds_dir = Path(sounds_dir)

        try:
            for entry in sounds_dir.glob("*.wav"):
                name = entry.stem  # filename without extension
                try:
                    self._sounds_dictionary[name] = mixer.Sound(entry)
                except pygame.error as e:  # pylint: disable=no-member
                    print(f"Error loading {entry.name}: {e}")
        except FileNotFoundError:
            print(f"ERROR: 'sounds' directory not found at {sounds_dir}")
            return

        if not self._sounds_dictionary:
            print("Warning: No sounds loaded. Module running without audio.")

    def _get_sound(self, sound_name: str) -> mixer.Sound | None:
        """
        Retrieve a sound by name, considering lid state and random variants.
        """
        prefix = f"{self.lid_state}-{sound_name}"

        # Exact match
        sound = self._sounds_dictionary.get(prefix)
        if sound:
            return sound

        # Variant matches
        matches = [s for name, s in self._sounds_dictionary.items() if name.startswith(prefix)]
        return random.choice(matches) if matches else None

    def _start_continuous_sounds(self) -> None:
        """Restart continuous sounds."""
        # Map sound keys to their continuous channels
        channel_map = {
            'print-chars': self.ch_chars,
            'print-spaces': self.ch_spaces,
            'hum': self.ch_hum,
            "tape-reader": self.ch_tape_reader,
        }

        # Restart playback if this is a continuous looping sound
        for key, ch in channel_map.items():
            ch.stop()
            sound = self._get_sound(key)
            if sound is not None:
                ch.play(sound, loops=-1)

    def _set_volume_targets(self, target_state) -> None:
        """Sets target volumes for the given state and initiates fade."""
        self.fade_start_time = time.time()
        self.fade_start_volumes = {
            ch: self.actual_volumes[ch] for ch in [
                    self.ch_chars,
                    self.ch_spaces,
                    self.ch_hum,
                    self.ch_tape_reader
                ]
        }
        if target_state == 'print-chars':
            self.target_volumes[self.ch_chars] = 1.0
            self.target_volumes[self.ch_spaces] = 0.0
            self.target_volumes[self.ch_hum] = 0.0
        elif target_state == 'print-spaces':
            self.target_volumes[self.ch_chars] = 0.0
            self.target_volumes[self.ch_spaces] = 1.0
            self.target_volumes[self.ch_hum] = 0.0
        elif target_state == 'hum':
            self.target_volumes[self.ch_chars] = 0.0
            self.target_volumes[self.ch_spaces] = 0.0
            self.target_volumes[self.ch_hum] = 1.0
        elif target_state == 'tape-reader':
            self.target_volumes[self.ch_chars] = 0.0
            self.target_volumes[self.ch_spaces] = 0.0
            self.target_volumes[self.ch_hum] = 0.0

    def set_mute_status(self, muted: bool) -> None:
        """Sets mute status with fade."""
        with self.lock:
            if muted != self.is_muted:
                # record prior state so we can set a sensible fade start value
                prior_is_muted = self.is_muted
                # start fade
                self.mute_fade_start = time.time()
                # If currently not muted, fade starts from 1.0 -> 0.0 when muting.
                # If currently muted, fade starts from 0.0 -> 1.0 when unmuting.
                self.mute_fade_from = 0.0 if prior_is_muted else 1.0
                self.mute_fade_to = 0.0 if muted else 1.0
                self.is_muted = muted

    def set_lid_state(self, state: str) -> None:
        """Set the lid state and update sound files accordingly."""
        with self.lock:
            self.lid_state = state # 'up' or 'down'
            # Restart continuous sounds to reflect lid state
            self._start_continuous_sounds()

    def set_tape_reader_state(self, running: bool) -> None:
        """Turn on or off the tape reader sound."""
        with self.lock:
            if running:
                self.actual_volumes[self.ch_tape_reader] = 1.0
            else:
                self.actual_volumes[self.ch_tape_reader] = 0.0

    def new_character_event(self, char_type, playtime_ms=None):
        """Thread-safe method to push a new character type into the main queue."""
        with self.lock:
            self.event_queue.put((char_type, playtime_ms))

    def process_event(self, char_type, playtime_ms=None) -> None:
        """Processes the next character event and sets new target state/volumes."""
        with self.lock:
            self.last_event_time = time.time()
            if char_type in ['print-chars', 'print-spaces']:
                if self.current_state != char_type:
                    self.current_state = char_type
                    self._set_volume_targets(char_type)
            elif char_type in self.effects_key_list:
                sound_obj = self._get_sound(char_type)
                if playtime_ms is None:
                    playtime_ms = DEFAULT_EFFECT_PLAYTIME_MS
                # Limit the effects queue size to MAX_EFFECTS_QUEUE_SIZE
                # to prevent excessive growth at high data rates.
                if sound_obj is not None and self.effects_queue.qsize() < MAX_EFFECTS_QUEUE_SIZE:
                    self.effects_queue.put((sound_obj, playtime_ms))
                if not self.ch_effects.get_busy() and not self.effects_queue.empty():
                    self.play_next_effect()

    def check_inactivity(self) :
        """Checks for inactivity and switches to hum state if needed."""
        with self.lock:
            if self.current_state != 'hum':
                elapsed = time.time() - self.last_event_time
                if elapsed >= INACTIVITY_TIMEOUT_SECONDS:
                    self.current_state = 'hum'
                    self._set_volume_targets('hum')

    def play_next_effect(self) -> None:
        """Plays the next effect sound from the effects queue."""
        if not self.effects_queue.empty():
            sound_obj, playtime_ms = self.effects_queue.get()
            if sound_obj is not None:
                self.ch_effects.play(sound_obj, maxtime=playtime_ms)
                self.actual_volumes[self.ch_effects] = 1.0

    def update_volumes(self) -> None:
        """Updates the actual volumes towards target volumes with fading."""
        with self.lock:
            if self.fade_start_time is not None:
                elapsed = time.time() - self.fade_start_time
                if elapsed >= FADE_DURATION_SECONDS:
                    for ch in [self.ch_chars, self.ch_spaces, self.ch_hum]:
                        # Use target volumes if fade finished
                        self.actual_volumes[ch] = (
                            self.target_volumes.get(ch, self.actual_volumes.get(ch, 0.0))
                        )
                    self.fade_start_time = None
                    self.fade_start_volumes = None
                else:
                    progress = elapsed / FADE_DURATION_SECONDS
                    for ch in [self.ch_chars, self.ch_spaces, self.ch_hum]:
                        # fade_start_volumes can be None in some race conditions;
                        # fall back to current actual volume
                        if self.fade_start_volumes is None:
                            start_vol = self.actual_volumes.get(ch, 0.0)
                        else:
                            start_vol = self.fade_start_volumes.get(
                                ch,
                                self.actual_volumes.get(ch, 0.0)
                            )
                        target_vol = self.target_volumes.get(ch, 0.0)
                        new_volume = start_vol + (target_vol - start_vol) * progress
                        self.actual_volumes[ch] = max(0.0, min(1.0, new_volume))

            mute_factor = 1.0
            if self.is_muted and self.mute_fade_start is None:
                mute_factor = 0.0
            if self.mute_fade_start is not None:
                elapsed = time.time() - self.mute_fade_start
                # compute safe defaults if fade endpoints were not set
                default_to = 0.0 if self.is_muted else 1.0
                default_from = 1.0 if self.is_muted else 0.0
                to_val = self.mute_fade_to if self.mute_fade_to is not None else default_to
                from_val = self.mute_fade_from if self.mute_fade_from is not None else default_from
                if elapsed >= MUTE_FADE_SECONDS:
                    mute_factor = to_val
                    self.mute_fade_start = None
                else:
                    progress = elapsed / MUTE_FADE_SECONDS
                    mute_factor = from_val + (to_val - from_val) * progress
            for ch in [
                self.ch_chars,
                self.ch_spaces,
                self.ch_hum,
                self.ch_tape_reader,
                self.ch_effects
            ]:
                intended_volume = self.actual_volumes[ch]
                final_volume = intended_volume * mute_factor
                ch.set_volume(final_volume)

            if not self.ch_effects.get_busy() and self.actual_volumes[self.ch_effects] != 0.0:
                self.actual_volumes[self.ch_effects] = 0.0

def _sounds_worker(manager_instance) -> None:
    """Main loop for the audio thread."""
    while manager_instance.running:
        while not manager_instance.event_queue.empty():
            char_type, playtime_ms = manager_instance.event_queue.get()
            manager_instance.process_event(char_type, playtime_ms)

        manager_instance.check_inactivity()

        with manager_instance.lock:
            if (
                not manager_instance.ch_effects.get_busy() and
                not manager_instance.effects_queue.empty()
            ):
                manager_instance.play_next_effect()

        manager_instance.update_volumes()
        time.sleep(UPDATE_INTERVAL_SECONDS)

    mixer.quit()


class ASR33AudioModule:
    """Encapsulates the TeletypeStateMachine and audio thread."""
    def __init__(self) -> None:
        self.shared_mixer_lock = threading.Lock()
        self.tt_manager = TeletypeStateMachine(self.shared_mixer_lock)
        self.audio_thread = threading.Thread(
            target=_sounds_worker, args=(self.tt_manager,), daemon=True
        )
        self.audio_thread.start()
        self.lid_state = "up"  # Initial lid state

    def stop(self) -> None:
        """Stops the audio module and joins the audio thread."""
        self.motor_off()  # Play motor-off sound at stop
        time.sleep(MOTOR_OFF_PLAY_TIME/1000.0 + 0.1)  # Wait for motor-off sound to finish
        self.tt_manager.stop()
        self.audio_thread.join()

    def start(self) -> None:
        """Starts the audio module."""
        self.tt_manager.start()
        self.motor_on()  # Play motor-on sound at start
        time.sleep(MOTOR_ON_PLAY_TIME/1000.0 + 0.1)  # Wait for motor-on sound to finish

    def mute(self, mute_flag: bool) -> None:
        """Sets mute status of the audio module."""
        self.tt_manager.set_mute_status(mute_flag)

    def print_char(self, ch: str) -> None:
        """Sends a character event to the state machine, with optional playtime limit."""
        if ch == '\r':  # carriage return
            self.tt_manager.new_character_event("cr", CR_PLAY_TIME)
        elif ch == '\n':  # line feed
            self.tt_manager.new_character_event("platen", PLATEN_PLAY_TIME)
        elif ch == '\a':  # bell character
            self.tt_manager.new_character_event("bell", BELL_PLAY_TIME)
        elif ch <= " " or ch > "~":  # control or non-printable
            self.tt_manager.new_character_event("print-spaces")
        else:
            self.tt_manager.new_character_event("print-chars")

    def platen(self, playtime_ms=PLATEN_PLAY_TIME) -> None:
        """Sends a platen (line feed) event."""
        self.tt_manager.new_character_event('platen', playtime_ms)
    def column_bell(self, playtime_ms=BELL_PLAY_TIME) -> None:
        """Sends a bell event."""
        self.tt_manager.new_character_event('bell', playtime_ms)

    def keypress(self, playtime_ms=KEY_PLAY_TIME) -> None:
        """Sends a key-press event."""
        self.tt_manager.new_character_event('key', playtime_ms)
    def motor_on(self, playtime_ms=MOTOR_ON_PLAY_TIME) -> None:
        """Sends a motor-on event."""
        self.tt_manager.new_character_event('motor-on', playtime_ms)

    def motor_off(self, playtime_ms=MOTOR_OFF_PLAY_TIME) -> None:
        """Sends a motor-off event."""
        self.tt_manager.new_character_event('motor-off', playtime_ms)
    def tape_reader_running(self, running: bool = False) -> None:
        """Simulates tape reader running (maps to continuous char sound)."""
        self.tt_manager.set_tape_reader_state(running)

    def lid(self, set_lid_to_up: bool = False) -> None:
        """Sets the lid state and plays the lid sound effect."""
        state_change = True if set_lid_to_up != (self.lid_state == "up") else False
        self.lid_state = "up" if set_lid_to_up is True else "down"
        self.tt_manager.set_lid_state(self.lid_state)
        if state_change: # Play lid sound only on state change
            self.tt_manager.new_character_event('lid', LID_PLAY_TIME)

# --- Simulation of Main Program ---
if __name__ == '__main__':
    shared_mixer_lock = threading.Lock()
    tt_manager = TeletypeStateMachine(shared_mixer_lock)
    audio_thread = threading.Thread(target=_sounds_worker, args=(tt_manager,), daemon=True)
    audio_thread.start()
    tt_manager.start()

    tt_manager.new_character_event('motor-on', MOTOR_OFF_PLAY_TIME)
    time.sleep(3.0)

    print("\n--- Test Case 1: Inactivity Fade to HUM ---")
    time.sleep(2.0)

    print("\n--- Test Case 2: Chars sound during activity ---")
    for _ in range(10):
        tt_manager.new_character_event('print-chars')
        time.sleep(0.1)
    time.sleep(0.5)

    print("\n--- Test Case 3: Spaces sound, then immediate BELL event ---")
    for _ in range(5):
        tt_manager.new_character_event('print-spaces')
        time.sleep(0.1)
    tt_manager.new_character_event('bell', playtime_ms=BELL_PLAY_TIME)
    time.sleep(1.5)

    print("\n--- Test Case 4: Muting and Unmuting mid-activity ---")
    for _ in range(5):
        tt_manager.new_character_event('print-chars')
        time.sleep(0.05)
    print("Muting NOW")
    tt_manager.set_mute_status(True)

    for _ in range(10):
        tt_manager.new_character_event('print-chars')
        time.sleep(0.1)

    time.sleep(0.5)
    print("Unmuting NOW")
    tt_manager.set_mute_status(False)
    time.sleep(1)

    print("\n--- Test Case 5: Sequential CR and LF sounds ---")
    tt_manager.new_character_event('cr', playtime_ms=CR_PLAY_TIME)
    tt_manager.new_character_event('platen', playtime_ms=PLATEN_PLAY_TIME)
    time.sleep(2.0)

    tt_manager.new_character_event('motor-off', MOTOR_OFF_PLAY_TIME)
    time.sleep(2.0)

    print("\n--- Graceful Exit ---")
    tt_manager.stop()
    audio_thread.join()
    print("Audio thread joined successfully. Program exit complete.")
