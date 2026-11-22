"""
Aggies Invent (ExxonMobil Challenge) — Arduino Button Control Panel Reader
Reads physical push buttons on Arduino pins 2-5 and relays actions to the
Rig Power Advisory SCADA dashboard.

Channels:
  Channel 1 (Pin 2): Shale Shaker & Agitator
  Channel 2 (Pin 3): Rig Lighting & Auxiliary Compressors
  Channel 3 (Pin 4): Ventilation & Motor Blower
  Channel 4 (Pin 5): Skipped
"""

import json
import re
import sys
import threading
import time
import urllib.request
import serial
import serial.tools.list_ports

PORT = "COM4"
BAUD = 9600
SCADA_URL = "http://localhost:8000/api/command"
stop_flag = threading.Event()

BUTTON_MAPPING = {
    1: {"name": "Shale Shaker & Agitator", "implement": "shaker", "pin": 2},
    2: {"name": "Rig Lighting & Auxiliary Compressors", "implement": "lights", "pin": 3},
    3: {"name": "Ventilation & Motor Blower", "implement": "fan", "pin": 4},
    4: {"name": "Channel 4 (Skipped)", "implement": None, "pin": 5},
}


def find_serial_port() -> str:
    """Finds COM4 or auto-detects an available USB serial device."""
    try:
        ports = list(serial.tools.list_ports.comports())
        for p in ports:
            if "COM4" in p.device.upper():
                return p.device
        for p in ports:
            desc = (p.description or "").lower()
            dev = (p.device or "").lower()
            if any(k in desc or k in dev for k in ["arduino", "ch340", "cp210", "ftdi", "usb serial", "usbmodem", "usbserial", "ttyusb", "ttyacm"]):
                return p.device
    except Exception:
        pass
    return PORT


def send_to_scada(channel: int, explicit_state=None) -> None:
    """Relays the button event to the SCADA server."""
    payload = {"action": "button_press", "channel": channel}
    if explicit_state is not None:
        payload["state"] = explicit_state
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(SCADA_URL, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            pass
    except Exception as e:
        # Server might be offline or running on another port
        pass


def parse_button_event(raw_line: str):
    line = raw_line.strip()
    if not line:
        return None

    if line.startswith("{") and line.endswith("}"):
        try:
            d = json.loads(line)
            ch = d.get("button") or d.get("channel") or d.get("pin")
            if ch:
                ch = int(ch)
                if ch in [2, 3, 4, 5]:
                    ch = ch - 1
                return (ch, d.get("state"))
        except Exception:
            pass

    upper = line.upper()
    explicit_state = None
    if any(k in upper for k in [" OFF", ":OFF", "=OFF", " LOW", ":LOW", " STOPPED", ":0"]):
        explicit_state = False
    elif any(k in upper for k in [" ON", ":ON", "=ON", " HIGH", ":HIGH", " STARTED", ":1"]):
        explicit_state = True

    if line in ["1", "2", "3", "4"]:
        return (int(line), explicit_state)

    m = re.search(r'\bT([1-4])\b', line, re.IGNORECASE)
    if m:
        return (int(m.group(1)), explicit_state)

    m = re.search(r'\b(?:button|btn|b|switch|sw)\s*[:#]?\s*([1-4])\b', line, re.IGNORECASE)
    if m:
        return (int(m.group(1)), explicit_state)

    m = re.search(r'\b(?:channel|chan|ch)\s*[:#]?\s*([1-4])\b', line, re.IGNORECASE)
    if m:
        return (int(m.group(1)), explicit_state)

    m = re.search(r'\bpin\s*[:#]?\s*([2-5])\b', line, re.IGNORECASE)
    if m:
        return (int(m.group(1)) - 1, explicit_state)

    m = re.search(r'(?:press|input|key|read)\s*[:#]?\s*([1-4])\b', line, re.IGNORECASE)
    if m:
        return (int(m.group(1)), explicit_state)

    return None


def keyboard_loop(ser) -> None:
    while not stop_flag.is_set():
        try:
            key = input().strip()
        except EOFError:
            break
        if key in {"1", "2", "3", "4"}:
            ch = int(key)
            info = BUTTON_MAPPING.get(ch, {})
            name = info.get("name", "Unknown")
            print(f"--> [KEYBOARD] Pressed {ch}: Controlling {name}")
            if ser:
                try:
                    ser.write(f"T{key}\n".encode("utf-8"))
                    ser.flush()
                except Exception:
                    pass
            send_to_scada(ch)


def main() -> None:
    port = find_serial_port()
    print("=" * 68)
    print("  EXXONMOBIL RIG CONTROL PANEL — ARDUINO BUTTON BRIDGE")
    print(f"  Target Serial Port: {port} @ {BAUD} baud")
    print("  Button Mappings:")
    print("    Button 1 (Pin 2): Shale Shaker & Agitator")
    print("    Button 2 (Pin 3): Rig Lighting & Auxiliary Compressors")
    print("    Button 3 (Pin 4): Ventilation & Motor Blower")
    print("    Button 4 (Pin 5): Skipped for now")
    print("  Type 1, 2, 3, 4 + Enter in this window to test without hardware.")
    print("=" * 68)

    ser = None
    try:
        ser = serial.Serial(port, BAUD, timeout=0.5)
        time.sleep(1.5)
        ser.reset_input_buffer()
        print(f"[STATUS] Connected to Arduino on {port}.\n")
    except Exception as e:
        print(f"[WARNING] Could not open {port}: {e}")
        print("[STATUS] Running in mock/keyboard mode (type 1-4 + Enter).\n")

    t = threading.Thread(target=keyboard_loop, args=(ser,), daemon=True)
    t.start()

    rx_buf = ""
    last_button_tokens = None
    last_press_time = {}

    try:
        while True:
            if ser and ser.is_open:
                chunk = ser.read(1024)
                if chunk:
                    if len(chunk) == 1 and chunk[0] in [1, 2, 3, 4, 5]:
                        ch = chunk[0] if chunk[0] < 5 else 4
                        info = BUTTON_MAPPING.get(ch, {})
                        print(f"[ARDUINO EVENT] Raw Byte {ch} -> {info.get('name')}")
                        send_to_scada(ch)
                        continue

                    text = chunk.decode("utf-8", errors="ignore")
                    rx_buf += text
                    while "\n" in rx_buf or "\r" in rx_buf:
                        m = re.search(r'[\r\n]', rx_buf)
                        if not m:
                            break
                        pos = m.start()
                        line = rx_buf[:pos].strip()
                        rx_buf = rx_buf[pos+1:].lstrip("\r\n")
                        if line:
                            if "," in line or ";" in line or "/" in line:
                                tokens = [t.strip().upper() for t in re.split(r'[,;/]+', line) if t.strip()]
                                if len(tokens) >= 2:
                                    now = time.time()
                                    if last_button_tokens is None:
                                        if len(set(tokens)) <= 1:
                                            last_button_tokens = tokens[:]
                                            print(f"[STATUS] Hardware baseline initialized: {last_button_tokens}")
                                            continue
                                        else:
                                            from collections import Counter
                                            maj = Counter(tokens).most_common(1)[0][0]
                                            last_button_tokens = [maj] * len(tokens)

                                    changed = []
                                    for idx in range(min(len(tokens), len(last_button_tokens))):
                                        if tokens[idx] != last_button_tokens[idx]:
                                            ch = idx + 1
                                            if (now - last_press_time.get(ch, 0)) >= 0.20:
                                                changed.append(ch)
                                                last_press_time[ch] = now

                                    last_button_tokens = tokens[:]

                                    for ch in changed:
                                        if ch in BUTTON_MAPPING:
                                            info = BUTTON_MAPPING[ch]
                                            name = info.get("name")
                                            imp = info.get("implement")
                                            if imp:
                                                print(f"  -> Hardware Button {ch} ({name}) toggling {imp}")
                                                send_to_scada(ch)
                                    continue

                            parsed = parse_button_event(line)
                            if parsed:
                                ch, st = parsed
                                now = time.time()
                                if (now - last_press_time.get(ch, 0)) >= 0.20:
                                    last_press_time[ch] = now
                                    info = BUTTON_MAPPING.get(ch, {})
                                    name = info.get("name", f"Channel {ch}")
                                    state_str = f" ({'ON' if st else 'OFF'})" if st is not None else " [TOGGLE]"
                                    print(f"[ARDUINO EVENT] Button {ch} -> {name}{state_str} (raw: '{line}')")
                                    send_to_scada(ch, st)
                            else:
                                print(f"[ARDUINO RAW] {line}")
                    if len(rx_buf) in [1, 2]:
                        stripped = rx_buf.strip()
                        if stripped in ["1", "2", "3", "4", "5"] or (stripped.upper().startswith("T") and len(stripped) == 2):
                            parsed = parse_button_event(stripped)
                            if parsed:
                                ch, st = parsed
                                now = time.time()
                                if (now - last_press_time.get(ch, 0)) >= 0.20:
                                    last_press_time[ch] = now
                                    info = BUTTON_MAPPING.get(ch, {})
                                    print(f"[ARDUINO EVENT] Pulse {ch} -> {info.get('name')}")
                                    send_to_scada(ch, st)
                            rx_buf = ""
            else:
                time.sleep(0.1)
    except KeyboardInterrupt:
        stop_flag.set()
        if ser:
            ser.close()
        print("\nExiting.")


if __name__ == "__main__":
    main()