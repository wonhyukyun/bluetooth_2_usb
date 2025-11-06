# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bluetooth to USB is a Python application that converts a Raspberry Pi into a HID (Human Interface Device) relay. It translates Bluetooth keyboard and mouse input to USB using Linux's USB gadget mode, allowing Bluetooth devices to function as if they were USB-connected devices.

**Key use cases:**
- Wake up sleeping devices with Bluetooth peripherals
- Access BIOS/GRUB menus before OS loads
- Use Bluetooth devices with systems lacking Bluetooth support (e.g., KVM switches)

**Special Features:**
- **Configurable Mouse Movement Patterns**: Press Ctrl 5 times within 3 seconds to toggle automatic mouse movement. Supports circle, zigzag, square, and mix patterns (cycles through all). Configure patterns via `mouse_patterns.json`. Useful for preventing screensavers or keeping systems active.

## Requirements

- **Platform**: Raspberry Pi Zero W(H), Zero 2 W, or Pi 4B/5 (models with USB OTG support)
- **OS**: Raspberry Pi OS (Bookworm-based or newer)
- **Python**: 3.11+ (required for TaskGroups)
- **Hardware**: USB OTG-capable port for device mode

## Development Setup

### Installation for Development

```bash
# Install prerequisites
sudo apt update && sudo apt install -y git python3.11 python3.11-venv python3-dev

# Clone and navigate to repository
cd ~ && git clone https://github.com/quaxalber/bluetooth_2_usb.git
cd bluetooth_2_usb

# Create virtual environment
python3.11 -m venv venv

# Install dependencies
venv/bin/pip3.11 install -r requirements.txt
```

### Running the Application

```bash
# List available input devices
sudo venv/bin/python3 bluetooth_2_usb.py --list_devices

# Run with auto-discovery (relay all devices)
sudo venv/bin/python3 bluetooth_2_usb.py --auto_discover --grab_devices

# Run with specific devices
sudo venv/bin/python3 bluetooth_2_usb.py --device_ids '/dev/input/event2,A1:B2:C3:D4:E5:F6'

# Run with debug logging
sudo venv/bin/python3 bluetooth_2_usb.py --auto_discover --debug

# Run with interrupt shortcut (toggle relay on/off)
sudo venv/bin/python3 bluetooth_2_usb.py --auto_discover --interrupt_shortcut CTRL+SHIFT+F12
```

### Service Management

```bash
# Install as systemd service
sudo ~/bluetooth_2_usb/scripts/install.sh

# Check service status
service bluetooth_2_usb status

# View service logs in real-time
journalctl -u bluetooth_2_usb.service -n 50 -f

# Stop service for manual testing
sudo service bluetooth_2_usb stop

# Restart service
sudo service bluetooth_2_usb restart

# Update to latest version
sudo ~/bluetooth_2_usb/scripts/update.sh

# Uninstall
sudo ~/bluetooth_2_usb/scripts/uninstall.sh
```

## Architecture

### Core Components

The application uses modern Python async/await patterns with asyncio TaskGroups for reliable concurrency:

1. **Main Entry Point** ([bluetooth_2_usb.py](bluetooth_2_usb.py))
   - Parses CLI arguments
   - Sets up logging and signal handlers
   - Initializes USB HID gadgets (keyboard, mouse, consumer control)
   - Orchestrates the main async event loop

2. **Relay System** ([src/bluetooth_2_usb/relay.py](src/bluetooth_2_usb/relay.py))
   - **GadgetManager**: Manages USB HID gadget devices (keyboard, mouse, consumer control)
   - **RelayController**: Manages lifecycle of per-device relay tasks using TaskGroups
   - **DeviceRelay**: Relays events from a single InputDevice to USB HID gadgets
   - **UdevEventMonitor**: Monitors for device add/remove events using pyudev
   - **UdcStateMonitor**: Monitors USB Device Controller state to detect cable connection/disconnection
   - **ShortcutToggler**: Handles global keyboard shortcuts to pause/resume relaying

3. **Event Translation** ([src/bluetooth_2_usb/evdev.py](src/bluetooth_2_usb/evdev.py))
   - Maps Linux evdev key codes to USB HID Usage IDs
   - Handles keyboard keys, mouse buttons, and 146 multimedia/consumer control keys
   - Distinguishes between keyboard, mouse, and consumer control events

4. **Argument Parsing** ([src/bluetooth_2_usb/args.py](src/bluetooth_2_usb/args.py))
   - Custom ArgumentParser with structured Arguments class
   - Handles device identifiers (path, MAC address, or name substring)

### Configurable Mouse Movement Patterns Feature

The DeviceRelay class includes a special feature to prevent screensavers and keep systems active with multiple movement patterns:

- **Trigger**: Press Ctrl key 5 times within 3 seconds on any connected Bluetooth keyboard
- **Action**: Toggles automatic mouse movement on/off
- **Configuration**: `src/bluetooth_2_usb/mouse_patterns.json` defines patterns and parameters
- **Supported Patterns**:
  - **Circle**: Moves in circular pattern (configurable radius, steps, delay)
  - **Zigzag**: Horizontal zigzag pattern (configurable width, height, steps)
  - **Square**: Traces a square shape (configurable size, steps)
  - **Mix**: Cycles through multiple patterns (configurable duration per pattern)
  - **Random**: Randomly selects pattern and sizes from specified ranges
- **Randomization Features**:
  - **Range Values**: All size parameters (radius, width, height, size, steps) can be specified as ranges `[min, max]`
  - **Random Pattern Selection**: Set `"default_pattern": "random"` to randomly select patterns
  - **Pattern Switching**: When in random mode, patterns automatically switch after `random_pattern_change_interval` seconds
  - **Random Sizes per Cycle**: New random values are selected from ranges for each movement cycle
  - **Example**: `"radius": [5, 20]` means radius will be randomly selected between 5 and 20 for each cycle
- **Default Configuration**:
  ```json
  {
    "default_pattern": "random",
    "random_pattern_change_interval": 20,
    "patterns": {
      "circle": {"radius": [5, 20], "steps": [20, 50], "delay": 0.05},
      "zigzag": {"width": [10, 30], "height": [5, 15], "steps": [30, 60], "delay": 0.05},
      "square": {"size": [10, 25], "steps": [30, 60], "delay": 0.05},
      "mix": {"patterns": ["circle", "zigzag", "square"], "duration_per_pattern": 10, "delay": 0.05}
    }
  }
  ```
- **Error Handling**: Automatically stops on USB connection errors after 5 consecutive failures, falls back to defaults if JSON missing
- **Use Cases**:
  - Keep remote desktop sessions alive with varied, natural-looking movements
  - Prevent screensaver activation during presentations
  - Maintain system activity without physical interaction
  - Test different movement patterns for specific use cases
  - Avoid detection by mimicking more organic mouse behavior

### Event Flow

1. Bluetooth devices connect to Raspberry Pi and appear as `/dev/input/eventX` devices
2. `UdevEventMonitor` detects new input devices and notifies `RelayController`
3. `RelayController` creates a `DeviceRelay` task for each matching device in a TaskGroup
4. `DeviceRelay` reads events from the evdev InputDevice using `async_read_loop()`
5. Events are categorized (KeyEvent, RelEvent) and translated to USB HID codes
6. USB HID writes are sent to the appropriate gadget (keyboard/mouse/consumer control)
7. On device disconnect or error, tasks are cancelled gracefully

### Key Design Patterns

- **Async Context Managers**: `DeviceRelay`, `UdevEventMonitor`, and `UdcStateMonitor` use `__aenter__`/`__aexit__` for resource management
- **TaskGroups**: Ensures all relay tasks are properly managed and cancelled together
- **Event-driven Architecture**: Uses asyncio Events (`relaying_active`) to coordinate pause/resume across all relays
- **Device Identification**: Flexible matching by path, MAC address, or device name substring
- **Retry Logic**: BlockingIOError on HID writes is retried up to 3 times with 0.1s delays

## Code Style

- Follow [PEP 8](https://pep8.org/) Python style guidelines
- Use [Black](https://black.readthedocs.io/) code formatter before committing
- Write meaningful variable and function names
- Keep functions small and focused with single responsibility
- Use type hints for function signatures
- Document classes and non-trivial functions with docstrings
- Follow OOP principles: encapsulation, appropriate inheritance, polymorphism

## Testing and Debugging

### Manual Testing

```bash
# Stop service and run manually with debug logging
{ sudo service bluetooth_2_usb stop && sudo venv/bin/python3 bluetooth_2_usb.py -gads CTRL+SHIFT+F12 ; } ; sudo service bluetooth_2_usb start

# Test with file logging enabled
sudo venv/bin/python3 bluetooth_2_usb.py --auto_discover --debug --log_to_file

# Check logs
tail -f /var/log/bluetooth_2_usb/bluetooth_2_usb.log
```

### Common Issues

- **Power issues**: Pi may reboot/crash if drawing insufficient power from USB. Use USB 3.0/USB-C ports or external power supply
- **Wrong port**: Pi 4B/5 must use USB-C power port (OTG), Pi Zero must use data port (not power port)
- **Device pairing**: Devices must be paired, trusted, connected, and not blocked in bluetoothctl
- **Cable issues**: Ensure USB cable supports data transfer, not just power

## Important Files

- [bluetooth_2_usb.py](bluetooth_2_usb.py) - Main entry point
- [src/bluetooth_2_usb/relay.py](src/bluetooth_2_usb/relay.py) - Core relay logic, event handling, and mouse movement patterns
- [src/bluetooth_2_usb/evdev.py](src/bluetooth_2_usb/evdev.py) - Event code translation mappings
- [src/bluetooth_2_usb/mouse_patterns.json](src/bluetooth_2_usb/mouse_patterns.json) - Mouse movement pattern configuration (circle, zigzag, square, mix, random)
- [bluetooth_2_usb.service](bluetooth_2_usb.service) - systemd service configuration
- [bluetooth_2_usb.sh](bluetooth_2_usb.sh) - Wrapper script for venv execution
- [requirements.txt](requirements.txt) - Python dependencies
- [scripts/install.sh](scripts/install.sh) - Installation script with system configuration

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed contribution guidelines including:
- Code formatting with Black
- OOP best practices
- Pull request workflow
- Issue reporting guidelines
