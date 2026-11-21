# Micro-Rig Electrical Wiring Quick Reference (Elegoo Mega 2560 R3)

*(LCD1602 omitted as requested)*

---

## 1. Power Distribution Rails
> ⚠️ **CRITICAL ELECTRICAL RULE:** Actuators (Stepper, Servo, DC Fan, Relay) MUST draw power from the **external 5V breadboard power module** (powered by the 9V wall adapter), **NOT** the Arduino's 5V pin.
> **All ground rails (`GND`) MUST be tied together (Arduino GND connected to breadboard power module GND).**

- **Mega 2560 USB**: Powers the Mega board and low-current sensors/LEDs.
- **External 5V Breadboard Module**: Powers 28BYJ-48 Stepper, SG90 Servo, DC Motor, and 5V Relay module.
- **Common Ground (`GND`)**: Connect Mega 2560 `GND` to external 5V power module `GND`.

---

## 2. Pinout Checklist

| Subsystem | Component | Mega 2560 Pin | External Connections |
| :--- | :--- | :---: | :--- |
| **Tier 1: BOP Status** | Green LED | **Pin 30** | Anode to Pin 30 through 220Ω / 330Ω resistor to GND. Always ON. |
| **Tier 1: BOP Alarm** | Red LED | **Pin 31** | Anode to Pin 31 through 220Ω / 330Ω resistor to GND. |
| **Tier 1: BOP Alarm** | Active Buzzer | **Pin 32** | (+) to Pin 32, (-) to GND. |
| **Rotary Drive (Drill)** | 28BYJ-48 + ULN2003 | **Pins 22, 24, 26, 28** | ULN2003 IN1=22, IN2=24, IN3=26, IN4=28. ULN2003 VCC to external 5V, GND to common GND. |
| **Tier 2: Shale Shaker** | SG90 Micro Servo | **Pin 9** | Orange (Signal) to Pin 9. Red to external 5V. Brown to common GND. |
| **Tier 3: Cooling Fan** | 3–6V DC Motor | **Pin 6 (PWM)** | Base of transistor/driver (PN2222 / L293D) to Pin 6. Motor (+) to external 5V. |
| **Tier 3: Site Lighting** | 5V Relay Module | **Pin 7** | IN to Pin 7. VCC to external 5V, GND to common GND. (Default: active LOW). |
| **Manual Override Button** | Tactile Push Button | **Pin 2** | One leg to Pin 2, opposite leg to GND (`INPUT_PULLUP`). |
| **Rig Mode Cycle Button** | Tactile Push Button | **Pin 3** | One leg to Pin 3, opposite leg to GND (`INPUT_PULLUP`). |
| **Torque Simulator** | 10kΩ Potentiometer | **Pin A0** | Outer pin 1 to Mega 5V, outer pin 3 to Mega GND, wiper (center pin) to **Pin A0**. |

---

## 3. Pre-Flight Continuity Checks Before Powering Up
1. [ ] Confirm external 5V module ground is jumpered to Arduino Mega `GND`.
2. [ ] Confirm Stepper ULN2003 driver red/black leads go to external 5V / common GND.
3. [ ] Confirm Servo power is external 5V (brown to GND, red to external 5V, orange to Pin 9).
4. [ ] Confirm tactile buttons connect Pin 2 and Pin 3 directly to `GND` (internal pull-ups are enabled in firmware).
5. [ ] Confirm Green LED is on Pin 30 with a current-limiting resistor.
