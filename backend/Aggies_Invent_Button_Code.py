"""9600-baud Arduino button-panel bridge for the rig power SCADA."""

import json
import re
import time
import urllib.request

import serial
import serial.tools.list_ports

PORT = "COM4"
BAUD = 9600
SCADA_URL = "http://localhost:8000/api/command"
RECONNECT_SECONDS = 2.0

BUTTON_MAPPING = {
    1: {"name": "Solids Control & Mud Mixing Package", "implement": "shaker", "pin": 2},
    2: {"name": "Lighting, Air Compressors & Utility Aux", "implement": "lights", "pin": 3},
    3: {"name": "VFD House HVAC & Cooling Blowers", "implement": "fan", "pin": 4},
    4: {"name": "Channel 4 (Skipped)", "implement": None, "pin": 5},
}
ACTIVE_TOKENS = {"ON", "HIGH", "1", "TRUE", "ACTIVE", "STARTED"}
VALID_TOKENS = ACTIVE_TOKENS | {"OFF", "LOW", "0", "FALSE", "INACTIVE", "STOPPED"}


def find_serial_port() -> str:
    """Prefer COM4, then return the first recognized USB serial device."""
    try:
        ports = list(serial.tools.list_ports.comports())
        for port in ports:
            if "COM4" in port.device.upper():
                return port.device
        for port in ports:
            description = (port.description or "").lower()
            device = (port.device or "").lower()
            if any(key in description or key in device for key in (
                "arduino", "ch340", "cp210", "ftdi", "usb serial",
                "usbmodem", "usbserial", "ttyusb", "ttyacm",
            )):
                return port.device
    except Exception:
        pass
    return PORT


def send_to_scada(channel: int, explicit_state=None) -> None:
    payload = {"action": "button_press", "channel": channel}
    if explicit_state is not None:
        payload["state"] = bool(explicit_state)
    try:
        request = urllib.request.Request(
            SCADA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=0.8):
            pass
    except Exception:
        pass


def parse_button_event(raw_line: str):
    line = raw_line.strip()
    if not line:
        return None
    if line.startswith("{") and line.endswith("}"):
        try:
            data = json.loads(line)
            channel = data.get("button") or data.get("channel") or data.get("pin")
            if channel is not None:
                channel = int(channel)
                if channel in (2, 3, 4, 5):
                    channel -= 1
                return channel, data.get("state")
        except (ValueError, TypeError, json.JSONDecodeError):
            pass

    upper = line.upper()
    explicit_state = None
    if any(token in upper for token in (" OFF", ":OFF", "=OFF", " LOW", ":LOW", ":0")):
        explicit_state = False
    elif any(token in upper for token in (" ON", ":ON", "=ON", " HIGH", ":HIGH", ":1")):
        explicit_state = True

    if line in ("1", "2", "3", "4"):
        return int(line), explicit_state
    for pattern, pin_offset in (
        (r"\bT([1-4])\b", 0),
        (r"\b(?:button|btn|b|switch|sw|channel|chan|ch)\s*[:#]?\s*([1-4])\b", 0),
        (r"\bpin\s*[:#]?\s*([2-5])\b", -1),
    ):
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            return int(match.group(1)) + pin_offset, explicit_state
    return None


def process_line(line: str, previous_tokens, last_press_time):
    tokens = [token.strip().upper() for token in re.split(r"[,;/\s]+", line) if token.strip()]
    if len(tokens) >= 2 and all(token in VALID_TOKENS for token in tokens):
        for index, token in enumerate(tokens[:4]):
            channel = index + 1
            mapping = BUTTON_MAPPING[channel]
            if mapping["implement"] and (
                previous_tokens is None
                or index >= len(previous_tokens)
                or previous_tokens[index] != token
            ):
                state = token in ACTIVE_TOKENS
                print(f"[ARDUINO EVENT] Channel {channel} ({mapping['name']}) -> {'ON' if state else 'OFF'}")
                send_to_scada(channel, state)
        return tokens[:], last_press_time

    parsed = parse_button_event(line)
    if parsed:
        channel, state = parsed
        now = time.time()
        if 1 <= channel <= 4 and now - last_press_time.get(channel, 0.0) >= 0.2:
            last_press_time[channel] = now
            name = BUTTON_MAPPING[channel]["name"]
            print(f"[ARDUINO EVENT] Button {channel} -> {name} (raw: {line!r})")
            send_to_scada(channel, state)
    else:
        print(f"[ARDUINO RAW] {line}")
    return previous_tokens, last_press_time


def main() -> None:
    print("=" * 68)
    print("  EXXONMOBIL RIG CONTROL PANEL — ARDUINO BUTTON BRIDGE")
    print(f"  Serial Baud: {BAUD}")
    for channel, mapping in BUTTON_MAPPING.items():
        print(f"  Button {channel} (Pin {mapping['pin']}): {mapping['name']}")
    print("=" * 68)

    connection = None
    receive_buffer = ""
    previous_tokens = None
    last_press_time = {}
    try:
        while True:
            if not connection or not connection.is_open:
                port = find_serial_port()
                try:
                    print(f"[STATUS] Waiting for Arduino on {port} @ {BAUD} baud...")
                    connection = serial.Serial(port, BAUD, timeout=0.5)
                    time.sleep(1.5)
                    connection.reset_input_buffer()
                    receive_buffer = ""
                    previous_tokens = None
                    print(f"[STATUS] Connected to Arduino on {port}.")
                except (serial.SerialException, OSError) as error:
                    print(f"[WARNING] Could not open {port}: {error}. Retrying in 2 seconds...")
                    connection = None
                    time.sleep(RECONNECT_SECONDS)
                    continue

            try:
                chunk = connection.read(1024)
            except (serial.SerialException, OSError) as error:
                print(f"[WARNING] Arduino disconnected: {error}. Retrying in 2 seconds...")
                try:
                    connection.close()
                except Exception:
                    pass
                connection = None
                time.sleep(RECONNECT_SECONDS)
                continue

            if not chunk:
                continue
            if len(chunk) == 1 and chunk[0] in (1, 2, 3, 4, 5):
                channel = min(chunk[0], 4)
                send_to_scada(channel)
                continue

            receive_buffer += chunk.decode("utf-8", errors="ignore")
            while "\n" in receive_buffer or "\r" in receive_buffer:
                separator = re.search(r"[\r\n]", receive_buffer)
                if not separator:
                    break
                line = receive_buffer[:separator.start()].strip()
                receive_buffer = receive_buffer[separator.start() + 1:].lstrip("\r\n")
                if line:
                    previous_tokens, last_press_time = process_line(
                        line, previous_tokens, last_press_time
                    )
    except KeyboardInterrupt:
        if connection:
            connection.close()
        print("\nExiting.")


if __name__ == "__main__":
    main()
