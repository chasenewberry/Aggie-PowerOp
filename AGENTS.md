# AGENTS.md — Automated Rig Power Management Micro-Rig Demo

## 1. Project Overview
This repository contains the firmware, backend server, and SCADA web dashboard for an **Aggies Invent (ExxonMobil Challenge)** prototype.

- **Target System:** Off-grid **onshore diesel-electric land drilling rig**.
- **Core Solution:** A non-intrusive software control and monitoring layer that automatically disables unused non-critical and auxiliary rig equipment during idle/standby states while providing real-time visibility into power draw, avoided fuel waste, and emissions.
- **Physical Demo Setup:** An **Elegoo Mega 2560 R3** drives 3D-printed micro-rig implements over USB serial from a local laptop running a Python FastAPI server. Judges and operators interact with the live dashboard from **iPads** over a local Wi-Fi hotspot.

---

## 2. Non-Negotiable Engineering & Safety Requirements
Every code change must preserve these five checkpoint design requirements:

1. **Non-Intrusive Modular Architecture:** Operates as a lightweight software layer over standard telemetry/JSON feeds. Must support a `--mock` hardware mode so the backend and UI can be developed and demonstrated without the Arduino plugged in.
2. **Fail-Safe & Hard Safety Lockouts:**
   - **Tier 1 (`bop`) is permanently locked ON** in both firmware and backend. Software must NEVER allow turning off `bop`.
   - **2000 ms Serial Watchdog:** The backend sends a heartbeat `PING` every `500 ms`. If the Mega 2560 receives no `PING` for `2000 ms` (e.g., USB unplugged or server crash), firmware MUST immediately force all implements **ON**, enter fail-safe mode, and display `FAILSAFE: ALL ON` on the LCD1602.
   - **Glitch Filter:** Auto-shutdown logic must require a **3-second persistence delay** after entering an idle state before disabling any Tier 2/3 implement so momentary sensor glitches never trigger false shutoffs.
3. **Zero Operational Disruption (Zero NPT) & Instant Override:**
   - Triggering **Manual Override** (either via the physical push button on Pin `2` or the red button on the iPad UI) must immediately re-energize all implements in `<1 second` and suspend auto-shutoff until cleared.
   - All Arduino firmware must be **strictly non-blocking** (use `millis()`; NEVER use `delay()` in `loop()`).
4. **Actionable Operator Visibility:** Live dashboard must display active vs. baseline kW, diesel saved, cost saved, $\text{CO}_2$ avoided, a 24-hour circular operational energy ring, and a timestamped event/advisory log.
5. **Measurable Idle Reduction:** Auto-shutoff targets Tier 2 and Tier 3 auxiliary/idle loads to demonstrate a $\ge 15\text{--}25\%$ reduction in avoidable idle energy draw.

---

## 3. Recommended Repository Structure
Keep the repository flat and hackathon-friendly:

```text
.
├── AGENTS.md
├── firmware/
│   └── micro_rig/
│       └── micro_rig.ino      # Elegoo Mega 2560 R3 non-blocking C++ firmware
├── backend/
│   ├── server.py              # FastAPI + WebSocket + pyserial bridge + power model
│   └── requirements.txt       # fastapi, uvicorn, pyserial, websockets
└── frontend/
    └── index.html             # Single-file responsive dark-mode SCADA UI (iPad Safari safe)
```

---

## 4. Hardware Architecture & Fixed Pin Map (Elegoo Mega 2560 R3)
**DO NOT change pin assignments** without updating both `firmware/micro_rig/micro_rig.ino` and this table.

| Rig Implement | Tier / Rule | Kit Component | Mega 2560 Pin(s) | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **Well Control / BOP** | **Tier 1 (Locked ON)** | Green LED, Red Alarm LED, Active Buzzer | `30` (Green), `31` (Red), `32` (Buzzer) | Green LED always HIGH; Red/Buzzer alert on failsafe or torque spike |
| **Rotary Drive / Drill String** | Primary Process Load | 28BYJ-48 Stepper + ULN2003 | `22, 24, 26, 28` | Non-blocking stepping (`AccelStepper` or `millis()` step loop) |
| **Mud Agitator / Shale Shaker** | **Tier 2 (Auto-Disable)** | SG90 Micro Servo | `9` | Oscillates smoothly when ON; parks at 90° when OFF |
| **Ventilation / Cooling Blower** | **Tier 3 (Auto-Disable)** | 3–6V DC Motor + L293D / PN2222 | `6` (PWM) | Ramps/runs at PWM duty cycle when ON; `0` when OFF |
| **Site Lighting / Aux Bank** | **Tier 3 (Auto-Disable)** | 5V Relay Module + LEDs | `7` | Check active-LOW vs active-HIGH relay polarity via a `#define RELAY_ACTIVE_LOW` constant |
| **Manual Override Button** | Instant Crew Override | Tactile Push Button | `2` | `INPUT_PULLUP` (active LOW, debounced) |
| **Rig Mode Cycle Button** | Local State Toggle | Tactile Push Button | `3` | `INPUT_PULLUP` (active LOW, debounced) |
| **Torque / Load Simulator** | Analog Load Input | 10k$\Omega$ Potentiometer | `A0` | `0–1023` mapped to simulated torque load & spike trigger |
| **Doghouse Display** | Local Rig Status | LCD1602 (4-bit parallel) | `40, 41, 42, 43, 44, 45` | `RS=40, EN=41, D4=42, D5=43, D6=44, D7=45` |

> **Electrical Safety Note:** Actuators (stepper, servo, DC fan, relay) are powered by the external 5V breadboard power module (9V wall adapter) with a shared `GND` to the Mega 2560. Keep initial startup current smooth to avoid brownouts.

---

## 5. Serial Communication Contract (115200 Baud)
All messages between `backend/server.py` and `firmware/micro_rig/micro_rig.ino` are **newline-delimited JSON (`\n`)**.

### Laptop $\rightarrow$ Mega 2560 (Commands)
- **Heartbeat (sent every `500 ms`):**
  ```json
  {"cmd":"PING"}
  ```
- **Set Implement / Mode States:**
  ```json
  {"cmd":"SET","rotary":1,"shaker":1,"fan":0,"lights":0,"rig_state":"IDLE_STANDBY","kw_saved":145}
  ```
  - `rotary`, `shaker`, `fan`, `lights`: `1` (ON) or `0` (OFF).
  - `bop` is intentionally omitted from writable commands (hard-locked ON in firmware).
  - `rig_state` and `kw_saved` are rendered on the LCD1602.

### Mega 2560 $\rightarrow$ Laptop (Telemetry @ `5 Hz` / every `200 ms`)
```json
{
  "bop": 1,
  "rotary": 1,
  "shaker": 1,
  "fan": 0,
  "lights": 0,
  "pot_raw": 412,
  "override": false,
  "failsafe": false,
  "mode_btn_pressed": false
}
```

---

## 6. Digital Twin Power & Emissions Model (`backend/server.py`)
Map each implement's physical ON/OFF state to realistic scaled onshore land-rig power ratings:

| Key | Display Name | Tier | Rated Power (kW) | Behavior in `IDLE_STANDBY` (Auto-Saver ON) |
| :--- | :--- | :---: | :---: | :--- |
| `bop` | Well Control / BOP & Monitoring | Tier 1 | `35` | **Locked ON** (Never disabled) |
| `rotary` | Rotary Drive / Top Drive | Primary | `420` | OFF during `PIPE_CONNECTION` / `IDLE_STANDBY` |
| `shaker` | Mud Agitator / Shale Shaker | Tier 2 | `110` | Auto-disabled after 3 s idle confirmation |
| `fan` | Ventilation / Cooling Blower | Tier 3 | `85` | Auto-disabled after 3 s idle confirmation |
| `lights` | Site Lighting / Aux Compressors | Tier 3 | `60` | Auto-disabled after 3 s idle confirmation |

### Calculations
- **Dynamic Torque Load:** Scale `pot_raw` (`0–1023`) to add `0–150 kW` of variable rotary/downhole torque load when `rotary == 1`. If `pot_raw > 850`, flag a `"TORQUE_SPIKE"` alert.
- **Baseline Power ($P_{\text{base}}$):** Sum of all implements running continuously (traditional unoptimized rig where Tier 2 & 3 stay ON during idle).
- **Actual Power ($P_{\text{actual}}$):** Sum of currently energized implements + dynamic torque load.
- **Power Saved:** $P_{\text{saved}} = \max(0, P_{\text{base}} - P_{\text{actual}})$ (kW).
- **Diesel Saved Rate (L/hr):** $\dot{V}_{\text{diesel}} = P_{\text{saved}} \times 0.26\text{ L/kWh}$.
- **Cost Saved Rate (\$/hr):** $\dot{C} = \dot{V}_{\text{diesel}} \times \$1.00/\text{L}$ (configurable constant).
- **$\text{CO}_2$ Avoided Rate (kg/hr):** $\dot{m}_{\text{CO}_2} = \dot{V}_{\text{diesel}} \times 2.68\text{ kg/L}$.
- Track cumulative session/shift totals (`kwh_saved_total`, `diesel_saved_l`, `co2_saved_kg`, `cost_saved_usd`) in memory and broadcast via WebSocket at `5 Hz`.

### Rig States
Support four selectable states:
1. `ACTIVE_DRILLING`: All implements ON.
2. `PIPE_CONNECTION`: `rotary` OFF; `shaker`, `fan`, `lights` remain ON unless auto-saver rules disable Tier 3.
3. `TRIPPING`: `rotary` OFF; `shaker` ON; Tier 3 (`fan`, `lights`) auto-disabled if Auto-Saver is ON.
4. `IDLE_STANDBY`: `rotary` OFF; after the **3-second glitch-filter persistence timer**, Tier 2 (`shaker`) and Tier 3 (`fan`, `lights`) auto-disable if Auto-Saver is ON and Manual Override is OFF.

---

## 7. Frontend / SCADA Dashboard Guidelines (`frontend/index.html`)
- **Single-File / Zero Build Step:** Use vanilla HTML/CSS/JS with lightweight CDN libraries (e.g., Tailwind CSS CDN + Chart.js or Plotly CDN) served directly by FastAPI so it runs reliably without complex Node toolchains.
- **iPad Safari Compatibility:** Must be touch-friendly (large buttons, `min-height: 44px`), responsive on tablet viewports, and automatically connect WebSockets using `window.location.host` (never hardcode `localhost`).
- **Required UI Panels:**
  1. **Top KPI Bar:** Live Power (kW), Power Saved (kW & %), Est. Diesel/Cost Saved (\$/shift), $\text{CO}_2$ Avoided (kg/shift), and live Hardware/Watchdog Heartbeat indicator (`ONLINE`, `MOCK`, or `FAILSAFE`).
  2. **Operations Control Bar:** Rig State buttons (`Active Drilling`, `Pipe Connection`, `Tripping`, `Idle Standby`), **Auto-Saver Toggle**, and a high-contrast red **`MANUAL OVERRIDE`** button.
  3. **Equipment Cards (5 implements):** Show Tier badge (`TIER 1 LOCKED`, `PRIMARY`, `TIER 2`, `TIER 3`), live kW draw, animated ON/OFF status, and manual toggle switch (`bop` visually padlocked and disabled).
  4. **Visualizations:**
     - Real-time scrolling line chart comparing **Baseline kW** vs. **Actual Optimized kW**.
     - **24-Hour Circular Operational Energy Ring** (polar/radar chart) showing steady load vs. radial surges/savings.
  5. **Actionable Insights & Event Log:** Timestamped feed of automated shutoffs, glitch-filter countdowns, torque spike alerts, manual overrides, and fail-safe events.

---

## 8. Running & Testing Locally

### Backend Setup
```bash
cd backend
pip install -r requirements.txt
```

### Run in Mock Mode (No Arduino Required)
```bash
python server.py --mock
```

### Run with Elegoo Mega 2560 Connected
```bash
# Replace COM3 / /dev/ttyACM0 with the actual Arduino serial port
python server.py --port /dev/ttyACM0
```

### Accessing from iPads
- Always bind Uvicorn to **`0.0.0.0:8000`** so iPads on the same phone hotspot can open `http://<laptop-local-ip>:8000`.

### Required Pre-Demo Verification Checklist
- [ ] **Auto-Disable Test:** Switch to `IDLE_STANDBY` with Auto-Saver ON $\rightarrow$ verify 3-second confirmation countdown $\rightarrow$ verify `shaker`, `fan`, `lights` turn OFF while `bop` stays ON.
- [ ] **Manual Override Test:** Press physical Pin `2` button OR tap iPad `MANUAL OVERRIDE` $\rightarrow$ verify all implements turn ON in `<1 second`.
- [ ] **Fail-Safe Test:** Unplug USB cable while Mega is externally powered $\rightarrow$ verify all implements turn ON within `2 seconds` and LCD displays `FAILSAFE: ALL ON`.
- [ ] **iPad Sync Test:** Open dashboard on laptop and iPad simultaneously $\rightarrow$ verify state changes reflect in real time on both screens.

---

## 9. Agent Working Rules
- Favor clarity, reliability, and demo stability over over-engineering.
- Never introduce blocking I/O in FastAPI WebSocket loops or `delay()` in Arduino firmware.
- Wrap all serial read/write calls in `try/except` with automatic reconnection handling so unplugging and replugging the USB cable during judging recovers cleanly without crashing `server.py`.
- Keep all external JS/CSS dependencies minimal (or cacheable) so the dashboard loads quickly over a phone hotspot.