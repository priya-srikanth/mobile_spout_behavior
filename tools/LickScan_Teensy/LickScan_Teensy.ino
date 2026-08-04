// LickScan_Teensy.ino
// Read-only pin scanner for the shared Teensy 4.1 on Pavel's breakout board.
//
// PURPOSE: settle which physical pin actually carries each lick-detector signal,
// without trusting any firmware comment. Every pin below is configured as an
// INPUT and never driven, so this sketch cannot fight the lick board, cannot
// open a valve, and cannot command a laser. It is safe to run with the rig
// fully connected.
//
// USAGE
//   1. Flash. Open Serial Monitor at 115200.
//   2. Type 'p' + Enter to toggle the internal pulldowns on/off. Compare both:
//      a pin whose reading follows the pulldown is FLOATING (nothing on it).
//      A pin that holds its level regardless is DRIVEN by external hardware.
//   3. Touch the LEFT spout. Note which pin(s) change. Repeat for the RIGHT.
//   4. Record the pin numbers, then set PIN_LICK_LEFT_IN in the main firmware.
//
// Only pins that change when you touch a spout are lick lines. Pins that read
// a steady 1 with pulldowns enabled are being actively driven high by something
// and must never be configured as OUTPUT in your firmware.

#include <Arduino.h>

// Accessible input candidates on this board (10-12 and 24-39 are unreachable
// with the Teensy seated; 13 is the onboard LED; 9 sits behind an RC filter).
static const uint8_t SCAN_PINS[] = {
  0, 1, 2, 3, 4, 5, 6, 7, 8,
  14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
  40, 41
};
static const uint8_t NUM_SCAN = sizeof(SCAN_PINS) / sizeof(SCAN_PINS[0]);

static bool usePulldown = true;
static uint8_t lastState[NUM_SCAN];
static uint32_t changeCount[NUM_SCAN];
static uint32_t lastReportMs = 0;

void applyModes() {
  for (uint8_t i = 0; i < NUM_SCAN; i++) {
    pinMode(SCAN_PINS[i], usePulldown ? INPUT_PULLDOWN : INPUT);
  }
}

void printHeader() {
  Serial.println();
  Serial.print("=== mode: INPUT");
  Serial.print(usePulldown ? "_PULLDOWN" : " (floating)");
  Serial.println(" ===");
  Serial.print("pin:   ");
  for (uint8_t i = 0; i < NUM_SCAN; i++) {
    Serial.print(SCAN_PINS[i]);
    Serial.print(SCAN_PINS[i] < 10 ? "  " : " ");
  }
  Serial.println();
}

void setup() {
  Serial.begin(115200);
  delay(600);
  applyModes();
  for (uint8_t i = 0; i < NUM_SCAN; i++) {
    lastState[i] = digitalRead(SCAN_PINS[i]);
    changeCount[i] = 0;
  }
  Serial.println("LickScan - all pins are INPUTs, nothing is driven.");
  Serial.println("Type 'p' + Enter to toggle internal pulldowns.");
  Serial.println("Type 'r' + Enter to reset the change counters.");
  printHeader();
}

void loop() {
  // Edge counting at full loop rate catches brief lick contacts that a
  // once-per-200ms snapshot would miss entirely.
  for (uint8_t i = 0; i < NUM_SCAN; i++) {
    uint8_t v = digitalRead(SCAN_PINS[i]);
    if (v != lastState[i]) {
      lastState[i] = v;
      changeCount[i]++;
    }
  }

  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == 'p' || c == 'P') {
      usePulldown = !usePulldown;
      applyModes();
      printHeader();
    } else if (c == 'r' || c == 'R') {
      for (uint8_t i = 0; i < NUM_SCAN; i++) changeCount[i] = 0;
      Serial.println("-- counters reset --");
    }
  }

  if (millis() - lastReportMs >= 250) {
    lastReportMs = millis();
    Serial.print("state: ");
    for (uint8_t i = 0; i < NUM_SCAN; i++) {
      Serial.print(lastState[i]);
      Serial.print(SCAN_PINS[i] < 10 ? "  " : "  ");
    }
    Serial.print("   edges: ");
    for (uint8_t i = 0; i < NUM_SCAN; i++) {
      if (changeCount[i] > 0) {
        Serial.print("p");
        Serial.print(SCAN_PINS[i]);
        Serial.print("=");
        Serial.print(changeCount[i]);
        Serial.print(" ");
      }
    }
    Serial.println();
  }
}
