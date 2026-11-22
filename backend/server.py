"""
Aggies Invent (ExxonMobil Challenge) - Automated Rig Power Management
Backend Server: FastAPI + WebSockets + PySerial Bridge + Digital Twin Power Model
"""

import argparse
import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Set, Tuple

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("RigServer")

# Monitored Rig Power Distribution Nameplate Capacity (kW) — 3.0 MW Total Rated
RATED_CAPACITY_KW: Dict[str, float] = {
    "bop": 40.0,
    "rotary": 2400.0,
    "shaker": 240.0,
    "fan": 180.0,
    "lights": 140.0,
}

# Nominal Continuous Operating Draw when Energized (kW)
NOMINAL_LOAD_KW: Dict[str, float] = {
    "bop": 20.0,
    "rotary": 2000.0,
    "shaker": 215.0,
    "fan": 145.0,
    "lights": 120.0,
}

# Backward-compatible alias for existing integrations.
RATINGS = NOMINAL_LOAD_KW
CONNECTED_CAPACITY_KW: float = sum(RATED_CAPACITY_KW.values())

# Environmental / Economic Multipliers
DIESEL_PER_KWH = 0.26       # L / kWh
COST_PER_LITER = 1.00       # $ / L
CO2_PER_LITER = 2.68        # kg CO2 / L

# Rig Operational States
RIG_STATES = ["ACTIVE_DRILLING", "PIPE_CONNECTION", "TRIPPING", "IDLE_STANDBY"]

# -----------------------------------------------------------------------------
# HARDWARE CONTROL PANEL BUTTON MAPPINGS (Arduino Pins 2-5)
# -----------------------------------------------------------------------------
# Button 1 -> Shale Shaker & Mud Agitator (FDR-03)
# Button 2 -> Rig Lighting & Auxiliary Compressors (FDR-05)
# Button 3 -> Ventilation & Motor Blower (FDR-04)
# Button 4 -> Skipped for now
BUTTON_MAP: Dict[int, Optional[str]] = {
    1: "shaker",   # Channel 1 (Pin 2): Shale Shaker
    2: "lights",   # Channel 2 (Pin 3): Rig Lighting & Auxiliary Compressors
    3: "fan",      # Channel 3 (Pin 4): Ventilation & Motor Blower
    4: None,       # Channel 4 (Pin 5): Skipped
}

IMPLEMENT_NAMES: Dict[str, str] = {
    "bop": "Well Control, BOP & Safety Controls",
    "rotary": "Top Drive System (Primary Rotary)",
    "shaker": "Solids Control & Mud Mixing Package",
    "fan": "VFD House HVAC & Cooling Blowers",
    "lights": "Lighting, Air Compressors & Utility Aux",
}

IMPLEMENT_SUBTITLES: Dict[str, str] = {
    "bop": "40 kW Rated · Accumulator Standby & Gas Detection",
    "rotary": "2,400 kW AC Top Drive Motor (2.4 MW)",
    "shaker": "240 kW Rated · Shakers, Agitators & Centrifuge",
    "fan": "180 kW Rated · PCR/VFD Cooling & Motor Blowers",
    "lights": "140 kW Rated · Yard Lighting, Compressors & Utility Aux",
}

IMPLEMENT_FEEDERS: Dict[str, str] = {
    "bop": "FDR-01", "rotary": "FDR-02", "shaker": "FDR-03",
    "fan": "FDR-04", "lights": "FDR-05",
}


def parse_button_event(raw_line: str) -> Optional[Tuple[int, Optional[bool]]]:
    """
    Parses incoming serial lines from the Arduino button control panel.
    Returns: (channel, explicit_state)
      channel: 1, 2, 3, or 4
      explicit_state: True (ON), False (OFF), or None (Toggle)
    """
    if not raw_line:
        return None

    # Handle binary bytes e.g. b'\x01'
    if len(raw_line) == 1 and ord(raw_line) in [1, 2, 3, 4, 5]:
        val = ord(raw_line)
        if val in [2, 3, 4, 5]:
            return (val - 1, None)
        return (val, None)

    line = raw_line.strip()
    if not line:
        return None

    # Check for JSON: {"button": 1, "state": "ON"} or {"channel": 1}
    if line.startswith("{") and line.endswith("}"):
        try:
            data = json.loads(line)
            ch = data.get("button") or data.get("channel") or data.get("pin")
            if ch is not None:
                ch = int(ch)
                if ch in [2, 3, 4, 5]:  # Pin number -> channel number
                    ch = ch - 1
                if 1 <= ch <= 4:
                    st = data.get("state")
                    if isinstance(st, bool):
                        return (ch, st)
                    elif isinstance(st, (int, float)):
                        return (ch, bool(st))
                    elif isinstance(st, str):
                        st_upper = st.upper()
                        if any(k in st_upper for k in ["ON", "HIGH", "PRESS", "CLOSED", "1"]):
                            return (ch, True)
                        elif any(k in st_upper for k in ["OFF", "LOW", "RELEASE", "OPEN", "0"]):
                            return (ch, False)
                    return (ch, None)
            if data.get("override"):
                return (1, None)
            if data.get("mode_btn_pressed"):
                return (2, None)
        except Exception:
            pass

    # Detect explicit ON/OFF keywords if present
    upper = line.upper()
    explicit_state = None
    if any(k in upper for k in [" OFF", ":OFF", "=OFF", " LOW", ":LOW", "=LOW", " DE-ENERGIZED", " STOPPED", ":0", "=0", " RELEASE"]):
        explicit_state = False
    elif any(k in upper for k in [" ON", ":ON", "=ON", " HIGH", ":HIGH", "=HIGH", " ENERGIZED", " STARTED", ":1", "=1", " PRESS"]):
        explicit_state = True

    # 1. Exact single digit: "1", "2", "3", "4"
    if line in ["1", "2", "3", "4"]:
        return (int(line), explicit_state)
    if line == "5":
        return (4, explicit_state)

    # 2. "T1", "T2", "T3", "T4" (matches keyboard_loop format)
    m = re.search(r'\bT([1-4])\b', line, re.IGNORECASE)
    if m:
        return (int(m.group(1)), explicit_state)

    # 3. "Pin 2", "Pin 3", "Pin 4", "Pin 5", "P2", "D2"
    m = re.search(r'\b(?:pin|p|d)\s*[:#]?\s*([2-5])\b', line, re.IGNORECASE)
    if m:
        pin = int(m.group(1))
        return (pin - 1, explicit_state)

    # 4. "Button 1", "Btn 2", "B3", "SW1", "Switch 2", "Key 1"
    m = re.search(r'\b(?:button|btn|b|switch|sw|key)\s*[:#]?\s*([1-4])\b', line, re.IGNORECASE)
    if m:
        return (int(m.group(1)), explicit_state)

    # 5. "Channel 1", "CH2", "CH:3"
    m = re.search(r'\b(?:channel|chan|ch)\s*[:#]?\s*([1-4])\b', line, re.IGNORECASE)
    if m:
        return (int(m.group(1)), explicit_state)

    # 6. Standalone digit 1-4 in line (e.g. "PRESS 1", "INPUT: 2")
    m = re.search(r'(?:press|input|key|read|val)\s*[:#]?\s*([1-4])\b', line, re.IGNORECASE)
    if m:
        return (int(m.group(1)), explicit_state)

    # 7. Fallback prefix: e.g. "1: HIGH", "2=ON"
    m = re.match(r'^([1-4])[\s:=-]', line)
    if m:
        return (int(m.group(1)), explicit_state)

    return None

class DigitalTwinRig:
    def __init__(self):
        # Operational mode (Decision Support / Advisory System)
        self.rig_state: str = "ACTIVE_DRILLING"
        self.hardware_connected: bool = False

        # Equipment On/Off states (1 = ON, 0 = OFF)
        self.implements: Dict[str, int] = {
            "bop": 1,        # Permanently 1 (Locked ON)
            "rotary": 1,
            "shaker": 1,
            "fan": 1,
            "lights": 1,
        }

        # Sensor inputs
        self.pot_raw: int = 0
        self.pre_spike_pot: int = 0
        self.torque_spike: bool = False
        self.simulated_spike_end_time: float = 0.0
        self.manual_override: bool = False
        self.failsafe: bool = False
        self.auto_saver_active: bool = False

        # Energy & Emissions Metrics
        self.base_kw: float = sum(NOMINAL_LOAD_KW.values())
        self.actual_kw: float = sum(NOMINAL_LOAD_KW.values())
        self.saved_kw: float = 0.0
        self.dynamic_torque_kw: float = 0.0
        self.overload_active: bool = False
        self.high_load_warning: bool = False

        # Cumulative counters
        self.last_update_time: float = time.time()
        self.kwh_saved_total: float = 0.0
        self.diesel_saved_l: float = 0.0
        self.cost_saved_usd: float = 0.0
        self.co2_saved_kg: float = 0.0

        # Rolling event log (max 100 entries)
        self.events: deque = deque(maxlen=100)
        self.add_event("SYSTEM", "Rig Power Advisory & Decision Support System initialized.")

    def add_event(self, category: str, message: str, level: str = "info"):
        timestamp_str = time.strftime("%H:%M:%S")
        entry = {
            "time": timestamp_str,
            "category": category,
            "message": message,
            "level": level,  # info, success, warning, danger
        }
        self.events.appendleft(entry)
        logger.info(f"[{category}] {message}")

    def get_advisories(self) -> List[Dict]:
        """Identify energized loads that are unnecessary in the current rig phase."""
        phase_rules = {
            "IDLE_STANDBY": {
                "shaker": ("Mud circulation suspended during extended standby", "Suggest local crew power down Feeder 03"),
                "fan": ("Post-cooldown blowers & non-essential VFD HVAC idle in standby", "Suggest local crew power down Feeder 04"),
                "lights": ("Non-essential daytime yard lighting & idle utility compressors running", "Suggest local crew power down Feeder 05 (Tier-1 floor lighting retained)"),
            },
            "TRIPPING": {
                "shaker": ("Mud circulation halted while tripping pipe", "Suggest local crew power down Feeder 03"),
                "lights": ("Non-essential yard lighting & idle utility compressors active during trip", "Suggest local crew power down Feeder 05"),
            },
            "PIPE_CONNECTION": {
                "lights": ("Idle utility compressors & non-essential yard lighting active during connection", "Suggest local crew shed non-essential Feeder 05 circuits"),
            },
        }
        rules = dict(phase_rules.get(self.rig_state, {}))
        if self.rig_state != "ACTIVE_DRILLING":
            rules["rotary"] = (
                f"Top drive energized while rig is in {self.rig_state}",
                "Isolate rotary drive Feeder 02",
            )

        advisories = []
        for implement, (reason, suggestion) in rules.items():
            if not self.implements.get(implement, 0):
                continue
            savings_kw = NOMINAL_LOAD_KW[implement]
            advisories.append({
                "implement": implement,
                "feeder": IMPLEMENT_FEEDERS[implement],
                "name": IMPLEMENT_NAMES[implement],
                "title": IMPLEMENT_NAMES[implement],
                "reason": reason,
                "suggestion": suggestion,
                "savings_kw": savings_kw,
                "cost_hr": round(savings_kw * DIESEL_PER_KWH * COST_PER_LITER, 2),
            })
        return advisories

    def set_rig_state(self, new_state: str):
        if new_state not in RIG_STATES:
            return
        if self.rig_state != new_state:
            old_state = self.rig_state
            self.rig_state = new_state
            self.add_event("MODE", f"Rig operational phase changed: {old_state} -> {new_state}", "info")

            # Rotary top drive is strictly controlled by operational phase:
            if new_state == "ACTIVE_DRILLING":
                self.implements["rotary"] = 1
                self.add_event("DRILLING", "Top drive energized for continuous drilling.", "info")
            else:
                # Rotary is physically stopped during connection, tripping, and idle
                self.implements["rotary"] = 0

            # Auxiliary loads (shaker, fan, lights):
            # Synchronization with physical Arduino lights MUST persist through phase switches!
            global last_arduino_tokens
            if last_arduino_tokens and len(last_arduino_tokens) >= 3:
                self.implements["shaker"] = 1 if is_token_active(last_arduino_tokens[0]) else 0
                self.implements["lights"] = 1 if is_token_active(last_arduino_tokens[1]) else 0
                self.implements["fan"] = 1 if is_token_active(last_arduino_tokens[2]) else 0

            # When entering IDLE_STANDBY or TRIPPING, check for any active unnecessary loads
            adv = self.get_advisories()
            if adv:
                total_wasted = sum(a["savings_kw"] for a in adv)
                total_cost = sum(a["cost_hr"] for a in adv)
                self.add_event("ADVISORY", f"Rig entered {new_state}: {len(adv)} unnecessary loads active ({total_wasted:.0f} kW). Suggestions issued to crew.", "warning")

    def set_implement_state(self, implement: str, state: bool):
        """Simulates physical local crew switch actions for demonstration purposes."""
        if implement == "bop":
            self.add_event("SAFETY", "COMMAND REJECTED: Tier 1 BOP is permanently locked ON by safety interlock.", "danger")
            return
        if implement not in self.implements:
            return

        val = 1 if state else 0
        if self.implements[implement] != val:
            self.implements[implement] = val
            state_label = "ENERGIZED" if val else "DE-ENERGIZED"
            
            # If crew turned off a load during standby, log compliance
            if not val and self.rig_state in ["IDLE_STANDBY", "TRIPPING", "PIPE_CONNECTION"]:
                self.add_event("CREW-ACTION", f"Crew powered down {implement.upper()} locally ({RATINGS[implement]} kW avoided waste).", "success")
            else:
                self.add_event("SIMULATION", f"{implement.upper()} locally switched to {state_label} ({RATINGS[implement]} kW).", "info")

    def handle_button_press(self, channel: int, explicit_state: Optional[bool] = None) -> Optional[str]:
        """Handles physical button press from the Arduino control panel."""
        if channel not in BUTTON_MAP or BUTTON_MAP[channel] is None:
            self.add_event("PANEL", f"Hardware Button {channel} pressed (Channel {channel} is unassigned/skipped).", "info")
            return None

        implement = BUTTON_MAP[channel]
        current_state = bool(self.implements.get(implement, 0))
        new_state = (not current_state) if (explicit_state is None) else bool(explicit_state)

        self.set_implement_state(implement, new_state)
        state_str = "ON" if new_state else "OFF"
        imp_name = IMPLEMENT_NAMES.get(implement, implement.upper())
        self.add_event("PANEL", f"Control Panel Button {channel}: {imp_name} switched {state_str}.", "info")
        return implement

    def simulate_all_on(self):
        """Simulates scenario where crew leaves all auxiliary equipment running in idle."""
        self.implements["shaker"] = 1
        self.implements["fan"] = 1
        self.implements["lights"] = 1
        self.add_event("SCENARIO", "Unmanaged Rig Simulated: All auxiliary loads left running in standby.", "warning")

    def simulate_crew_compliance(self):
        """Simulates field crew complying with every current advisory."""
        advisories = [a for a in self.get_advisories() if a["implement"] != "bop"]
        if not advisories:
            return
        total_kw = sum(a["savings_kw"] for a in advisories)
        for advisory in advisories:
            self.implements[advisory["implement"]] = 0
        self.add_event(
            "COMPLIANCE",
            f"Crew complied with {len(advisories)} advisories: recommended loads shut down ({total_kw:.0f} kW saved).",
            "success",
        )

    def set_manual_override(self, active: bool):
        self.manual_override = active
        if active:
            self.implements["rotary"] = 1 if self.rig_state == "ACTIVE_DRILLING" else 0
            self.implements["shaker"] = 1
            self.implements["fan"] = 1
            self.implements["lights"] = 1
            self.add_event("SAFETY", "MANUAL OVERRIDE ACTIVATED: All implements energized.", "danger")
        else:
            self.add_event("SAFETY", "Manual override cleared.", "info")

    def set_auto_saver(self, active: bool):
        self.auto_saver_active = active
        status_str = "ENABLED" if active else "DISABLED"
        self.add_event("MODE", f"Auto-saver mode {status_str}.", "info")

    def trigger_torque_spike(self, duration: float = 4.0, spike_adc: int = 920):
        """Triggers a simulated torque spike held for the specified duration (default: 4 seconds)."""
        now = time.time()
        if self.pot_raw <= 850:
            self.pre_spike_pot = self.pot_raw
        self.simulated_spike_end_time = now + duration
        self.update_sensors(spike_adc)

    def update_sensors(self, pot_raw: int, hw_override: Optional[bool] = None, hw_failsafe: Optional[bool] = None):
        now = time.time()
        if hw_override is not None:
            self.manual_override = hw_override
        if hw_failsafe is not None:
            self.failsafe = hw_failsafe

        # If a simulated spike is actively running, hold pot_raw at spike level
        if now < self.simulated_spike_end_time:
            effective_pot = 920
        else:
            effective_pot = pot_raw

        self.pot_raw = max(0, min(1023, effective_pot))
        was_spike = self.torque_spike
        self.torque_spike = self.pot_raw > 850
        if self.torque_spike and not was_spike:
            self.add_event("ALARM", f"High downhole torque spike detected! Pot: {self.pot_raw}", "danger")
        elif not self.torque_spike and was_spike:
            self.add_event("ALARM", "Downhole torque returned to normal operating range.", "info")

    def apply_automation_rules(self):
        # Tier 1 BOP is hard-locked ON permanently
        self.implements["bop"] = 1
        if self.rig_state != "ACTIVE_DRILLING":
            self.implements["rotary"] = 0

    def step_simulation(self):
        now = time.time()
        dt = now - self.last_update_time
        self.last_update_time = now

        # Defense in depth: Tier-1 well control can never be de-energized.
        self.implements["bop"] = 1

        if self.simulated_spike_end_time > 0 and now >= self.simulated_spike_end_time:
            self.simulated_spike_end_time = 0.0
            self.update_sensors(getattr(self, "pre_spike_pot", 0))

        self.dynamic_torque_kw = (
            (self.pot_raw / 1023.0) * 500.0 if self.implements["rotary"] else 0.0
        )

        base_rotary = (
            NOMINAL_LOAD_KW["rotary"] + self.dynamic_torque_kw
            if self.rig_state == "ACTIVE_DRILLING" else 0.0
        )
        self.base_kw = (
            NOMINAL_LOAD_KW["bop"] + base_rotary + NOMINAL_LOAD_KW["shaker"]
            + NOMINAL_LOAD_KW["fan"] + NOMINAL_LOAD_KW["lights"]
        )

        feeder_kw = {
            key: (NOMINAL_LOAD_KW[key] if is_on else 0.0)
            for key, is_on in self.implements.items()
        }
        feeder_kw["rotary"] += self.dynamic_torque_kw
        self.actual_kw = sum(feeder_kw.values())
        self.saved_kw = max(0.0, self.base_kw - self.actual_kw)

        any_feeder_overloaded = any(
            feeder_kw[key] > RATED_CAPACITY_KW[key] for key in feeder_kw
        )
        self.overload_active = self.actual_kw > CONNECTED_CAPACITY_KW or any_feeder_overloaded
        utilization_pct = (self.actual_kw / CONNECTED_CAPACITY_KW) * 100.0
        self.high_load_warning = utilization_pct >= 85.0 or self.torque_spike

        hours = dt / 3600.0
        kwh_delta = self.saved_kw * hours
        self.kwh_saved_total += kwh_delta
        diesel_delta = kwh_delta * DIESEL_PER_KWH
        self.diesel_saved_l += diesel_delta
        self.cost_saved_usd += diesel_delta * COST_PER_LITER
        self.co2_saved_kg += diesel_delta * CO2_PER_LITER

    def get_telemetry_dict(self) -> dict:
        saved_pct = (self.saved_kw / self.base_kw * 100.0) if self.base_kw > 0 else 0.0
        advisories = self.get_advisories()
        tiers = {
            "bop": "Tier 1 (Locked ON)", "rotary": "Primary Process",
            "shaker": "Tier 2 (Auxiliary)", "fan": "Tier 3 (Auxiliary)",
            "lights": "Tier 3 (Auxiliary)",
        }
        implements = {}
        for key, is_on in self.implements.items():
            kw = NOMINAL_LOAD_KW[key] if is_on else 0.0
            if key == "rotary" and is_on:
                kw += self.dynamic_torque_kw
            implements[key] = {
                "on": bool(is_on), "kw": round(kw, 1),
                "kw_rated": RATED_CAPACITY_KW[key], "tier": tiers[key],
                "name": IMPLEMENT_NAMES[key], "subtitle": IMPLEMENT_SUBTITLES[key],
                "feeder": IMPLEMENT_FEEDERS[key],
                "overloaded": kw > RATED_CAPACITY_KW[key],
            }
        utilization_pct = (self.actual_kw / CONNECTED_CAPACITY_KW) * 100.0
        return {
            "rig_state": self.rig_state,
            "hardware_connected": self.hardware_connected,
            "auto_saver": self.auto_saver_active,
            "manual_override": self.manual_override,
            "failsafe": self.failsafe,
            "advisories": advisories,
            "advisory_count": len(advisories),
            "unnecessary_kw": sum(a["savings_kw"] for a in advisories),
            "glitch_filter": {"active": False, "remaining_sec": 0.0, "passed": True},
            "implements": implements,
            "pot_raw": self.pot_raw,
            "torque_spike": self.torque_spike,
            "power": {
                "base_kw": round(self.base_kw, 1),
                "actual_kw": round(self.actual_kw, 1),
                "saved_kw": round(self.saved_kw, 1),
                "saved_pct": round(saved_pct, 1),
                "connected_capacity_kw": round(CONNECTED_CAPACITY_KW, 1),
                "utilization_pct": round(utilization_pct, 1),
                "dynamic_torque_kw": round(self.dynamic_torque_kw, 1),
                "overload_active": self.overload_active,
                "high_load_warning": self.high_load_warning,
            },
            "cumulative": {
                "kwh_saved": round(self.kwh_saved_total, 2),
                "diesel_l": round(self.diesel_saved_l, 2),
                "cost_usd": round(self.cost_saved_usd, 2),
                "co2_kg": round(self.co2_saved_kg, 2),
            },
            "events": list(self.events)[:15],
            "timestamp": time.time(),
        }

    def get_hardware_command(self) -> dict:
        return {
            "cmd": "SET",
            "rotary": self.implements["rotary"],
            "shaker": self.implements["shaker"],
            "fan": self.implements["fan"],
            "lights": self.implements["lights"],
            "rig_state": self.rig_state,
            "kw_saved": int(self.saved_kw)
        }


# Global Rig Instance
rig = DigitalTwinRig()

# Active WebSocket connections
connected_websockets: Set[WebSocket] = set()

# Serial manager variables
serial_port_name: Optional[str] = None
serial_baud: int = 9600
serial_conn = None
serial_lock = asyncio.Lock()


async def broadcast_telemetry():
    if not connected_websockets:
        return
    payload = json.dumps(rig.get_telemetry_dict())
    dead = set()
    for ws in list(connected_websockets):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        connected_websockets.discard(ws)


def find_arduino_port() -> Optional[str]:
    """Scans system for connected Arduino or USB serial devices."""
    try:
        import serial.tools.list_ports
        ports = list(serial.tools.list_ports.comports())
        # First priority: COM4 (default in Aggies_Invent_Button_Code.py)
        for p in ports:
            if "COM4" in p.device.upper():
                return p.device
        # Second priority: Arduino, CH340, CP210, FTDI, USB Serial, usbmodem, ttyUSB, ttyACM
        for p in ports:
            desc = (p.description or "").lower()
            dev = (p.device or "").lower()
            if any(k in desc or k in dev for k in ["arduino", "ch340", "cp210", "ftdi", "usb serial", "usbmodem", "usbserial", "ttyusb", "ttyacm"]):
                return p.device
    except Exception:
        pass
    return None


# Hardware switch state tracking for delta-detection
last_arduino_tokens: Optional[List[str]] = None
last_button_press_time: Dict[int, float] = {}


VALID_STATE_TOKENS = {"ON", "OFF", "HIGH", "LOW", "1", "0", "TRUE", "FALSE", "ACTIVE", "INACTIVE", "STOPPED", "STARTED"}

def is_token_active(token: str) -> bool:
    """Returns True if the token represents an energized / high / on hardware state."""
    t = token.strip().upper()
    return t in {"ON", "HIGH", "1", "TRUE", "ACTIVE", "STARTED", "CLOSED", "PRESS"}


def handle_arduino_serial_line(line: str):
    """Processes a line or token received from the Arduino control panel."""
    global last_arduino_tokens, last_button_press_time
    line = line.strip()
    if not line:
        return

    logger.info(f"[ARDUINO-SERIAL-RX] '{line}'")
    # Broadcast to SCADA dashboard log so the user immediately sees the received text in their browser!
    rig.add_event("ARDUINO-RX", f"Hardware Rx: '{line}'", "info")

    # Check for multi-channel state list: e.g. "OFF,OFF,OFF", "ON,OFF,ON", "OFF OFF OFF", "ON, ON, ON"
    candidate_tokens = [t.strip().upper() for t in re.split(r'[,;/\s]+', line) if t.strip()]
    is_multi_state = (
        len(candidate_tokens) >= 2
        and all(t in VALID_STATE_TOKENS for t in candidate_tokens)
    )

    if is_multi_state:
        state_tokens = candidate_tokens
        for idx in range(len(state_tokens)):
            ch = idx + 1
            if ch in BUTTON_MAP and BUTTON_MAP[ch] is not None:
                imp = BUTTON_MAP[ch]
                target_state = is_token_active(state_tokens[idx])
                cur_state = bool(rig.implements.get(imp, 0))
                # Direct state synchronization: website feeder state matches Arduino light state exactly
                if cur_state != target_state:
                    rig.handle_button_press(ch, explicit_state=target_state)
                    logger.info(f"[BUTTON-PANEL] Hardware Channel {ch} ({imp}) synchronized to {'ON' if target_state else 'OFF'} (token: '{state_tokens[idx]}')")
        last_arduino_tokens = state_tokens[:]
        return

    parsed = parse_button_event(line)
    if parsed:
        ch, explicit_state = parsed
        now = time.time()
        if (now - last_button_press_time.get(ch, 0)) >= 0.15:
            last_button_press_time[ch] = now
            imp = rig.handle_button_press(ch, explicit_state)
            logger.info(f"[BUTTON-PANEL] Hardware Button {ch} -> {imp} (state={explicit_state}, raw: '{line}')")
            global serial_conn
            if serial_conn:
                try:
                    serial_conn.write(f"T{ch}\n".encode("utf-8"))
                    serial_conn.flush()
                except Exception:
                    pass
    elif line.startswith("{") and line.endswith("}"):
        try:
            data = json.loads(line)
            if "pot_raw" in data:
                rig.update_sensors(data.get("pot_raw", rig.pot_raw))
            if data.get("override"):
                rig.handle_button_press(1)
            if data.get("mode_btn_pressed"):
                rig.handle_button_press(2)
        except json.JSONDecodeError:
            pass


def serial_thread_worker(port: Optional[str], baud: int):
    """
    Dedicated OS thread for robust Arduino serial reading.
    Uses direct blocking read() with a 100 ms timeout to completely bypass the macOS in_waiting bug.
    Handles \\r, \\n, \\r\\n, and bare character pulses without newline.
    """
    import serial
    global serial_conn, last_arduino_tokens
    rig.hardware_connected = False
    last_arduino_tokens = None

    while True:
        target_port = port
        if not target_port or target_port == "auto":
            target_port = find_arduino_port()

        if not target_port:
            rig.hardware_connected = False
            last_arduino_tokens = None
            time.sleep(2.0)
            continue

        s = None
        try:
            logger.info(f"Connecting to Arduino Control Panel on {target_port} @ {baud} baud...")
            s = serial.Serial(target_port, baud, timeout=0.1)
            try:
                s.dtr = False
                s.rts = False
            except Exception:
                pass
            time.sleep(1.8)  # Let Mega 2560 bootloader settle
            s.reset_input_buffer()

            serial_conn = s
            rig.hardware_connected = True
            last_arduino_tokens = None  # Reset baseline on fresh connection
            rig.add_event("HARDWARE", f"Arduino Control Panel online on {target_port} ({baud} baud).", "success")
            logger.info(f"Arduino Control Panel online on {target_port}. Ready for buttons on pins 2-5...")

            rx_buffer = ""

            while True:
                # Direct read system call with 100ms timeout (never stalled by macOS in_waiting bug)
                try:
                    raw_chunk = s.read(1024)
                except Exception as e:
                    logger.warning(f"Serial read error on {target_port}: {e}")
                    break

                if raw_chunk:
                    # Check for single raw binary byte e.g. \x01, \x02, \x03, \x04
                    if len(raw_chunk) == 1 and raw_chunk[0] in [1, 2, 3, 4, 5]:
                        handle_arduino_serial_line(str(raw_chunk[0]))
                        continue

                    text = raw_chunk.decode("utf-8", errors="ignore")
                    rx_buffer += text

                    # Split on any CR, LF, or CRLF
                    while "\n" in rx_buffer or "\r" in rx_buffer:
                        m = re.search(r'[\r\n]', rx_buffer)
                        if not m:
                            break
                        pos = m.start()
                        line = rx_buffer[:pos].strip()
                        rx_buffer = rx_buffer[pos+1:].lstrip("\r\n")
                        if line:
                            handle_arduino_serial_line(line)

                    # Handle bare single characters in buffer without newline
                    if len(rx_buffer) in [1, 2]:
                        stripped = rx_buffer.strip()
                        if stripped in ["1", "2", "3", "4", "5"] or (stripped.upper().startswith("T") and len(stripped) == 2):
                            handle_arduino_serial_line(stripped)
                            rx_buffer = ""

        except Exception as e:
            rig.hardware_connected = False
            last_arduino_tokens = None
            if s:
                try:
                    s.close()
                except Exception:
                    pass
            serial_conn = None
            logger.warning(f"Arduino connection on {target_port} error: {e}. Retrying in 2 seconds...")
            time.sleep(2.0)


async def main_loop():
    """Main 5 Hz state calculation and WebSocket broadcast loop."""
    while True:
        rig.step_simulation()
        await broadcast_telemetry()
        await asyncio.sleep(0.2)  # 5 Hz update interval (200 ms)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start 5 Hz main simulation and broadcast loop
    asyncio.create_task(main_loop())

    # Start serial worker for Arduino control panel in dedicated OS thread
    global serial_port_name, serial_baud
    t_serial = threading.Thread(target=serial_thread_worker, args=(serial_port_name, serial_baud), daemon=True)
    t_serial.start()

    yield


# FastAPI Application Setup
app = FastAPI(title="Rig Energy Management SCADA API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
index_html_path = frontend_dir / "index.html"


@app.get("/")
async def get_index():
    if index_html_path.exists():
        return FileResponse(str(index_html_path))
    return HTMLResponse("<h1>SCADA Dashboard UI is being loaded...</h1>")


@app.get("/api/state")
async def get_state():
    return rig.get_telemetry_dict()


@app.get("/api/button_map")
async def get_button_map():
    return {
        "button_map": BUTTON_MAP,
        "implement_names": IMPLEMENT_NAMES
    }


@app.post("/api/command")
async def handle_rest_command(cmd: dict):
    action = cmd.get("action")
    if action == "set_state":
        rig.set_rig_state(cmd.get("state", "ACTIVE_DRILLING"))
    elif action == "button_press":
        ch = int(cmd.get("channel", 1))
        st = cmd.get("state", None)
        rig.handle_button_press(ch, st)
    elif action == "toggle_override":
        rig.set_manual_override(not rig.manual_override)
    elif action == "set_override":
        rig.set_manual_override(bool(cmd.get("value", False)))
    elif action == "toggle_autosaver":
        rig.set_auto_saver(not rig.auto_saver_active)
    elif action == "set_autosaver":
        rig.set_auto_saver(bool(cmd.get("value", True)))
    elif action == "set_pot":
        val = int(cmd.get("value", 400))
        if rig.simulated_spike_end_time > 0 and val <= 850:
            rig.simulated_spike_end_time = 0.0
        rig.update_sensors(val)
    elif action == "trigger_torque_spike":
        duration = float(cmd.get("duration", 4.0))
        rig.trigger_torque_spike(duration=duration)
    elif action == "toggle_implement":
        imp = cmd.get("implement")
        cur = rig.implements.get(imp, 0)
        rig.set_implement_state(imp, not bool(cur))
    elif action == "set_implement":
        rig.set_implement_state(cmd.get("implement"), bool(cmd.get("value", False)))
    elif action == "optimize_idle" or action == "simulate_crew_compliance":
        rig.simulate_crew_compliance()
    elif action == "simulate_all_on":
        rig.simulate_all_on()
    elif action == "clear_events":
        rig.events.clear()
        rig.add_event("SYSTEM", "Event log cleared by operator.")
    return {"status": "ok", "state": rig.get_telemetry_dict()}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_websockets.add(websocket)
    # Immediately send current state
    await websocket.send_text(json.dumps(rig.get_telemetry_dict()))
    try:
        while True:
            text = await websocket.receive_text()
            try:
                cmd = json.loads(text)
                action = cmd.get("action")
                if action == "set_state":
                    rig.set_rig_state(cmd.get("state", "ACTIVE_DRILLING"))
                elif action == "button_press":
                    ch = int(cmd.get("channel", 1))
                    st = cmd.get("state", None)
                    rig.handle_button_press(ch, st)
                elif action == "set_pot":
                    val = int(cmd.get("value", 400))
                    if rig.simulated_spike_end_time > 0 and val <= 850:
                        rig.simulated_spike_end_time = 0.0
                    rig.update_sensors(val)
                elif action == "trigger_torque_spike":
                    duration = float(cmd.get("duration", 4.0))
                    rig.trigger_torque_spike(duration=duration)
                elif action == "toggle_implement":
                    imp = cmd.get("implement")
                    cur = rig.implements.get(imp, 0)
                    rig.set_implement_state(imp, not bool(cur))
                elif action == "set_implement":
                    rig.set_implement_state(cmd.get("implement"), bool(cmd.get("value", False)))
                elif action == "optimize_idle" or action == "simulate_crew_compliance":
                    rig.simulate_crew_compliance()
                elif action == "simulate_all_on":
                    rig.simulate_all_on()
                elif action == "clear_events":
                    rig.events.clear()
                    rig.add_event("SYSTEM", "Event log cleared by operator.")
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        connected_websockets.discard(websocket)


def main():
    parser = argparse.ArgumentParser(description="Automated Rig Power SCADA Server")
    parser.add_argument("--port", type=str, default=None, help="Serial port for Arduino Button Panel (e.g. COM4 or /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=9600, help="Serial baud rate for button panel (default: 9600)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="HTTP / WebSocket host address (default: 0.0.0.0)")
    parser.add_argument("--web-port", type=int, default=8000, help="Web server port (default: 8000)")
    parser.add_argument("--button-map", type=str, default=None, help="Custom button map e.g. '1:shaker,2:rotary,3:fan'")

    args = parser.parse_args()

    global serial_port_name, serial_baud
    serial_port_name = args.port
    serial_baud = args.baud

    if args.button_map:
        for pair in args.button_map.split(","):
            if ":" in pair:
                ch_str, imp = pair.split(":", 1)
                try:
                    ch = int(ch_str)
                    BUTTON_MAP[ch] = imp.strip().lower() if imp.strip().lower() != "none" else None
                except ValueError:
                    pass

    logger.info("======================================================================")
    logger.info("  RIG CONTROL PANEL (ARDUINO BUTTON MAPPING - PINS 2-5)")
    for ch in range(1, 5):
        imp = BUTTON_MAP.get(ch)
        name = IMPLEMENT_NAMES.get(imp, "Skipped / Unassigned") if imp else "Skipped / Unassigned"
        logger.info(f"  Channel {ch} (Pin {ch+1}): {name} [{imp or 'None'}]")
    logger.info("======================================================================")

    uvicorn.run(app, host=args.host, port=args.web_port, log_level="info")


if __name__ == "__main__":
    main()
