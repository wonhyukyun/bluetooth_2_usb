import asyncio
from asyncio import CancelledError, Task, TaskGroup
import json
import math
from pathlib import Path
import random
import re
import time
from typing import Any, Optional, Union

from adafruit_hid.consumer_control import ConsumerControl
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.mouse import Mouse
from evdev import InputDevice, InputEvent, KeyEvent, RelEvent, categorize, list_devices
import pyudev
import usb_hid
from usb_hid import Device

from .evdev import (
    ecodes,
    evdev_to_usb_hid,
    find_key_name,
    get_mouse_movement,
    is_consumer_key,
    is_mouse_button,
)
from .logging import get_logger

_logger = get_logger()


class GadgetManager:
    """
    Manages enabling, disabling, and references to USB HID gadget devices.

    :ivar _gadgets: Internal dictionary mapping device types to HID device objects
    :ivar _enabled: Indicates whether the gadgets have been enabled
    """

    def __init__(self) -> None:
        """
        Initialize without enabling devices. Call enable_gadgets() to enable them.
        """
        self._gadgets = {
            "keyboard": None,
            "mouse": None,
            "consumer": None,
        }
        self._enabled = False

    def enable_gadgets(self) -> None:
        """
        Disable and re-enable usb_hid devices, then store references
        to the new Keyboard, Mouse, and ConsumerControl gadgets.
        """
        try:
            usb_hid.disable()
        except Exception as ex:
            _logger.debug(f"usb_hid.disable() failed or was already disabled: {ex}")

        usb_hid.enable([Device.BOOT_MOUSE, Device.KEYBOARD, Device.CONSUMER_CONTROL])  # type: ignore
        enabled_devices = list(usb_hid.devices)  # type: ignore

        self._gadgets["keyboard"] = Keyboard(enabled_devices)
        self._gadgets["mouse"] = Mouse(enabled_devices)
        self._gadgets["consumer"] = ConsumerControl(enabled_devices)
        self._enabled = True

        _logger.debug(f"USB HID gadgets re-initialized: {enabled_devices}")

    def get_keyboard(self) -> Optional[Keyboard]:
        """
        Get the Keyboard gadget.

        :return: A Keyboard object, or None if not initialized
        :rtype: Keyboard | None
        """
        return self._gadgets["keyboard"]

    def get_mouse(self) -> Optional[Mouse]:
        """
        Get the Mouse gadget.

        :return: A Mouse object, or None if not initialized
        :rtype: Mouse | None
        """
        return self._gadgets["mouse"]

    def get_consumer(self) -> Optional[ConsumerControl]:
        """
        Get the ConsumerControl gadget.

        :return: A ConsumerControl object, or None if not initialized
        :rtype: ConsumerControl | None
        """
        return self._gadgets["consumer"]


class ShortcutToggler:
    """
    Tracks a user-defined shortcut and toggles relaying on/off when the shortcut is pressed.
    """

    def __init__(
        self,
        shortcut_keys: set[str],
        relaying_active: asyncio.Event,
        gadget_manager: GadgetManager,
    ) -> None:
        """
        :param shortcut_keys: A set of evdev-style key names to detect
        :param relaying_active: An asyncio.Event controlling whether relaying is active
        :param gadget_manager: GadgetManager to release keyboard/mouse states on toggle
        """
        self.shortcut_keys = shortcut_keys
        self.relaying_active = relaying_active
        self.gadget_manager = gadget_manager

        self.currently_pressed: set[str] = set()

    def handle_key_event(self, event: KeyEvent) -> None:
        """
        Process a key press or release to detect the toggle shortcut.

        :param event: The incoming KeyEvent from evdev
        :type event: KeyEvent
        """
        key_name = find_key_name(event)
        if key_name is None:
            return

        if event.keystate == KeyEvent.key_down:
            self.currently_pressed.add(key_name)
        elif event.keystate == KeyEvent.key_up:
            self.currently_pressed.discard(key_name)

        if self.shortcut_keys and self.shortcut_keys.issubset(self.currently_pressed):
            self.toggle_relaying()

    def toggle_relaying(self) -> None:
        """
        Toggle the global relaying state: if it was on, turn it off, otherwise turn it on.
        """
        if self.relaying_active.is_set():
            keyboard = self.gadget_manager.get_keyboard()
            mouse = self.gadget_manager.get_mouse()
            if keyboard:
                keyboard.release_all()
            if mouse:
                mouse.release_all()

            self.currently_pressed.clear()
            self.relaying_active.clear()
            _logger.info("ShortcutToggler: Relaying is now OFF.")
        else:
            self.relaying_active.set()
            _logger.info("ShortcutToggler: Relaying is now ON.")


class RelayController:
    """
    Controls the creation and lifecycle of per-device relays.
    Monitors add/remove events from udev and includes optional auto-discovery.
    """

    def __init__(
        self,
        gadget_manager: GadgetManager,
        device_identifiers: Optional[list[str]] = None,
        auto_discover: bool = False,
        skip_name_prefixes: Optional[list[str]] = None,
        grab_devices: bool = False,
        relaying_active: Optional[asyncio.Event] = None,
        shortcut_toggler: Optional["ShortcutToggler"] = None,
    ) -> None:
        """
        :param gadget_manager: Provides the USB HID gadget devices
        :param device_identifiers: A list of path, MAC, or name fragments to identify devices to relay
        :param auto_discover: If True, relays all valid input devices except those skipped
        :param skip_name_prefixes: A list of device.name prefixes to skip if auto_discover is True
        :param grab_devices: If True, the relay tries to grab exclusive access to each device
        :param relaying_active: asyncio.Event to indicate if relaying is active
        :param shortcut_toggler: ShortcutToggler to allow toggling relaying globally
        """
        self._gadget_manager = gadget_manager
        self._device_ids = [DeviceIdentifier(id) for id in (device_identifiers or [])]
        self._auto_discover = auto_discover
        self._skip_name_prefixes = skip_name_prefixes or ["vc4-hdmi"]
        self._grab_devices = grab_devices
        self._relaying_active = relaying_active
        self._shortcut_toggler = shortcut_toggler

        self._active_tasks: dict[str, Task] = {}
        self._task_group: Optional[TaskGroup] = None
        self._cancelled = False

    async def async_relay_devices(self) -> None:
        """
        Launch a TaskGroup that relays events from all matching devices.
        Dynamically adds or removes tasks when devices appear or disappear.

        :return: Never returns unless an unrecoverable exception or cancellation occurs
        :rtype: None
        """
        try:
            async with TaskGroup() as task_group:
                self._task_group = task_group
                _logger.debug("RelayController: TaskGroup started.")

                for device in await async_list_input_devices():
                    if self._should_relay(device):
                        self.add_device(device.path)

                # Keep running unless canceled
                while not self._cancelled:
                    await asyncio.sleep(0.1)
        except* Exception as exc_grp:
            _logger.exception(
                "RelayController: Exception in TaskGroup", exc_info=exc_grp
            )
        finally:
            self._task_group = None
            _logger.debug("RelayController: TaskGroup exited.")

    def add_device(self, device_path: str) -> None:
        """
        Add a device by path. If a TaskGroup is active, create a new relay task.

        :param device_path: The absolute path to the input device (e.g., /dev/input/event5)
        """
        if not Path(device_path).exists():
            _logger.debug(f"{device_path} does not exist.")
            return

        try:
            device = InputDevice(device_path)
        except (OSError, FileNotFoundError):
            _logger.debug(f"{device_path} vanished before opening.")
            return

        if self._task_group is None:
            _logger.critical(f"No TaskGroup available; ignoring {device}.")
            return

        if device.path in self._active_tasks:
            _logger.debug(f"Device {device} is already active.")
            return

        task = self._task_group.create_task(
            self._async_relay_events(device), name=device.path
        )
        self._active_tasks[device.path] = task
        _logger.debug(f"Created task for {device}.")

    def remove_device(self, device_path: str) -> None:
        """
        Cancel and remove the relay task for a given device path.

        :param device_path: The path of the device to remove
        """
        task = self._active_tasks.pop(device_path, None)
        if task and not task.done():
            task.cancel()
            _logger.debug(f"Cancelled relay for {device_path}.")
        else:
            _logger.debug(f"No active task found for {device_path} to remove.")

    async def _async_relay_events(self, device: InputDevice) -> None:
        """
        Create a DeviceRelay context, then read events in a loop until cancellation or error.

        :param device: The evdev InputDevice to relay
        """
        try:
            async with DeviceRelay(
                device,
                self._gadget_manager,
                grab_device=self._grab_devices,
                relaying_active=self._relaying_active,
                shortcut_toggler=self._shortcut_toggler,
            ) as relay:
                _logger.info(f"Activated {relay}")
                await relay.async_relay_events_loop()
        except (OSError, FileNotFoundError):
            _logger.info(f"Lost connection to {device}.")
        except Exception:
            _logger.exception(f"Unhandled exception in relay for {device}.")
        finally:
            self.remove_device(device.path)

    def _should_relay(self, device: InputDevice) -> bool:
        """
        Decide if a device should be relayed based on auto_discover,
        skip_name_prefixes, or user-specified device_identifiers.

        :param device: The input device to check
        :return: True if we should relay it, False otherwise
        :rtype: bool
        """
        name_lower = device.name.lower()
        if self._auto_discover:
            for prefix in self._skip_name_prefixes:
                if name_lower.startswith(prefix.lower()):
                    return False
            return True

        return any(identifier.matches(device) for identifier in self._device_ids)


class DeviceRelay:
    """
    Relay a single InputDevice's events to USB HID gadgets.

    - Optionally grabs the device exclusively.
    - Retries HID writes if they raise BlockingIOError.
    """

    def __init__(
        self,
        input_device: InputDevice,
        gadget_manager: GadgetManager,
        grab_device: bool = False,
        relaying_active: Optional[asyncio.Event] = None,
        shortcut_toggler: Optional["ShortcutToggler"] = None,
    ) -> None:
        """
        :param input_device: The evdev input device
        :param gadget_manager: Provides references to Keyboard, Mouse, ConsumerControl
        :param grab_device: Whether to grab the device for exclusive access
        :param relaying_active: asyncio.Event that indicates relaying is on/off
        :param shortcut_toggler: Optional handler for toggling relay via a shortcut
        """
        self._input_device = input_device
        self._gadget_manager = gadget_manager
        self._grab_device = grab_device
        self._relaying_active = relaying_active
        self._shortcut_toggler = shortcut_toggler

        self._currently_grabbed = False

        # Ctrl tap sequence detection for mouse movement patterns
        self._ctrl_tap_times: list[float] = []
        self._mouse_movement_task: Optional[asyncio.Task] = None
        self._mouse_movement_active = False

        # Load mouse movement configuration
        self._movement_config = self._load_movement_config()
        default_pattern = self._movement_config.get("default_pattern", "circle")

        # Support random pattern selection
        if default_pattern == "random":
            available_patterns = list(self._movement_config.get("patterns", {}).keys())
            # Exclude 'mix' from random selection as it already cycles through patterns
            available_patterns = [p for p in available_patterns if p != "mix"]
            self._current_pattern = random.choice(available_patterns) if available_patterns else "circle"
            _logger.info(f"Random pattern selected: {self._current_pattern}")
        else:
            self._current_pattern = default_pattern

        # For mix pattern: track timing
        self._mix_start_time: Optional[float] = None

        # For random pattern: track timing for pattern changes
        self._random_pattern_start_time: Optional[float] = None
        self._is_random_mode = (default_pattern == "random")

    def __str__(self) -> str:
        return f"relay for {self._input_device}"

    @property
    def input_device(self) -> InputDevice:
        """
        The underlying evdev InputDevice being relayed.

        :return: The InputDevice
        :rtype: InputDevice
        """
        return self._input_device

    async def __aenter__(self) -> "DeviceRelay":
        """
        Async context manager entry. Grabs the device if requested.

        :return: self
        """
        if self._grab_device:
            try:
                self._input_device.grab()
                self._currently_grabbed = True
            except Exception as ex:
                _logger.warning(f"Could not grab {self._input_device.path}: {ex}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """
        Async context manager exit. Ungrabs the device if we grabbed it.

        :return: False to propagate exceptions
        """
        if self._grab_device:
            try:
                self._input_device.ungrab()
                self._currently_grabbed = False
            except Exception as ex:
                _logger.warning(f"Unable to ungrab {self._input_device.path}: {ex}")
        return False

    async def async_relay_events_loop(self) -> None:
        """
        Continuously read events from the device and relay them
        to the USB HID gadgets. Stops when canceled or on error.

        :return: None
        """
        async for input_event in self._input_device.async_read_loop():
            event = categorize(input_event)

            if any(isinstance(event, ev_type) for ev_type in [KeyEvent, RelEvent]):
                _logger.debug(
                    f"Received {event} from {self._input_device.name} ({self._input_device.path})"
                )

            # Check for Ctrl tap sequence (run without blocking)
            if isinstance(event, KeyEvent):
                asyncio.create_task(self._check_ctrl_tap_sequence(event))

            if self._shortcut_toggler and isinstance(event, KeyEvent):
                self._shortcut_toggler.handle_key_event(event)

            active = self._relaying_active and self._relaying_active.is_set()

            # Dynamically grab/ungrab if relaying state changes
            if self._grab_device and active and not self._currently_grabbed:
                try:
                    self._input_device.grab()
                    self._currently_grabbed = True
                    _logger.debug(f"Grabbed {self._input_device}")
                except Exception as ex:
                    _logger.warning(f"Could not grab {self._input_device}: {ex}")

            elif self._grab_device and not active and self._currently_grabbed:
                try:
                    self._input_device.ungrab()
                    self._currently_grabbed = False
                    _logger.debug(f"Ungrabbed {self._input_device}")
                except Exception as ex:
                    _logger.warning(f"Could not ungrab {self._input_device}: {ex}")

            if not active:
                continue

            await self._process_event_with_retry(event)

    def _load_movement_config(self) -> dict[str, Any]:
        """Load mouse movement configuration from JSON file with fallback to defaults"""
        # Use the same directory as this relay.py file
        config_path = Path(__file__).parent / "mouse_patterns.json"

        try:
            if config_path.exists():
                with open(config_path, "r") as f:
                    config = json.load(f)
                _logger.info(f"Loaded mouse movement config from {config_path}")
                return config
            else:
                _logger.warning(
                    f"Config file {config_path} not found, using default configuration"
                )
        except json.JSONDecodeError as e:
            _logger.error(f"Invalid JSON in {config_path}: {e}, using defaults")
        except Exception as e:
            _logger.error(f"Error loading config from {config_path}: {e}, using defaults")

        return self._get_default_config()

    def _get_default_config(self) -> dict[str, Any]:
        """Return default mouse movement configuration with random ranges"""
        return {
            "default_pattern": "random",
            "random_pattern_change_interval": 20,
            "patterns": {
                "circle": {"radius": [5, 20], "steps": [20, 50], "delay": 0.05},
                "zigzag": {"width": [10, 30], "height": [5, 15], "steps": [30, 60], "delay": 0.05},
                "square": {"size": [10, 25], "steps": [30, 60], "delay": 0.05},
                "mix": {
                    "patterns": ["circle", "zigzag", "square"],
                    "duration_per_pattern": 10,
                    "delay": 0.05,
                },
            },
        }

    def _resolve_config_value(self, value: Union[int, float, list]) -> Union[int, float]:
        """Resolve configuration value - if it's a list [min, max], return random value in range"""
        if isinstance(value, list) and len(value) == 2:
            min_val, max_val = value
            if isinstance(min_val, int) and isinstance(max_val, int):
                return random.randint(min_val, max_val)
            else:
                return random.uniform(float(min_val), float(max_val))
        return value

    def _is_ctrl_key(self, event: KeyEvent) -> bool:
        """Check if the event is a Ctrl key press"""
        return event.scancode in (ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL)

    async def _check_ctrl_tap_sequence(self, event: KeyEvent) -> None:
        """Detect 5 Ctrl taps within 3 seconds to toggle mouse movement"""
        if not self._is_ctrl_key(event):
            return

        # Only detect key down events
        if event.keystate != KeyEvent.key_down:
            return

        current_time = time.time()
        tap_window = 3.0  # seconds
        required_taps = 5

        # Add current tap time
        self._ctrl_tap_times.append(current_time)

        # Remove taps older than the window
        self._ctrl_tap_times = [
            t for t in self._ctrl_tap_times if current_time - t <= tap_window
        ]

        tap_count = len(self._ctrl_tap_times)
        _logger.debug(
            f"Ctrl tap detected! Count: {tap_count}/{required_taps} in last {tap_window}s"
        )

        # Check if we have enough taps
        if tap_count >= required_taps:
            _logger.warning(
                f"🎯 Ctrl tap sequence detected! {tap_count} taps in {tap_window} seconds"
            )
            await self._toggle_mouse_movement()
            # Clear the tap times after triggering
            self._ctrl_tap_times.clear()

    async def _toggle_mouse_movement(self) -> None:
        """Toggle the mouse movement on/off"""
        if self._mouse_movement_active:
            # Stop the mouse movement
            _logger.warning(
                f"🔴 Ctrl tap sequence detected - STOPPING {self._current_pattern} mouse movement"
            )
            self._mouse_movement_active = False
            if self._mouse_movement_task and not self._mouse_movement_task.done():
                self._mouse_movement_task.cancel()
                try:
                    await self._mouse_movement_task
                except CancelledError:
                    pass
            self._mouse_movement_task = None
            self._mix_start_time = None
            _logger.warning(f"🖱️  {self._current_pattern.capitalize()} mouse movement stopped")
        else:
            # Start the mouse movement
            _logger.warning(
                f"🟢 Ctrl tap sequence detected - STARTING {self._current_pattern} mouse movement"
            )

            # Release all keyboard keys to prevent stuck keys (especially Ctrl)
            keyboard = self._gadget_manager.get_keyboard()
            if keyboard is not None:
                try:
                    keyboard.release_all()
                    _logger.debug(
                        "Released all keyboard keys before starting mouse movement"
                    )
                except Exception as e:
                    _logger.warning(f"Failed to release keyboard keys: {e}")

            self._mouse_movement_active = True
            self._mouse_movement_task = asyncio.create_task(self._mouse_movement_loop())
            _logger.warning(f"🖱️  {self._current_pattern.capitalize()} mouse movement started")

    def _calculate_circle_delta(
        self, step: int, prev_x: float, prev_y: float, config: dict[str, Any], radius: Optional[float] = None, steps: Optional[int] = None
    ) -> tuple[int, int, float, float]:
        """Calculate mouse delta for circular movement"""
        if radius is None:
            radius = self._resolve_config_value(config.get("radius", 10))
        if steps is None:
            steps = int(self._resolve_config_value(config.get("steps", 36)))

        angle = 2 * math.pi * step / steps
        curr_x = radius * math.cos(angle)
        curr_y = radius * math.sin(angle)

        if step == 0:
            return 0, 0, curr_x, curr_y

        delta_x = int(curr_x - prev_x)
        delta_y = int(curr_y - prev_y)
        return delta_x, delta_y, curr_x, curr_y

    def _calculate_zigzag_delta(
        self, step: int, prev_x: float, prev_y: float, config: dict[str, Any], width: Optional[float] = None, height: Optional[float] = None, steps: Optional[int] = None
    ) -> tuple[int, int, float, float]:
        """Calculate mouse delta for horizontal zigzag movement"""
        if width is None:
            width = self._resolve_config_value(config.get("width", 20))
        if height is None:
            height = self._resolve_config_value(config.get("height", 10))
        if steps is None:
            steps = int(self._resolve_config_value(config.get("steps", 40)))

        # Divide steps into rows (vertical segments)
        steps_per_row = max(1, steps // int(height))
        row = step // steps_per_row
        position_in_row = step % steps_per_row

        # Alternate direction for each row (horizontal zigzag)
        direction = 1 if row % 2 == 0 else -1
        progress = position_in_row / steps_per_row

        curr_x = width * progress * direction
        curr_y = row * (height / max(1, height - 1)) if height > 1 else 0

        if step == 0:
            return 0, 0, curr_x, curr_y

        delta_x = int(curr_x - prev_x)
        delta_y = int(curr_y - prev_y)
        return delta_x, delta_y, curr_x, curr_y

    def _calculate_square_delta(
        self, step: int, prev_x: float, prev_y: float, config: dict[str, Any],
        size: Optional[float] = None, steps: Optional[int] = None
    ) -> tuple[int, int, float, float]:
        """Calculate mouse delta for square movement"""
        if size is None:
            size = self._resolve_config_value(config.get("size", 15))
        if steps is None:
            steps = int(self._resolve_config_value(config.get("steps", 40)))

        # Divide steps into 4 sides
        steps_per_side = steps // 4
        side = step // steps_per_side  # 0=top, 1=right, 2=bottom, 3=left
        position = step % steps_per_side
        progress = position / steps_per_side if steps_per_side > 0 else 0

        if side == 0:  # Top side: left to right
            curr_x = size * progress
            curr_y = 0
        elif side == 1:  # Right side: top to bottom
            curr_x = size
            curr_y = size * progress
        elif side == 2:  # Bottom side: right to left
            curr_x = size * (1 - progress)
            curr_y = size
        else:  # Left side: bottom to top
            curr_x = 0
            curr_y = size * (1 - progress)

        if step == 0:
            return 0, 0, curr_x, curr_y

        delta_x = int(curr_x - prev_x)
        delta_y = int(curr_y - prev_y)
        return delta_x, delta_y, curr_x, curr_y

    def _get_current_mix_pattern(self, config: dict[str, Any]) -> str:
        """Determine which pattern to use in mix mode based on elapsed time"""
        patterns = config.get("patterns", ["circle"])
        duration = config.get("duration_per_pattern", 10)

        if self._mix_start_time is None:
            self._mix_start_time = time.time()

        elapsed = time.time() - self._mix_start_time
        pattern_index = int(elapsed / duration) % len(patterns)
        return patterns[pattern_index]

    async def _mouse_movement_loop(self) -> None:
        """Move mouse in configured pattern continuously"""
        pattern_name = self._current_pattern

        # Initialize random pattern start time if in random mode
        if self._is_random_mode and self._random_pattern_start_time is None:
            self._random_pattern_start_time = time.time()

        _logger.info(
            f"🖱️  Mouse movement loop starting with pattern '{pattern_name}'. "
            f"Random mode: {self._is_random_mode}. "
            f"Mouse gadget status: {self._gadget_manager.get_mouse() is not None}"
        )

        mouse = self._gadget_manager.get_mouse()
        if mouse is None:
            _logger.error("🖱️  Mouse gadget is None!")
            self._mouse_movement_active = False
            return

        # Get pattern configuration
        patterns_config = self._movement_config.get("patterns", {})
        pattern_config = patterns_config.get(pattern_name, {})

        if not pattern_config:
            _logger.error(f"🖱️  Pattern '{pattern_name}' not found in config!")
            self._mouse_movement_active = False
            return

        cycle = 0
        consecutive_errors = 0
        max_consecutive_errors = 5
        delay = pattern_config.get("delay", 0.05)

        # Initialize position tracking
        prev_x, prev_y = 0.0, 0.0
        current_mix_pattern = None

        try:
            while self._mouse_movement_active:
                cycle += 1

                # Check if we need to switch pattern in random mode
                if self._is_random_mode and self._random_pattern_start_time is not None:
                    elapsed = time.time() - self._random_pattern_start_time
                    interval = self._movement_config.get("random_pattern_change_interval", 20)
                    if elapsed >= interval:
                        # Select a new random pattern
                        available_patterns = list(patterns_config.keys())
                        available_patterns = [p for p in available_patterns if p != "mix"]
                        if available_patterns:
                            old_pattern = pattern_name
                            pattern_name = random.choice(available_patterns)
                            pattern_config = patterns_config.get(pattern_name, {})
                            self._current_pattern = pattern_name
                            self._random_pattern_start_time = time.time()
                            _logger.info(f"🎲 Random mode: Switching pattern from '{old_pattern}' to '{pattern_name}'")

                # Resolve random values for this cycle
                resolved_params = {}
                if pattern_name == "circle":
                    resolved_params["radius"] = self._resolve_config_value(pattern_config.get("radius", 10))
                    resolved_params["steps"] = int(self._resolve_config_value(pattern_config.get("steps", 36)))
                elif pattern_name == "zigzag":
                    resolved_params["width"] = self._resolve_config_value(pattern_config.get("width", 20))
                    resolved_params["height"] = self._resolve_config_value(pattern_config.get("height", 10))
                    resolved_params["steps"] = int(self._resolve_config_value(pattern_config.get("steps", 40)))
                elif pattern_name == "square":
                    resolved_params["size"] = self._resolve_config_value(pattern_config.get("size", 15))
                    resolved_params["steps"] = int(self._resolve_config_value(pattern_config.get("steps", 40)))

                steps = resolved_params.get("steps", int(self._resolve_config_value(pattern_config.get("steps", 36))))

                # For mix pattern, determine current sub-pattern
                if pattern_name == "mix":
                    current_mix_pattern = self._get_current_mix_pattern(pattern_config)
                    mix_config = patterns_config.get(current_mix_pattern, {})

                    # Resolve random values for mix sub-pattern
                    if current_mix_pattern == "circle":
                        resolved_params["radius"] = self._resolve_config_value(mix_config.get("radius", 10))
                        resolved_params["steps"] = int(self._resolve_config_value(mix_config.get("steps", 36)))
                    elif current_mix_pattern == "zigzag":
                        resolved_params["width"] = self._resolve_config_value(mix_config.get("width", 20))
                        resolved_params["height"] = self._resolve_config_value(mix_config.get("height", 10))
                        resolved_params["steps"] = int(self._resolve_config_value(mix_config.get("steps", 40)))
                    elif current_mix_pattern == "square":
                        resolved_params["size"] = self._resolve_config_value(mix_config.get("size", 15))
                        resolved_params["steps"] = int(self._resolve_config_value(mix_config.get("steps", 40)))

                    steps = resolved_params.get("steps", 36)
                    _logger.debug(
                        f"🖱️  Cycle {cycle}: Mix pattern using '{current_mix_pattern}' with params {resolved_params}"
                    )
                else:
                    _logger.debug(
                        f"🖱️  Cycle {cycle}: Starting {pattern_name} movement with params {resolved_params}"
                    )

                for step in range(steps):
                    if not self._mouse_movement_active:
                        break

                    # Calculate movement delta based on pattern
                    if pattern_name == "mix":
                        # Use the current mix sub-pattern
                        if current_mix_pattern == "circle":
                            delta_x, delta_y, prev_x, prev_y = self._calculate_circle_delta(
                                step, prev_x, prev_y, mix_config,
                                radius=resolved_params.get("radius"),
                                steps=resolved_params.get("steps")
                            )
                        elif current_mix_pattern == "zigzag":
                            delta_x, delta_y, prev_x, prev_y = self._calculate_zigzag_delta(
                                step, prev_x, prev_y, mix_config,
                                width=resolved_params.get("width"),
                                height=resolved_params.get("height"),
                                steps=resolved_params.get("steps")
                            )
                        elif current_mix_pattern == "square":
                            delta_x, delta_y, prev_x, prev_y = self._calculate_square_delta(
                                step, prev_x, prev_y, mix_config,
                                size=resolved_params.get("size"),
                                steps=resolved_params.get("steps")
                            )
                        else:
                            delta_x, delta_y = 0, 0
                    elif pattern_name == "circle":
                        delta_x, delta_y, prev_x, prev_y = self._calculate_circle_delta(
                            step, prev_x, prev_y, pattern_config,
                            radius=resolved_params.get("radius"),
                            steps=resolved_params.get("steps")
                        )
                    elif pattern_name == "zigzag":
                        delta_x, delta_y, prev_x, prev_y = self._calculate_zigzag_delta(
                            step, prev_x, prev_y, pattern_config,
                            width=resolved_params.get("width"),
                            height=resolved_params.get("height"),
                            steps=resolved_params.get("steps")
                        )
                    elif pattern_name == "square":
                        delta_x, delta_y, prev_x, prev_y = self._calculate_square_delta(
                            step, prev_x, prev_y, pattern_config,
                            size=resolved_params.get("size"),
                            steps=resolved_params.get("steps")
                        )
                    else:
                        _logger.error(f"🖱️  Unknown pattern: {pattern_name}")
                        delta_x, delta_y = 0, 0

                    # Skip first step (just initialization)
                    if step == 0:
                        continue

                    try:
                        mouse.move(delta_x, delta_y, 0)
                        consecutive_errors = 0  # Reset error counter on success
                        _logger.debug(
                            f"🖱️  Step {step}/{steps}: Moved ({delta_x}, {delta_y})"
                        )
                    except BrokenPipeError as e:
                        consecutive_errors += 1
                        _logger.error(f"🖱️  USB connection error moving mouse: {e}")
                        if consecutive_errors >= max_consecutive_errors:
                            _logger.critical(
                                f"⚠️  CRITICAL: USB mouse gadget connection lost! "
                                f"Failed {consecutive_errors} consecutive times. "
                                f"Please check USB connection.\n"
                                f"Stopping {pattern_name} mouse movement."
                            )
                            self._mouse_movement_active = False
                            break
                    except Exception as e:
                        consecutive_errors += 1
                        _logger.error(f"🖱️  Failed moving mouse: {e}")
                        if consecutive_errors >= max_consecutive_errors:
                            _logger.error(
                                f"⚠️  Too many consecutive errors ({consecutive_errors}). Stopping."
                            )
                            self._mouse_movement_active = False
                            break

                    await asyncio.sleep(delay)

                # Reset position for next cycle
                prev_x, prev_y = 0.0, 0.0
                _logger.debug(f"🖱️  Cycle {cycle} complete")

        except CancelledError:
            _logger.warning(
                f"🖱️  Mouse movement loop cancelled (completed {cycle} cycles)"
            )
            raise
        except Exception as e:
            _logger.exception(f"🖱️  Mouse movement loop error: {e}")
            raise
        finally:
            if consecutive_errors >= max_consecutive_errors:
                _logger.warning(
                    f"🖱️  Mouse movement loop stopped due to USB connection errors"
                )

    async def _process_event_with_retry(self, event: InputEvent) -> None:
        """
        Attempt to relay the given event to the appropriate HID gadget.
        Retry on BlockingIOError up to 2 times.

        :param event: The InputEvent to process
        """
        max_tries = 3
        retry_delay = 0.1
        for attempt in range(1, max_tries + 1):
            try:
                relay_event(event, self._gadget_manager)
                return
            except BlockingIOError:
                if attempt < max_tries:
                    _logger.debug(f"HID write blocked ({attempt}/{max_tries})")
                    await asyncio.sleep(retry_delay)
                else:
                    _logger.warning(f"HID write blocked ({attempt}/{max_tries})")
            except BrokenPipeError:
                _logger.warning(
                    "BrokenPipeError: USB cable likely disconnected or power-only. "
                    "Pausing relay.\nSee: "
                    "https://github.com/quaxalber/bluetooth_2_usb?tab=readme-ov-file#7-troubleshooting"
                )
                if self._relaying_active:
                    self._relaying_active.clear()
                return
            except Exception:
                _logger.exception(f"Error processing {event}")
                return


class DeviceIdentifier:
    """
    Identifies an input device by path (/dev/input/eventX), MAC address,
    or a substring of the device name.
    """

    def __init__(self, device_identifier: str) -> None:
        """
        :param device_identifier: Path, MAC, or name fragment
        """
        self._value = device_identifier
        self._type = self._determine_identifier_type()
        self._normalized_value = self._normalize_identifier()

    def __str__(self) -> str:
        return f'{self._type} "{self._value}"'

    def _determine_identifier_type(self) -> str:
        if re.match(r"^/dev/input/event.*$", self._value):
            return "path"
        if re.match(r"^([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})$", self._value):
            return "mac"
        return "name"

    def _normalize_identifier(self) -> str:
        if self._type == "path":
            return self._value
        if self._type == "mac":
            return self._value.lower().replace("-", ":")
        return self._value.lower()

    def matches(self, device: InputDevice) -> bool:
        """
        Check whether this identifier matches the given evdev InputDevice.

        :param device: An evdev InputDevice to compare
        :return: True if matched, False otherwise
        :rtype: bool
        """
        if self._type == "path":
            return self._value == device.path
        if self._type == "mac":
            return self._normalized_value == (device.uniq or "").lower()
        return self._normalized_value in device.name.lower()


async def async_list_input_devices() -> list[InputDevice]:
    """
    Return a list of available /dev/input/event* devices.

    :return: List of InputDevice objects
    :rtype: list[InputDevice]
    """
    try:
        return [InputDevice(path) for path in list_devices()]
    except (OSError, FileNotFoundError) as ex:
        _logger.critical(f"Failed listing devices: {ex}")
        return []
    except Exception:
        _logger.exception("Unexpected error listing devices")
        return []


def relay_event(event: InputEvent, gadget_manager: GadgetManager) -> None:
    """
    Relay the given event to the appropriate USB HID device.

    :param event: The evdev InputEvent
    :param gadget_manager: GadgetManager with references to HID devices
    :raises BlockingIOError: If HID device write is blocked
    """
    if isinstance(event, RelEvent):
        move_mouse(event, gadget_manager)
    elif isinstance(event, KeyEvent):
        send_key_event(event, gadget_manager)


def move_mouse(event: RelEvent, gadget_manager: GadgetManager) -> None:
    """
    Relay relative mouse movement events to the USB HID Mouse gadget.

    :param event: A RelEvent describing the movement
    :param gadget_manager: GadgetManager with Mouse reference
    :raises RuntimeError: If Mouse gadget is not available
    """
    mouse = gadget_manager.get_mouse()
    if mouse is None:
        raise RuntimeError("Mouse gadget not initialized or manager not enabled.")

    x, y, mwheel = get_mouse_movement(event)
    mouse.move(x, y, mwheel)


def send_key_event(event: KeyEvent, gadget_manager: GadgetManager) -> None:
    """
    Relay a key event (press/release) to the appropriate HID gadget.

    :param event: The KeyEvent to process
    :param gadget_manager: GadgetManager with references to the HID devices
    :raises RuntimeError: If no appropriate HID gadget is available
    """
    key_id, key_name = evdev_to_usb_hid(event)
    if key_id is None or key_name is None:
        return

    output_gadget = get_output_device(event, gadget_manager)
    if output_gadget is None:
        raise RuntimeError("No appropriate USB gadget found (manager not enabled?).")

    if event.keystate == KeyEvent.key_down:
        _logger.debug(f"Pressing {key_name} (0x{key_id:02X}) via {output_gadget}")
        output_gadget.press(key_id)
    elif event.keystate == KeyEvent.key_up:
        _logger.debug(f"Releasing {key_name} (0x{key_id:02X}) via {output_gadget}")
        output_gadget.release(key_id)


def get_output_device(
    event: KeyEvent, gadget_manager: GadgetManager
) -> Union[ConsumerControl, Keyboard, Mouse, None]:
    """
    Determine which HID gadget to target for the given key event.

    :param event: The KeyEvent to process
    :param gadget_manager: GadgetManager for HID references
    :return: A ConsumerControl, Mouse, or Keyboard object, or None if not found
    """
    if is_consumer_key(event):
        return gadget_manager.get_consumer()
    elif is_mouse_button(event):
        return gadget_manager.get_mouse()
    return gadget_manager.get_keyboard()


class UdcStateMonitor:
    """
    Monitors the UDC (USB Device Controller) state and
    sets/clears an Event when the device is configured or not.
    """

    def __init__(
        self,
        relaying_active: asyncio.Event,
        udc_path: Path = Path("/sys/class/udc/20980000.usb/state"),
        poll_interval: float = 0.5,
    ) -> None:
        """
        :param relaying_active: Event controlling whether relaying is active
        :param udc_path: Path to the UDC state file
        :param poll_interval: Interval (seconds) to re-check the UDC state
        """
        self._relaying_active = relaying_active
        self.udc_path = udc_path
        self.poll_interval = poll_interval

        self._stop = False
        self._task: Optional[asyncio.Task] = None
        self._last_state: Optional[str] = None

        if not self.udc_path.is_file():
            _logger.warning(
                f"UDC state file {self.udc_path} not found. Cable monitoring may be unavailable."
            )

    async def __aenter__(self):
        """
        Async context manager entry. Starts a background task to poll the UDC state.
        """
        self._stop = False
        self._task = asyncio.create_task(self._poll_state())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Async context manager exit. Cancels the polling task.
        """
        if self._task:
            self._stop = True
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        return False

    async def _poll_state(self):
        while not self._stop:
            new_state = self._read_udc_state()
            if new_state != self._last_state:
                self._handle_state_change(new_state)
                self._last_state = new_state
            await asyncio.sleep(self.poll_interval)

    def _read_udc_state(self) -> str:
        """
        Read the UDC state file. If not found, treat as "not_attached".

        :return: The current UDC state (e.g. "configured")
        :rtype: str
        """
        try:
            with open(self.udc_path, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            return "not_attached"

    def _handle_state_change(self, new_state: str):
        """
        Handle a change in the UDC state. If "configured", set relaying_active.
        Otherwise clear it.

        :param new_state: The new UDC state
        """
        _logger.debug(f"UDC state changed to '{new_state}'")

        if new_state == "configured":
            self._relaying_active.set()
        else:
            self._relaying_active.clear()


class UdevEventMonitor:
    """
    Monitors udev for /dev/input/event* add/remove events and
    notifies the RelayController.
    """

    def __init__(self, relay_controller: RelayController) -> None:
        """
        :param relay_controller: The RelayController to add/remove devices
        :param loop: The asyncio event loop
        """
        self.relay_controller = relay_controller

        self.context = pyudev.Context()
        self.monitor = pyudev.Monitor.from_netlink(self.context)
        self.monitor.filter_by("input")
        self.observer = pyudev.MonitorObserver(self.monitor, self._udev_event_callback)

    async def __aenter__(self):
        """
        Async context manager entry. Starts the pyudev monitor observer.
        """
        self.observer.start()
        _logger.debug("UdevEventMonitor started observer.")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Async context manager exit. Stops the pyudev monitor observer.
        """
        self.observer.stop()
        _logger.debug("UdevEventMonitor stopped observer.")
        return False

    def _udev_event_callback(self, action: str, device: pyudev.Device) -> None:
        """
        pyudev callback for input devices.

        :param action: "add" or "remove"
        :param device: The pyudev device
        """
        device_node = device.device_node
        if not device_node or not device_node.startswith("/dev/input/event"):
            return

        if action == "add":
            _logger.debug(f"UdevEventMonitor: Added input => {device_node}")
            self.relay_controller.add_device(device_node)
        elif action == "remove":
            _logger.debug(f"UdevEventMonitor: Removed input => {device_node}")
            self.relay_controller.remove_device(device_node)
