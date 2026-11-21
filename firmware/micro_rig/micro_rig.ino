/*
 * ==============================================================================
 * Aggies Invent (ExxonMobil Challenge) — Automated Rig Power Management
 * Micro-Rig Firmware for Elegoo Mega 2560 R3
 *
 * NON-NEGOTIABLE SAFETY & ENGINEERING REQUIREMENTS:
 * 1. Tier 1 (BOP) is PERMANENTLY LOCKED ON (Pin 30 Green LED always HIGH).
 * 2. 2000 ms Serial Watchdog: If no PING/SET is received within 2000 ms,
 *    enter FAILSAFE mode: force all implements ON immediately (<1s).
 * 3. Strictly NON-BLOCKING: All timing uses millis(). NO delay() in loop().
 * 4. Zero external libraries required (uses only standard Arduino Servo.h).
 * ==============================================================================
 */

#include <Arduino.h>
#include <Servo.h>

// -----------------------------------------------------------------------------
// PIN DEFINITIONS (Elegoo Mega 2560 R3 Fixed Pin Map)
// -----------------------------------------------------------------------------
// Tier 1: Well Control / BOP & Alarms
const uint8_t PIN_BOP_GREEN   = 30;  // Green status LED (Always HIGH)
const uint8_t PIN_BOP_RED     = 31;  // Red alarm LED (Failsafe or Torque Spike)
const uint8_t PIN_BOP_BUZZER  = 32;  // Active Buzzer

// Rotary Drive: 28BYJ-48 Stepper + ULN2003 Driver
const uint8_t PIN_STEP_IN1    = 22;
const uint8_t PIN_STEP_IN2    = 24;
const uint8_t PIN_STEP_IN3    = 26;
const uint8_t PIN_STEP_IN4    = 28;

// Tier 2: Mud Agitator / Shale Shaker (SG90 Micro Servo)
const uint8_t PIN_SERVO       = 9;

// Tier 3: Ventilation / Cooling Blower (3-6V DC Motor PWM)
const uint8_t PIN_FAN_PWM     = 6;

// Tier 3: Site Lighting / Aux Bank (5V Relay Module)
const uint8_t PIN_RELAY       = 7;

// Crew Inputs
const uint8_t PIN_BTN_OVERRIDE = 2;  // Manual Override Push Button (Active LOW)
const uint8_t PIN_BTN_MODE     = 3;  // Rig Mode Cycle Push Button (Active LOW)
const uint8_t PIN_POT_TORQUE   = A0; // Downhole Torque Simulator (0 - 1023)

// -----------------------------------------------------------------------------
// HARDWARE CONFIGURATION CONSTANTS
// -----------------------------------------------------------------------------
#define RELAY_ACTIVE_LOW true   // Set to false if relay module is active-HIGH
#define FAN_PWM_SPEED 220       // PWM duty cycle (0-255) when fan is ON
#define WATCHDOG_TIMEOUT_MS 2000 // 2.0s watchdog timeout
#define TELEMETRY_INTERVAL_MS 200 // 5 Hz telemetry broadcast (200 ms)
#define STEPPER_INTERVAL_MICROS 2500 // Speed of stepper motor (2.5 ms per half-step)

// -----------------------------------------------------------------------------
// GLOBAL STATE VARIABLES
// -----------------------------------------------------------------------------
// Commanded implement states (from backend or failsafe)
bool state_rotary = true;
bool state_shaker = true;
bool state_fan    = true;
bool state_lights = true;

// Safety & System Flags
bool manual_override_active = false;
bool failsafe_active = false;
bool torque_spike_active = false;
bool mode_btn_pressed_latched = false;

// Serial Watchdog Tracking
unsigned long last_serial_rx_ms = 0;

// Timing Trackers
unsigned long last_telemetry_tx_ms = 0;
unsigned long last_servo_update_ms = 0;
unsigned long last_stepper_step_micros = 0;
unsigned long last_alarm_blink_ms = 0;

// Inputs & Debouncing
int pot_raw_value = 0;
bool last_override_btn_reading = HIGH;
unsigned long last_override_btn_debounce_ms = 0;
bool last_mode_btn_reading = HIGH;
unsigned long last_mode_btn_debounce_ms = 0;

// Actuator Objects & Variables
Servo shaker_servo;
int servo_angle = 90;
int servo_direction = 1;

// 8-step half-stepping sequence for 28BYJ-48
const uint8_t STEP_PINS[4] = {PIN_STEP_IN1, PIN_STEP_IN2, PIN_STEP_IN3, PIN_STEP_IN4};
const uint8_t HALF_STEPS[8][4] = {
  {1, 0, 0, 0},
  {1, 1, 0, 0},
  {0, 1, 0, 0},
  {0, 1, 1, 0},
  {0, 0, 1, 0},
  {0, 0, 1, 1},
  {0, 0, 0, 1},
  {1, 0, 0, 1}
};
uint8_t current_step_index = 0;

// Serial RX Buffer
char rx_line_buffer[128];
uint8_t rx_buffer_index = 0;

// -----------------------------------------------------------------------------
// HELPER FUNCTIONS
// -----------------------------------------------------------------------------
void setRelay(bool on) {
  if (RELAY_ACTIVE_LOW) {
    digitalWrite(PIN_RELAY, on ? LOW : HIGH);
  } else {
    digitalWrite(PIN_RELAY, on ? HIGH : LOW);
  }
}

void deenergizeStepper() {
  for (uint8_t i = 0; i < 4; i++) {
    digitalWrite(STEP_PINS[i], LOW);
  }
}

void stepMotor() {
  current_step_index = (current_step_index + 1) % 8;
  for (uint8_t i = 0; i < 4; i++) {
    digitalWrite(STEP_PINS[i], HALF_STEPS[current_step_index][i]);
  }
}

// -----------------------------------------------------------------------------
// SETUP
// -----------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);

  // Initialize Tier 1 BOP & Alarms
  pinMode(PIN_BOP_GREEN, OUTPUT);
  pinMode(PIN_BOP_RED, OUTPUT);
  pinMode(PIN_BOP_BUZZER, OUTPUT);

  // Requirement 2.1: Tier 1 BOP Green LED is permanently LOCKED ON
  digitalWrite(PIN_BOP_GREEN, HIGH);
  digitalWrite(PIN_BOP_RED, LOW);
  digitalWrite(PIN_BOP_BUZZER, LOW);

  // Initialize Stepper Pins
  for (uint8_t i = 0; i < 4; i++) {
    pinMode(STEP_PINS[i], OUTPUT);
    digitalWrite(STEP_PINS[i], LOW);
  }

  // Initialize Fan (DC Motor PWM)
  pinMode(PIN_FAN_PWM, OUTPUT);
  analogWrite(PIN_FAN_PWM, FAN_PWM_SPEED);

  // Initialize Relay (Lights)
  pinMode(PIN_RELAY, OUTPUT);
  setRelay(true);

  // Initialize Servo (Shaker)
  shaker_servo.attach(PIN_SERVO);
  shaker_servo.write(90);

  // Initialize Inputs
  pinMode(PIN_BTN_OVERRIDE, INPUT_PULLUP);
  pinMode(PIN_BTN_MODE, INPUT_PULLUP);
  pinMode(PIN_POT_TORQUE, INPUT);

  // Seed watchdog timer to give laptop server 5 seconds to connect before failsafe
  last_serial_rx_ms = millis() + 3000;
}

// -----------------------------------------------------------------------------
// MAIN LOOP (NON-BLOCKING)
// -----------------------------------------------------------------------------
void loop() {
  unsigned long now_ms = millis();
  unsigned long now_micros = micros();

  // 1. READ ANALOG TORQUE POTENTIOMETER (PIN A0)
  pot_raw_value = analogRead(PIN_POT_TORQUE);
  torque_spike_active = (pot_raw_value > 850);

  // 2. READ & DEBOUNCE OVERRIDE BUTTON (PIN 2)
  bool reading_override = digitalRead(PIN_BTN_OVERRIDE);
  if (reading_override != last_override_btn_reading) {
    last_override_btn_debounce_ms = now_ms;
  }
  if ((now_ms - last_override_btn_debounce_ms) > 50) {
    // If button is held down (LOW), latch manual override
    if (reading_override == LOW && !manual_override_active) {
      manual_override_active = true;
    }
  }
  last_override_btn_reading = reading_override;

  // 3. READ & DEBOUNCE RIG MODE BUTTON (PIN 3)
  bool reading_mode = digitalRead(PIN_BTN_MODE);
  if (reading_mode != last_mode_btn_reading) {
    last_mode_btn_debounce_ms = now_ms;
  }
  if ((now_ms - last_mode_btn_debounce_ms) > 50) {
    if (reading_mode == LOW && last_mode_btn_reading == HIGH) {
      mode_btn_pressed_latched = true;
    }
  }
  last_mode_btn_reading = reading_mode;

  // 4. SERIAL READ & JSON PARSING
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (rx_buffer_index > 0) {
        rx_line_buffer[rx_buffer_index] = '\0';
        parseIncomingCommand(rx_line_buffer);
        rx_buffer_index = 0;
      }
    } else {
      if (rx_buffer_index < sizeof(rx_line_buffer) - 1) {
        rx_line_buffer[rx_buffer_index++] = c;
      }
    }
  }

  // 5. 2000 MS SERIAL WATCHDOG CHECK
  if (now_ms - last_serial_rx_ms > WATCHDOG_TIMEOUT_MS) {
    // Watchdog expired! Server disconnected or crashed -> FAILSAFE MODE
    failsafe_active = true;
  } else {
    failsafe_active = false;
  }

  // 6. APPLY FAILSAFE & OVERRIDE FORCE-ON LOGIC
  bool effective_rotary = state_rotary;
  bool effective_shaker = state_shaker;
  bool effective_fan    = state_fan;
  bool effective_lights = state_lights;

  // Requirement 2.2 & 3.1: Failsafe or Override immediately energizes all implements (<1s)
  if (failsafe_active || manual_override_active) {
    effective_rotary = true;
    effective_shaker = true;
    effective_fan    = true;
    effective_lights = true;
  }

  // Always enforce Tier 1 BOP Green ON
  digitalWrite(PIN_BOP_GREEN, HIGH);

  // Alarm LED & Buzzer handling (Failsafe or Torque Spike)
  if (failsafe_active || torque_spike_active) {
    if (now_ms - last_alarm_blink_ms >= 250) {
      last_alarm_blink_ms = now_ms;
      digitalWrite(PIN_BOP_RED, !digitalRead(PIN_BOP_RED));
      digitalWrite(PIN_BOP_BUZZER, !digitalRead(PIN_BOP_BUZZER));
    }
  } else {
    digitalWrite(PIN_BOP_RED, LOW);
    digitalWrite(PIN_BOP_BUZZER, LOW);
  }

  // 7. DRIVE ROTARY STEPPER MOTOR
  if (effective_rotary) {
    if (now_micros - last_stepper_step_micros >= STEPPER_INTERVAL_MICROS) {
      last_stepper_step_micros = now_micros;
      stepMotor();
    }
  } else {
    deenergizeStepper();
  }

  // 8. DRIVE MUD AGITATOR SERVO (NON-BLOCKING OSCILLATION)
  if (effective_shaker) {
    if (now_ms - last_servo_update_ms >= 18) {
      last_servo_update_ms = now_ms;
      servo_angle += (servo_direction * 2);
      if (servo_angle >= 135) {
        servo_angle = 135;
        servo_direction = -1;
      } else if (servo_angle <= 45) {
        servo_angle = 45;
        servo_direction = 1;
      }
      shaker_servo.write(servo_angle);
    }
  } else {
    // Parked at 90 degrees in standby
    if (servo_angle != 90) {
      servo_angle = 90;
      shaker_servo.write(90);
    }
  }

  // 9. DRIVE VENTILATION FAN (PWM)
  if (effective_fan) {
    analogWrite(PIN_FAN_PWM, FAN_PWM_SPEED);
  } else {
    analogWrite(PIN_FAN_PWM, 0);
  }

  // 10. DRIVE LIGHTING RELAY
  setRelay(effective_lights);

  // 11. TRANSMIT 5 HZ TELEMETRY (EVERY 200 MS)
  if (now_ms - last_telemetry_tx_ms >= TELEMETRY_INTERVAL_MS) {
    last_telemetry_tx_ms = now_ms;
    sendTelemetry(effective_rotary, effective_shaker, effective_fan, effective_lights);
  }
}

// -----------------------------------------------------------------------------
// TELEMETRY TRANSMITTER (Mega -> Laptop @ 5 Hz)
// -----------------------------------------------------------------------------
void sendTelemetry(bool r, bool s, bool f, bool l) {
  Serial.print(F("{\"bop\":1,"));
  Serial.print(F("\"rotary\":")); Serial.print(r ? 1 : 0); Serial.print(F(","));
  Serial.print(F("\"shaker\":")); Serial.print(s ? 1 : 0); Serial.print(F(","));
  Serial.print(F("\"fan\":"));    Serial.print(f ? 1 : 0); Serial.print(F(","));
  Serial.print(F("\"lights\":")); Serial.print(l ? 1 : 0); Serial.print(F(","));
  Serial.print(F("\"pot_raw\":")); Serial.print(pot_raw_value); Serial.print(F(","));
  Serial.print(F("\"override\":")); Serial.print(manual_override_active ? F("true,") : F("false,"));
  Serial.print(F("\"failsafe\":")); Serial.print(failsafe_active ? F("true,") : F("false,"));
  Serial.print(F("\"mode_btn_pressed\":")); Serial.print(mode_btn_pressed_latched ? F("true}\n") : F("false}\n"));

  mode_btn_pressed_latched = false;
}

// -----------------------------------------------------------------------------
// COMMAND PARSER (Laptop -> Mega)
// -----------------------------------------------------------------------------
void parseIncomingCommand(char* jsonStr) {
  // Feed watchdog on any valid incoming packet from server
  last_serial_rx_ms = millis();

  // Check for PING heartbeat: {"cmd":"PING"}
  if (strstr(jsonStr, "\"PING\"") != NULL) {
    return;
  }

  // Check for SET command: {"cmd":"SET", ...}
  if (strstr(jsonStr, "\"SET\"") != NULL) {
    // Parse "rotary":1/0
    char* pRotary = strstr(jsonStr, "\"rotary\":");
    if (pRotary != NULL) {
      state_rotary = (atoi(pRotary + 9) == 1);
    }

    // Parse "shaker":1/0
    char* pShaker = strstr(jsonStr, "\"shaker\":");
    if (pShaker != NULL) {
      state_shaker = (atoi(pShaker + 9) == 1);
    }

    // Parse "fan":1/0
    char* pFan = strstr(jsonStr, "\"fan\":");
    if (pFan != NULL) {
      state_fan = (atoi(pFan + 6) == 1);
    }

    // Parse "lights":1/0
    char* pLights = strstr(jsonStr, "\"lights\":");
    if (pLights != NULL) {
      state_lights = (atoi(pLights + 9) == 1);
    }
  }
}
