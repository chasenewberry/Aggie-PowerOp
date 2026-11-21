#!/usr/bin/env python3
"""
Quick interactive test tool for Rig Control Panel buttons.
Allows testing button presses (1, 2, 3, 4) from the terminal
and verifying immediate live updates on the SCADA dashboard.
"""
import sys
import json
import urllib.request

SCADA_URL = "http://localhost:8000/api/command"

BUTTONS = {
    1: "Shale Shaker & Mud Agitator (FDR-03)",
    2: "Rig Lighting & Auxiliary Compressors (FDR-05)",
    3: "Ventilation & Motor Blower (FDR-04)",
    4: "Channel 4 (Skipped / Unassigned)",
}

def trigger_button(ch: int):
    name = BUTTONS.get(ch, f"Channel {ch}")
    payload = json.dumps({"action": "button_press", "channel": ch}).encode("utf-8")
    try:
        req = urllib.request.Request(SCADA_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            st = data.get("state", {})
            imps = st.get("implements", {})
            print(f"[OK] Pressed Button {ch} -> {name}")
            print(f"     Current States: Shaker={imps.get('shaker',{}).get('on')} | Lights={imps.get('lights',{}).get('on')} | Fan={imps.get('fan',{}).get('on')}")
    except Exception as e:
        print(f"[ERROR] Could not contact server: {e}")

def main():
    print("=" * 65)
    print("  RIG SCADA BUTTON TESTER")
    print("  Type 1, 2, 3, 4 + Enter to toggle equipment on the live dashboard:")
    for ch, name in BUTTONS.items():
        print(f"    [{ch}] {name}")
    print("=" * 65)

    while True:
        try:
            line = input("\nEnter button (1-4, q to quit): ").strip()
            if line.lower() in ["q", "exit", "quit"]:
                break
            if line in ["1", "2", "3", "4"]:
                trigger_button(int(line))
            else:
                print("Please enter 1, 2, 3, or 4.")
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

if __name__ == "__main__":
    main()
