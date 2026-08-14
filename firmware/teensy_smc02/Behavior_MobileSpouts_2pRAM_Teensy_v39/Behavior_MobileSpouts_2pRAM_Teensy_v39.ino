#include <math.h>

// Forward declaration to keep the Arduino preprocessor from emitting
// an auto-generated prototype before the enum definition below.
enum RewardTriggerType : uint8_t;

// ============================================================
// Teensy 4.1 firmware  -  v39
// Backend: SMC02 timed motion controllers in P02 mode
// Protocol: v2 structured line protocol (UNCHANGED from v36, GUI v44 compatible)
//
// v39 changes vs v38 (2026-08-14):
//   1. The lick input now defaults to INPUT_PULLUP on both rigs, matching the
//      digital-input assumptions of the current rigs. This makes the line much less likely to
//      float when the detector output is open-drain / weakly driven / disconnected.
//   2. Lick debug output now advertises mode=digital so the GUI can render it honestly instead of
//      pretending a red analog threshold line is meaningful.
//   3. SMC02 CW/CCW/STOP lines are released immediately at sketch startup, before the rest of
//      setup runs, so a Teensy reset spends less time with the motor-control pins undefined.
//
// v38 changes vs v37 (2026-08-10):
//   Interrupt-driven lick-onset capture (see lickISR()/updateLick()). The v37 polled debounce was
//   a lockout that dropped brief/fast contacts, so the ENL was not enforced on ~16% of real licks
//   the DAQ recorded. A CHANGE interrupt on the lick pin now latches every onset, immune to loop/
//   serial/motion latency. Serial protocol unchanged. NOT yet bench-tested -- flash + verify
//   lick_on / pre_cue_reset_by_lick before deploying.
//
// v37 changes vs v36:
//   1. Rig-selectable pin map (see RIG_* below). 2pRAM shares a Teensy with
//      the 2-spout photometry task on Pavel's breakout board; GB219 keeps the
//      original dedicated-rig pinout.
//   2. SMC02 STOP lines removed on 2pRAM (P02 mode stops on CW/CCW release).
//      smcPressLine/smcReleaseLine now tolerate PIN_UNUSED.
//   3. Motion abort actually works: STOP mid-move halts the whole move
//      sequence, not just the current axis segment, and posMM is credited
//      only for distance actually travelled.
//   4. Blocking delay() calls in the motion and reward-calibration paths
//      replaced with serviced waits, so sync pulses no longer drop out.
// ============================================================

// -----------------------------
// RIG SELECT  -  set exactly one to 1
// -----------------------------
#define RIG_2PRAM 1     // shared Teensy on Pavel breakout board, 2-photon room
#define RIG_GB219 0     // dedicated Teensy, behavior room / photometry rig

#if (RIG_2PRAM + RIG_GB219) != 1
#error "Select exactly one rig: set RIG_2PRAM or RIG_GB219 to 1, the other to 0."
#endif

static const uint8_t PIN_UNUSED = 255;

// -----------------------------
// Pins
// -----------------------------
#if RIG_2PRAM
// ---- 2pRAM: shared breakout board, LEFT spout set only ----
// Pins fixed by the breakout board hardware:
static const uint8_t PIN_SPEAKER               = 2;   // board Speaker L (PWM)
static const uint8_t PIN_SYNC_OUT              = 3;   // board Sync BNC
static const uint8_t PIN_REWARD_LEFT_SOLENOID  = 5;   // board left spout driver
static const uint8_t PIN_LICK_LEFT_IN          = 14;  // board A0 lick detect (left)

// TTL out to DAQ. All of these are existing breakout-panel BNCs, so no new TTL
// wiring is needed: patch cables only. Every one is a plain TTL indicator
// output in the co-tenant firmware, never an actuator driver, so a latched
// level here cannot open a valve, fire a laser or light a cue.
//
// Pins 13 and 9 are NOT reachable on this board: 13 is the Teensy's own onboard
// LED (no header exists), and 9 feeds an on-PCB level converter + RC filter,
// so only the filtered output of that chain is exposed, not the pin.
static const uint8_t PIN_CUE_TTL               = 6;   // board SpeakerTTL BNC
static const uint8_t PIN_REWARD_LEFT_INDICATOR = 19;  // board LeftRewardIndicator BNC
static const uint8_t PIN_TTL_POS_STB           = 7;   // board TrialStartPin BNC
static const uint8_t PIN_TTL_POS0              = 8;   // board ITIIndicator BNC
static const uint8_t PIN_TTL_POS1              = 17;  // board LeftCueIndicator BNC
static const uint8_t PIN_TTL_POS2              = 18;  // board RightCueIndicator BNC
static const uint8_t PIN_TTL_TRIAL_STOP        = 20;  // board RightRewardIndicator BNC

// No separate trial-start TTL on this rig. The position strobe on pin 7 is the
// hardware trial marker. NOTE it fires AFTER the move completes, so it marks
// target arrival, not trial onset -- those differ by the move duration, which
// varies with position. The "trial_start" serial event still carries true onset.
static const uint8_t PIN_TTL_TRIAL             = PIN_UNUSED;

// SMC02 motion-control lines, direct button emulation:
//   released = pin INPUT (high-Z)
//   pressed  = pin OUTPUT LOW (pull to SMC02 control GND)
// STOP lines omitted: in P02 mode the motor stops when CW/CCW is released.
//
// CRITICAL: these six MUST live on pins the co-tenant firmware never calls
// pinMode() on, otherwise his sketch drives them as outputs and a LOW reads as
// "button pressed" -- his sessions would command your stages for hours.
// Pins 25-39 are never declared in teensy2spout.ino and are therefore safe.
// (Pin 24 is NOT: it is his DelayIndicatorPin and he does configure it.)
static const uint8_t PIN_X_CW   = 25;
static const uint8_t PIN_X_CCW  = 26;
static const uint8_t PIN_X_STOP = PIN_UNUSED;
static const uint8_t PIN_Y_CW   = 27;
static const uint8_t PIN_Y_CCW  = 28;
static const uint8_t PIN_Y_STOP = PIN_UNUSED;
static const uint8_t PIN_Z_CW   = 29;
static const uint8_t PIN_Z_CCW  = 30;
static const uint8_t PIN_Z_STOP = PIN_UNUSED;

// Never driven by this firmware (belong to the co-tenant task):
//   4 = right spout solenoid driver    15 = Speaker R
//   21 = random light driver           22 = external sync TTL input
//   40 = opto DAC0                     41 = right lick detect (drives INTO the
//   1  = opto reporter LED                  Teensy - never set OUTPUT)
// Spare and free: 0, 16, 23, 31-39

// Lick polarity: ACTIVE-LOW (line idles HIGH, pulls LOW on contact), matching
// the v36 default that is validated on the GB219 rig.
//
// NOTE the co-tenant firmware sets INPUT_PULLDOWN on this same pin, which
// implies ITS detector is active-HIGH. Whether that matters depends on which
// physical detector board is wired to pin 14 on this rig. If licks never
// register, or register constantly, that is the first thing to suspect:
//   SET lick.active_low=false
// INPUT_PULLUP is used rather than the bare INPUT of v36 so a disconnected or
// open-drain detector idles HIGH (no lick) instead of floating into spurious
// detections. A push-pull detector overrides the ~22k internal pullup easily.
static const bool DEFAULT_LICK_ACTIVE_LOW = true;
static const uint8_t LICK_PIN_MODE        = INPUT_PULLUP;

// Trial marker style: plain pulses, same as GB219 and the Zaber rig, so one
// analysis pipeline serves all three. PIN_TTL_TRIAL_STOP (pin 20) pulses at
// dock; the position strobe (pin 7) marks target arrival.
static const bool TRIAL_TTL_IS_GATE = false;

#else
// ---- GB219: dedicated Teensy, behavior room / photometry rig ----
// Original v36 pinout, unchanged. Only the v37 logic fixes apply here.
static const uint8_t PIN_SYNC_OUT              = 2;
static const uint8_t PIN_SPEAKER               = 3;   // audio PWM out
static const uint8_t PIN_TTL_TRIAL_STOP        = 4;   // trial-stop TTL
static const uint8_t PIN_REWARD_LEFT_SOLENOID  = 5;
static const uint8_t PIN_CUE_TTL               = 6;   // cue/event TTL to DAQ
static const uint8_t PIN_TTL_TRIAL             = 10;  // trial-start TTL to DAQ

static const uint8_t PIN_X_CW   = 7;
static const uint8_t PIN_X_CCW  = 8;
static const uint8_t PIN_X_STOP = 9;
static const uint8_t PIN_Y_CW   = 23;
static const uint8_t PIN_Y_CCW  = 22;
static const uint8_t PIN_Y_STOP = 21;
static const uint8_t PIN_Z_CW   = 18;
static const uint8_t PIN_Z_CCW  = 17;
static const uint8_t PIN_Z_STOP = 16;

static const uint8_t PIN_REWARD_LEFT_INDICATOR = 19;
static const uint8_t PIN_LICK_LEFT_IN          = 15;  // single lick input
static const uint8_t PIN_TTL_POS0              = 14;
static const uint8_t PIN_TTL_POS_STB           = 20;
static const uint8_t PIN_TTL_POS2              = 41;
static const uint8_t PIN_TTL_POS1              = 40;

static const bool DEFAULT_LICK_ACTIVE_LOW = true;     // matches current digital lick boards
static const uint8_t LICK_PIN_MODE        = INPUT_PULLUP;

// Keep v36 behavior: separate trial-start pulse (pin 10) and trial-stop pulse
// (pin 4), plus the full 3-bit position bus and per-axis STOP lines.
static const bool TRIAL_TTL_IS_GATE = false;
#endif

static const bool USE_HOME_SWITCHES = false;

// no home switches in this build
static const uint8_t NUM_POSITIONS = 6;
static const uint8_t NUM_DISTANCE_TIERS = 2;
static const uint8_t NUM_AZIMUTHS = 3;

static const uint32_t TTL_PULSE_MS = 8;
static const uint32_t POSITION_STROBE_MS = 10;
// Serial position-code timing (rigs with no parallel POS bus). Index idx is
// sent as (idx+1) pulses. The frame is padded to a constant total duration so
// that cue latency does not vary systematically with position.
static const uint32_t POSITION_CODE_PULSE_MS = 6;
static const uint32_t POSITION_CODE_GAP_MS   = 6;
static const uint32_t MOVE_SETTLE_MS = 20;
static const uint32_t SMC02_STOP_PULSE_MS = 80;
static const uint32_t LICK_REARM_AFTER_MOTION_MS = 75;

static const uint32_t DEFAULT_CUE_DURATION_MS = 1000;
static const uint32_t DEFAULT_CUE_FREQUENCY_HZ = 6000;
static const uint8_t  DEFAULT_CUE_VOLUME_PCT = 60;
static const uint32_t DEFAULT_POST_REWARD_HOLD_MS = 10000;
static const uint32_t DEFAULT_RESPONSE_WINDOW_MS = 5000;
static const uint32_t DEFAULT_REWARD_VALVE_MS = 60;
static const uint32_t DEFAULT_SETTLE_MS = 100;
static const uint32_t DEFAULT_PRE_CUE_MIN_MS = 100;
static const uint32_t DEFAULT_PRE_CUE_MAX_MS = 300;
static const uint32_t DEFAULT_ITI_MIN_MS = 1500;
static const uint32_t DEFAULT_ITI_JITTER_MS = 500;
static const uint32_t DEFAULT_AUTO_REWARD_DELAY_MS = 500;
static const uint32_t DEFAULT_FREE_REWARD_AFTER_MISSES = 6;

static const uint8_t  DEFAULT_HITS_TO_ADVANCE = 2;
static const float    DEFAULT_ADAPT_STEP_MM = 0.5f;
static const uint32_t DEFAULT_TARGET_TRIALS_PER_POSITION = 50;
static const uint32_t DEFAULT_MAX_DURATION_MIN = 60;
static const uint32_t DEFAULT_SYNC_PULSE_MS = 2;
static const uint32_t DEFAULT_SYNC_MIN_INTERVAL_MS = 70;
static const uint32_t DEFAULT_SYNC_MAX_INTERVAL_MS = 170;
static const uint32_t MOTOR_DIRECTION_GUARD_MS = 10;

// -----------------------------
// Helpers / structs
// -----------------------------
struct Vec3 {
  float x;
  float y;
  float z;
};

struct Axis {
  const char* name;
  uint8_t pinCW;
  uint8_t pinCCW;
  uint8_t pinSTOP;
  uint8_t pinHome;

  bool cwIsPositive = true;
  float posMM = 0.0f;
  float homePosMM = 0.0f;

  float msPerMMPos = 220.0f;
  float msPerMMNeg = 220.0f;
  float overheadMs = 15.0f;

  float backoffMM = 1.0f;
};

struct BackendAxisMeta {
  int mode = 2;                 // P02 for SMC02
  int microstep = 8;
  float leadMMRev = 1.0f;       // T6*1 => 1 mm / rev
  float rpm = 400.0f;
};

struct GeometryConfig {
  float distanceTierMm[NUM_DISTANCE_TIERS] = {3.0f, 6.0f};
  float azimuthDeg[NUM_AZIMUTHS] = {0.0f, 45.0f, -45.0f};
  float downwardAngleDeg = 30.0f;
  float headRollDeg = 15.0f;  // positive => left higher than right
};

struct AdaptiveConfig {
  bool enabled = false;
  int hitsToAdvance = 2;
  int missesToDecrease = 2;
  float stepMm = 0.5f;
  float decreaseStepMm = 0.5f;
  float minDistanceMm = 3.0f;
  float maxDistanceMm = 8.0f;
};

struct AdaptivePositionConfig {
  bool enabled = false;
  int hitsToAdvance = 2;
  int missesToDecrease = 2;
  float stepMm = 0.5f;
  float decreaseStepMm = 0.5f;
  float minDistanceMm = 3.0f;
  float maxDistanceMm = 8.0f;
};

struct FreeRewardConfig {
  bool enabled = true;
  int afterConsecutiveMisses = 6;
  uint32_t delayAfterCueMs = 500;
};

enum RewardMode {
  REWARD_MODE_CONTINGENT = 0,
  REWARD_MODE_AUTO = 1,
  REWARD_MODE_CONTINGENT_OR_AUTO = 2
};

enum ScheduleMode : uint8_t {
  SCHEDULE_BALANCED_BLOCK_CYCLES = 0,
  SCHEDULE_RANDOM_BLOCKS = 1
};

enum StopMode : uint8_t {
  STOP_END_OF_CURRENT_BLOCK = 0,
  STOP_END_OF_BALANCED_CYCLE = 1
};

enum StopPendingReason : uint8_t {
  STOP_PENDING_NONE = 0,
  STOP_PENDING_MAX_DURATION = 1,
  STOP_PENDING_TARGET_REACHED = 2
};


uint8_t selectedLickInputPin() {
  return PIN_LICK_LEFT_IN;
}

uint8_t selectedRewardSolenoidPin() {
  return PIN_REWARD_LEFT_SOLENOID;
}

uint8_t selectedRewardIndicatorPin() {
  return PIN_REWARD_LEFT_INDICATOR;
}

const char* rewardModeName(int mode) {
  switch (mode) {
    case REWARD_MODE_CONTINGENT: return "contingent";
    case REWARD_MODE_AUTO: return "auto_after_delay";
    case REWARD_MODE_CONTINGENT_OR_AUTO: return "contingent_or_auto_after_delay";
    default: return "contingent";
  }
}

struct BehaviorConfig {
  bool sessionRunning = false;
  int rewardMode = REWARD_MODE_CONTINGENT;
  bool enforceNoLick = true;
  bool manualRewardAllowed = true;

  uint32_t rewardOpenMs = 25;
  uint32_t autoRewardDelayMs = 500;
  bool autoHoldAfterMissEnabled = false;
  uint32_t autoHoldAfterMissThreshold = 3;
  float estimatedRewardUL = 2.5f;
  float sessionWaterLimitUL = 1000.0f;

  uint32_t settleMs = DEFAULT_SETTLE_MS;
  uint32_t postRewardHoldMs = DEFAULT_POST_REWARD_HOLD_MS;
  uint32_t preCueMinMs = DEFAULT_PRE_CUE_MIN_MS;
  uint32_t preCueMaxMs = DEFAULT_PRE_CUE_MAX_MS;
  uint32_t responseWindowMs = DEFAULT_RESPONSE_WINDOW_MS;
  uint32_t itiMinMs = DEFAULT_ITI_MIN_MS;
  uint32_t itiJitterMs = DEFAULT_ITI_JITTER_MS;

  uint8_t blockSize = 5;
  uint8_t blockSizeMin = 5;
  uint8_t blockSizeMax = 5;
  bool targetTrialsPerPositionEnabled = true;
  uint32_t targetTrialsPerPosition = DEFAULT_TARGET_TRIALS_PER_POSITION;
  bool maxDurationEnabled = false;
  uint32_t maxDurationMin = DEFAULT_MAX_DURATION_MIN;
  uint8_t scheduleMode = SCHEDULE_BALANCED_BLOCK_CYCLES;
  uint8_t stopMode = STOP_END_OF_CURRENT_BLOCK;

  bool enabledPositions[NUM_POSITIONS] = {true, true, true, true, true, true};
};

struct LickConfig {
  bool activeLow = DEFAULT_LICK_ACTIVE_LOW;
  uint32_t debounceMs = 20;
  // legacy fields retained for GUI/protocol compatibility, unused for digital lick input
  float baselineAlpha = 0.005f;
  int thresholdCounts = 500;
  int hysteresisCounts = 150;
  int polarity = -1;
  uint32_t refractoryMs = 20;
  bool debug = false;
};

struct CueConfig {
  uint32_t frequencyHz = DEFAULT_CUE_FREQUENCY_HZ;
  uint32_t durationMs = DEFAULT_CUE_DURATION_MS;
  uint8_t volumePct = DEFAULT_CUE_VOLUME_PCT;
};

// -----------------------------
// Globals
// -----------------------------
Axis axisX = {"x", PIN_X_CW, PIN_X_CCW, PIN_X_STOP, PIN_UNUSED};
Axis axisY = {"y", PIN_Y_CW, PIN_Y_CCW, PIN_Y_STOP, PIN_UNUSED};
Axis axisZ = {"z", PIN_Z_CW, PIN_Z_CCW, PIN_Z_STOP, PIN_UNUSED};

BackendAxisMeta metaX;
BackendAxisMeta metaY;
BackendAxisMeta metaZ;

GeometryConfig geom;
AdaptiveConfig adapt;
bool adaptUsePerPosition = false;
AdaptivePositionConfig adaptPos[NUM_POSITIONS];
FreeRewardConfig freeRewardCfg;
BehaviorConfig cfg;
LickConfig lickCfg;
CueConfig cueCfg;

Vec3 mouthOrigin = {0.0f, 0.0f, 0.0f};
Vec3 dockPosition = {0.0f, -10.0f, -5.0f};
Vec3 safePosition = {0.0f, 0.0f, -5.0f};

Vec3 positions[NUM_POSITIONS];
float currentDistanceMm[NUM_POSITIONS];
uint8_t positionTierIndex[NUM_POSITIONS] = {0, 0, 0, 1, 1, 1};
uint8_t positionAzIndex[NUM_POSITIONS]   = {0, 1, 2, 0, 1, 2};

uint32_t totalTrials = 0;
uint32_t totalHits = 0;
uint32_t totalMisses = 0;
uint32_t totalFreeRewards = 0;
uint32_t totalAutoRewards = 0;
uint32_t totalRewards = 0;
uint32_t currentBlockNumber = 0;
uint32_t enlViolationCount = 0;
float sessionWaterDeliveredUL = 0.0f;
uint32_t syncPulseCount = 0;

uint32_t trialsPerPosition[NUM_POSITIONS];
uint32_t hitsPerPosition[NUM_POSITIONS];
uint32_t missesPerPosition[NUM_POSITIONS];
uint32_t freeRewardsPerPosition[NUM_POSITIONS];
uint32_t adaptiveHitCounterPerPosition[NUM_POSITIONS];
uint32_t adaptiveMissCounterPerPosition[NUM_POSITIONS];

int currentBlockPos = -1;
uint32_t trialsInCurrentBlock = 0;
uint8_t currentBlockSize = 0;
int consecutiveMisses = 0;
uint32_t sessionStartMs = 0;
bool stopPending = false;
StopPendingReason stopPendingReason = STOP_PENDING_NONE;
int cycleQueue[NUM_POSITIONS];
uint8_t cycleQueueLen = 0;
uint8_t cycleQueueIndex = 0;
uint32_t cycleNumber = 0;

int currentTrialPos = -1;
bool freeRewardThisTrial = false;
bool freeRewardDeliveredThisTrial = false;
bool successfulLickThisTrial = false;
bool rewardIssuedThisTrial = false;
uint32_t freeRewardAtMs = 0;
uint32_t autoRewardAtMs = 0;
uint32_t responseDeadlineMs = 0;
uint32_t itiEndAtMs = 0;
uint32_t cueOffAtMs = 0;
bool cueActive = false;
bool taskRewardsHeld = false;
bool manualRewardsHeld = false;
bool autoRewardsHeld = false;
String serialRxBuffer = "";

// Set by any STOP handler. Every blocking motion loop polls this and unwinds
// the whole move sequence, not just the segment that happens to be running.
// Cleared when a new motion request or session start arrives.
volatile bool abortMotion = false;

// Lick detection state
bool lickSensingEnabled = false;
bool lickCurrent = false;
volatile bool lickOnsetLatched = false;
// v38: lick-onset interrupt state (written by lickISR()).
volatile uint8_t  isrOnsetPending = 0;   // onsets captured by the ISR, drained in updateLick()
volatile uint32_t isrLastOnsetMs  = 0;   // ISR-side refractory anchor

inline void clearLatchedLick() {
  lickOnsetLatched = false;
}

enum RewardTriggerType : uint8_t {
  REWARD_TRIGGER_NONE = 0,
  REWARD_TRIGGER_CONTINGENT = 1,
  REWARD_TRIGGER_AUTO = 2,
  REWARD_TRIGGER_FREE = 3,
  REWARD_TRIGGER_MANUAL = 4,
  REWARD_TRIGGER_CALIBRATION = 5
};

RewardTriggerType pendingRewardTrigger = REWARD_TRIGGER_NONE;
uint32_t lastLickOnsetMs = 0;
uint32_t lastLickChangeMs = 0;
uint8_t lickRawDigital = 1;

// TTL pulse timers
uint32_t pulseEndTrial = 0;
uint32_t pulseEndCue = 0;
uint32_t pulseEndReward = 0;
uint32_t pulseEndLick = 0;
uint32_t pulseEndSync = 0;
uint32_t pulseEndTrialStop = 0;
uint32_t pulseEndPosStrobe = 0;
uint32_t nextSyncAt = 0;
uint32_t syncPulseMinMs = DEFAULT_SYNC_PULSE_MS;
uint32_t syncPulseMaxMs = DEFAULT_SYNC_PULSE_MS;
uint32_t syncMinIntervalMs = DEFAULT_SYNC_MIN_INTERVAL_MS;
uint32_t syncMaxIntervalMs = DEFAULT_SYNC_MAX_INTERVAL_MS;

// State machine
// -----------------------------
enum RunState {
  ST_IDLE,
  ST_MOVE_TO_TARGET,
  ST_SETTLE,
  ST_PRE_CUE,
  ST_WAIT_FOR_LICK,
  ST_DELIVER_REWARD,
  ST_POST_REWARD_HOLD,
  ST_RETURN_TO_DOCK,
  ST_ITI
};

RunState runState = ST_IDLE;
uint32_t stateStartMs = 0;
uint32_t cueEligibleAtMs = 0;

// -----------------------------
// Forward declarations
// -----------------------------
void serviceCore();
void serviceWait(uint32_t ms);
void updateTTLPulses();
void updateSync();
void updateLick();
void lickISR();          // v38: lick-onset interrupt (defined alongside updateLick)
void updateCue();
void handleSerial();
bool handleSerialDuringBlocking();
bool readSerialLineNonBlocking(String &line);

void pulsePin(uint8_t pin, uint32_t durMs);
void smcReleaseLine(uint8_t pin);
void smcPressLine(uint8_t pin);
void smcAllRelease(const Axis& a);

void emitInfoReady();
void emitOK(const String& cmd, const String& extra = "");
void emitErr(const String& cmd, const String& code, const String& detail = "");
void emitEvent(const char* name);
void emitEventDetail(const char* name, const String& detail);
void emitStatus();
void emitPositions();
void emitConfig();
void emitStats();
void emitConfigKV(const String& key, const String& value);
void emitConfigKV(const String& key, int value);
void emitConfigKV(const String& key, uint32_t value);
void emitConfigKV(const String& key, float value, int digits = 3);
void emitConfigKV(const String& key, bool value);

const char* stateName(RunState s);
const char* rewardTriggerName(RewardTriggerType trigger);

bool consumeLickOnset();

bool homeAllAxes();
bool homeAxis(Axis& a);
bool moveAxisRelative(Axis& a, float deltaMM);
bool moveToPositionSafe(const Vec3& target);
void axisStop(Axis& a);
void allAxesStop();

void playCue();
void cueOnly();
void closeTrialAndCueGates();
void openRewardValve(uint32_t durMs, bool bypassHold=false);

void recomputeAllGeneratedPositions();
void recomputePosition(uint8_t idx);
// These two were relied on the Arduino IDE's auto-generated prototypes in v36.
// Declared explicitly so the sketch also builds under a plain C++ compiler.
void syncAdaptivePositionsFromGlobal();
bool deliverRewardForTrigger(RewardTriggerType trigger);
void resetAdaptiveDistances();
void resetSessionStats();
void refreshRewardHoldState(bool emitCfg=false);
void setManualRewardHold(bool held, bool emitCfg=true, bool emitEvt=true);
void setAutoRewardHold(bool held, bool emitCfg=true, bool emitEvt=true);
void clearAllRewardHolds(bool emitCfg=true, bool emitEvt=true);
void updateAutoRewardHoldFromMissStreak(bool emitCfg=true);
void chooseNextBlockPosition();
void startNextTrial();
void emitPositionCode(uint8_t idx);

void registerHit(uint8_t posIdx);
void registerMiss(uint8_t posIdx);
void maybeAdvanceDifficulty(uint8_t posIdx);
void maybeDecreaseDifficulty(uint8_t posIdx);
const char* scheduleModeName();
const char* stopModeName();
void normalizeSchedulerConfig();
bool positionNeedsTrials(uint8_t posIdx);
bool allTargetTrialsReached();
void buildBalancedCycle();
uint8_t sampleBlockSizeForPosition(uint8_t posIdx);
void requestStopPending(StopPendingReason reason);
void updateSessionStopChecks();
bool shouldStopBeforeStartingNextBlock();

uint32_t sampleITI();
void setCurrentPosition(float x, float y, float z);

String getArg(const String& line, const String& key);
bool parseBoolValue(const String& s, bool &out);
bool parseIntValue(const String& s, int &out);
bool parseUIntValue(const String& s, uint32_t &out);
bool parseFloatValue(const String& s, float &out);
Axis* getAxisByName(const String& s);
BackendAxisMeta* getMetaByAxisName(const String& s);
String normalizeLegacySetKey(const String& key);
bool handleSet(const String& key, const String& value);
void handleGet(const String& kind);
void handleMove(const String& rest);
void handleCal(const String& rest);
void handleHelp();

// ============================================================
// Setup
// ============================================================
void setup() {
  // Release SMC02 lines first so a sketch restart spends as little time as possible
  // with the motion-control pins floating or actively driven.
  smcAllRelease(axisX);
  smcAllRelease(axisY);
  smcAllRelease(axisZ);

  // Preload safe inactive levels before enabling outputs, so reset/boot does not
  // momentarily energize the solenoid or other TTL lines during serial connect.
  digitalWrite(PIN_REWARD_LEFT_SOLENOID, LOW);
  analogWrite(PIN_SPEAKER, 0);
  if (PIN_TTL_TRIAL != PIN_UNUSED) digitalWrite(PIN_TTL_TRIAL, LOW);
  digitalWrite(PIN_CUE_TTL, LOW);
  digitalWrite(PIN_REWARD_LEFT_INDICATOR, LOW);
  digitalWrite(PIN_SYNC_OUT, LOW);
  digitalWrite(PIN_TTL_TRIAL_STOP, LOW);
  if (PIN_TTL_POS0 != PIN_UNUSED) {
    digitalWrite(PIN_TTL_POS0, LOW);
    digitalWrite(PIN_TTL_POS1, LOW);
    digitalWrite(PIN_TTL_POS2, LOW);
  }
  digitalWrite(PIN_TTL_POS_STB, LOW);

  pinMode(PIN_REWARD_LEFT_SOLENOID, OUTPUT);
  pinMode(PIN_SPEAKER, OUTPUT);
  if (PIN_TTL_TRIAL != PIN_UNUSED) pinMode(PIN_TTL_TRIAL, OUTPUT);
  pinMode(PIN_CUE_TTL, OUTPUT);
  pinMode(PIN_REWARD_LEFT_INDICATOR, OUTPUT);
  pinMode(PIN_SYNC_OUT, OUTPUT);
  pinMode(PIN_TTL_TRIAL_STOP, OUTPUT);
  if (PIN_TTL_POS0 != PIN_UNUSED) {
    pinMode(PIN_TTL_POS0, OUTPUT);
    pinMode(PIN_TTL_POS1, OUTPUT);
    pinMode(PIN_TTL_POS2, OUTPUT);
  }
  pinMode(PIN_TTL_POS_STB, OUTPUT);

  Serial.begin(115200);
  delay(300);

  randomSeed((uint32_t)micros());

  analogWriteResolution(8);

  // SMC02 direct-control lines idle as INPUT (high impedance) and
  // are actively asserted by switching to OUTPUT LOW.
  smcAllRelease(axisX);
  smcAllRelease(axisY);
  smcAllRelease(axisZ);

  // Z convention: positive user Z is upward.
  axisZ.cwIsPositive = false;


  pinMode(PIN_LICK_LEFT_IN, LICK_PIN_MODE);
  attachInterrupt(digitalPinToInterrupt(selectedLickInputPin()), lickISR, CHANGE);  // v38: capture every lick onset

  syncAdaptivePositionsFromGlobal();
  resetAdaptiveDistances();
  resetSessionStats();
  recomputeAllGeneratedPositions();
  normalizeSchedulerConfig();

  nextSyncAt = millis() + random(syncMinIntervalMs, syncMaxIntervalMs + 1);

  emitInfoReady();
}

// ============================================================
// Main loop
// ============================================================
void loop() {
  serviceCore();
  handleSerial();
  updateSessionStopChecks();

  switch (runState) {
    case ST_IDLE:
      break;

    case ST_MOVE_TO_TARGET:
      startNextTrial();
      break;

    case ST_SETTLE:
      if (millis() - stateStartMs >= cfg.settleMs) {
        cueEligibleAtMs = millis() + random(cfg.preCueMinMs, cfg.preCueMaxMs + 1);
        lickSensingEnabled = true;
        runState = ST_PRE_CUE;
        stateStartMs = millis();
      }
      break;

    case ST_PRE_CUE:
      if (cfg.enforceNoLick && consumeLickOnset()) {
        cueEligibleAtMs = millis() + random(cfg.preCueMinMs, cfg.preCueMaxMs + 1);
        enlViolationCount++;
        emitEvent("pre_cue_reset_by_lick");
      }

      if (millis() >= cueEligibleAtMs) {
        playCue();   // opens the cue gate on PIN_CUE_TTL; updateCue() closes it
        emitEvent("cue");

        responseDeadlineMs = millis() + cfg.responseWindowMs;
        autoRewardAtMs = millis() + cfg.autoRewardDelayMs;

        if (freeRewardThisTrial) {
          freeRewardAtMs = millis() + freeRewardCfg.delayAfterCueMs;
        }

        runState = ST_WAIT_FOR_LICK;
        stateStartMs = millis();
      }
      break;

    case ST_WAIT_FOR_LICK:
      if (cfg.rewardMode == REWARD_MODE_CONTINGENT) {
        if (consumeLickOnset()) {
          if (currentTrialPos >= 0) registerHit((uint8_t)currentTrialPos);
          pendingRewardTrigger = REWARD_TRIGGER_CONTINGENT;
          runState = ST_DELIVER_REWARD;
          stateStartMs = millis();
        } else if (!rewardIssuedThisTrial && freeRewardThisTrial && !freeRewardDeliveredThisTrial && millis() >= freeRewardAtMs) {
          pendingRewardTrigger = REWARD_TRIGGER_FREE;
          runState = ST_DELIVER_REWARD;
          stateStartMs = millis();
        } else if (millis() >= responseDeadlineMs) {
          if (currentTrialPos >= 0) registerMiss((uint8_t)currentTrialPos);
          lickSensingEnabled = false;
          clearLatchedLick();
          runState = ST_RETURN_TO_DOCK;
          stateStartMs = millis();
        }
      } else if (cfg.rewardMode == REWARD_MODE_AUTO) {
        if (!successfulLickThisTrial && millis() < responseDeadlineMs && consumeLickOnset()) {
          successfulLickThisTrial = true;
          if (currentTrialPos >= 0) registerHit((uint8_t)currentTrialPos);
          if (rewardIssuedThisTrial) {
            lickSensingEnabled = false;
            clearLatchedLick();
            runState = ST_POST_REWARD_HOLD;
            stateStartMs = millis();
          }
        }
        if (!rewardIssuedThisTrial && millis() >= autoRewardAtMs) {
          rewardIssuedThisTrial = true;
          deliverRewardForTrigger(REWARD_TRIGGER_AUTO);
          if (successfulLickThisTrial) {
            lickSensingEnabled = false;
            clearLatchedLick();
            runState = ST_POST_REWARD_HOLD;
            stateStartMs = millis();
          }
        } else if (millis() >= responseDeadlineMs) {
          if (!successfulLickThisTrial && currentTrialPos >= 0) registerMiss((uint8_t)currentTrialPos);
          lickSensingEnabled = false;
          clearLatchedLick();
          runState = ST_RETURN_TO_DOCK;
          stateStartMs = millis();
        }
      } else {
        if (!rewardIssuedThisTrial && consumeLickOnset()) {
          successfulLickThisTrial = true;
          if (currentTrialPos >= 0) registerHit((uint8_t)currentTrialPos);
          pendingRewardTrigger = REWARD_TRIGGER_CONTINGENT;
          runState = ST_DELIVER_REWARD;
          stateStartMs = millis();
        } else if (!rewardIssuedThisTrial && millis() >= autoRewardAtMs) {
          rewardIssuedThisTrial = true;
          deliverRewardForTrigger(REWARD_TRIGGER_AUTO);
        } else if (!rewardIssuedThisTrial && freeRewardThisTrial && !freeRewardDeliveredThisTrial && millis() >= freeRewardAtMs) {
          pendingRewardTrigger = REWARD_TRIGGER_FREE;
          runState = ST_DELIVER_REWARD;
          stateStartMs = millis();
        } else if (rewardIssuedThisTrial && !successfulLickThisTrial && millis() < responseDeadlineMs && consumeLickOnset()) {
          successfulLickThisTrial = true;
          if (currentTrialPos >= 0) registerHit((uint8_t)currentTrialPos);
          lickSensingEnabled = false;
          clearLatchedLick();
          runState = ST_POST_REWARD_HOLD;
          stateStartMs = millis();
        } else if (millis() >= responseDeadlineMs && (!freeRewardThisTrial || rewardIssuedThisTrial || freeRewardDeliveredThisTrial)) {
          if (!successfulLickThisTrial && currentTrialPos >= 0) registerMiss((uint8_t)currentTrialPos);
          lickSensingEnabled = false;
          clearLatchedLick();
          runState = ST_RETURN_TO_DOCK;
          stateStartMs = millis();
        }
      }
      break;

    case ST_DELIVER_REWARD:
      deliverRewardForTrigger(pendingRewardTrigger);
      stateStartMs = millis();
      runState = ST_POST_REWARD_HOLD;
      break;

    case ST_POST_REWARD_HOLD:
      if (millis() - stateStartMs >= cfg.postRewardHoldMs) {
        lickSensingEnabled = false;
        clearLatchedLick();
        runState = ST_RETURN_TO_DOCK;
        stateStartMs = millis();
      }
      break;

    case ST_RETURN_TO_DOCK:
      emitEvent("dock_start");
      if (TRIAL_TTL_IS_GATE) {
        digitalWrite(PIN_TTL_TRIAL_STOP, LOW);    // trial gate closes: trial end
      } else {
        pulsePin(PIN_TTL_TRIAL_STOP, TTL_PULSE_MS);
      }
      if (moveToPositionSafe(dockPosition)) {
        emitEvent("dock");
        itiEndAtMs = millis() + sampleITI();
        runState = ST_ITI;
        stateStartMs = millis();
      } else {
        cfg.sessionRunning = false;
        runState = ST_IDLE;
      }
      break;

    case ST_ITI:
      if (millis() >= itiEndAtMs) {
        if (cfg.sessionRunning) {
          runState = ST_MOVE_TO_TARGET;
        } else {
          runState = ST_IDLE;
        }
      }
      break;
  }
}

// ============================================================
// Core service
// ============================================================
void serviceCore() {
  updateTTLPulses();
  updateSync();
  updateCue();
  updateLick();
}

// ============================================================
// Motion
// ============================================================
// Blocking wait that keeps sync pulses, TTL pulse timers, cue timing and the
// serial link alive. Use this instead of delay() anywhere in the motion path.
void serviceWait(uint32_t ms) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    handleSerialDuringBlocking();
    serviceCore();
  }
}

// PIN_UNUSED (255) is out of range for the Teensy 4 pin lookup tables and would
// index off the end of them, so every SMC02 line helper has to tolerate it.
void smcReleaseLine(uint8_t pin) {
  if (pin == PIN_UNUSED) return;
  pinMode(pin, INPUT);   // high-Z = "button released"
}

void smcPressLine(uint8_t pin) {
  if (pin == PIN_UNUSED) return;
  digitalWrite(pin, LOW);
  pinMode(pin, OUTPUT);  // pull to SMC02 control GND = "button pressed"
}

void smcAllRelease(const Axis& a) {
  smcReleaseLine(a.pinCW);
  smcReleaseLine(a.pinCCW);
  smcReleaseLine(a.pinSTOP);
}

bool isHomeTriggered(const Axis& a) {
  if (a.pinHome == PIN_UNUSED) return false;
  return digitalRead(a.pinHome) == LOW;
}

void axisStop(Axis& a) {
  // Releasing CW/CCW is what actually halts the motor in P02 mode. The STOP
  // line is belt-and-braces and is absent on rigs wired without it.
  smcReleaseLine(a.pinCW);
  smcReleaseLine(a.pinCCW);
  if (a.pinSTOP == PIN_UNUSED) return;
  smcPressLine(a.pinSTOP);
  serviceWait(SMC02_STOP_PULSE_MS);
  smcReleaseLine(a.pinSTOP);
}

void allAxesStop() {
  axisStop(axisX);
  axisStop(axisY);
  axisStop(axisZ);
}

bool moveAxisRelative(Axis& a, float deltaMM) {
  if (fabsf(deltaMM) < 0.001f) return true;

  bool positiveMove = (deltaMM > 0.0f);
  uint8_t drivePin;

  if (positiveMove) {
    drivePin = a.cwIsPositive ? a.pinCW : a.pinCCW;
  } else {
    drivePin = a.cwIsPositive ? a.pinCCW : a.pinCW;
  }

  float travelMM = fabsf(deltaMM);
  float msPerMM = positiveMove ? a.msPerMMPos : a.msPerMMNeg;
  uint32_t moveMs = (uint32_t)roundf(a.overheadMs + travelMM * msPerMM);

  if (abortMotion) return false;

  smcReleaseLine(a.pinCW);
  smcReleaseLine(a.pinCCW);
  serviceWait(MOTOR_DIRECTION_GUARD_MS);
  if (abortMotion) return false;

  smcPressLine(drivePin);
  uint32_t t0 = millis();
  uint32_t elapsed = 0;
  while (true) {
    elapsed = millis() - t0;
    if (elapsed >= moveMs) { elapsed = moveMs; break; }
    handleSerialDuringBlocking();
    serviceCore();
    if (abortMotion) { elapsed = millis() - t0; break; }
  }
  smcReleaseLine(drivePin);

  // Credit only the distance actually travelled, so an aborted move does not
  // desync posMM from the physical stage. With no home switches there is no
  // way to recover from that except re-referencing by hand.
  float frac = 1.0f;
  if (elapsed < moveMs) {
    float travelWindowMs = (float)moveMs - a.overheadMs;
    if (travelWindowMs <= 0.0f) {
      frac = 0.0f;
    } else {
      frac = ((float)elapsed - a.overheadMs) / travelWindowMs;
      if (frac < 0.0f) frac = 0.0f;
      if (frac > 1.0f) frac = 1.0f;
    }
  }
  a.posMM += deltaMM * frac;

  if (abortMotion) {
    emitEventDetail("move_aborted",
                    String("axis=") + a.name +
                    " requested_mm=" + String(deltaMM, 3) +
                    " applied_mm=" + String(deltaMM * frac, 3));
    return false;
  }

  serviceWait(MOTOR_DIRECTION_GUARD_MS);
  return true;
}

bool homeAxis(Axis& a) {
  if (!USE_HOME_SWITCHES) return false;
  bool driveCW = !a.cwIsPositive;
  uint8_t drivePin = driveCW ? a.pinCW : a.pinCCW;

  uint32_t timeoutMs = 15000;
  uint32_t t0 = millis();

  smcPressLine(drivePin);
  while (!isHomeTriggered(a)) {
    handleSerialDuringBlocking();
    serviceCore();
    if (abortMotion || millis() - t0 > timeoutMs) {
      smcReleaseLine(drivePin);
      return false;
    }
  }
  smcReleaseLine(drivePin);
  serviceWait(MOTOR_DIRECTION_GUARD_MS);

  moveAxisRelative(a, a.backoffMM);

  t0 = millis();
  smcPressLine(drivePin);
  while (!isHomeTriggered(a)) {
    handleSerialDuringBlocking();
    serviceCore();
    if (abortMotion || millis() - t0 > timeoutMs) {
      smcReleaseLine(drivePin);
      return false;
    }
  }
  smcReleaseLine(drivePin);
  serviceWait(MOTOR_DIRECTION_GUARD_MS);

  a.posMM = a.homePosMM;
  return true;
}

bool homeAllAxes() {
  if (!USE_HOME_SWITCHES) return false;
  lickSensingEnabled = false;
  allAxesStop();

  bool okZ = homeAxis(axisZ);
  bool okX = homeAxis(axisX);
  bool okY = homeAxis(axisY);

  return okX && okY && okZ;
}

bool moveToPositionSafe(const Vec3& target) {
  lickSensingEnabled = false;
  if (abortMotion) return false;

  // Each moveAxisRelative returns false on abort, so a STOP part-way through
  // unwinds the remaining segments instead of driving on to the next axis.
  float safeZ = safePosition.z;
  if (!moveAxisRelative(axisZ, safeZ - axisZ.posMM)) return false;
  if (!moveAxisRelative(axisX, target.x - axisX.posMM)) return false;
  if (!moveAxisRelative(axisY, target.y - axisY.posMM)) return false;
  if (!moveAxisRelative(axisZ, target.z - axisZ.posMM)) return false;

  serviceWait(LICK_REARM_AFTER_MOTION_MS);
  return true;
}

// ============================================================
// Geometry
// ============================================================
void recomputePosition(uint8_t idx) {
  uint8_t azIx = positionAzIndex[idx];

  float r = currentDistanceMm[idx];
  float azDeg = geom.azimuthDeg[azIx];

  float phi = azDeg * PI / 180.0f;
  float theta = geom.downwardAngleDeg * PI / 180.0f;
  float roll = geom.headRollDeg * PI / 180.0f;

  float x0 = r * cosf(theta) * sinf(phi);
  float y0 = -r * cosf(theta) * cosf(phi);
  float z0 = -r * sinf(theta);

  // Rotate the full mouth-relative spout vector around the fore-aft axis.
  // Positive roll lowers mouse-right.
  float x1 = x0 * cosf(roll) + z0 * sinf(roll);
  float z1 = -x0 * sinf(roll) + z0 * cosf(roll);

  positions[idx].x = mouthOrigin.x + x1;
  positions[idx].y = mouthOrigin.y + y0;
  positions[idx].z = mouthOrigin.z + z1;
}

void recomputeAllGeneratedPositions() {
  for (int i = 0; i < NUM_POSITIONS; i++) {
    recomputePosition(i);
  }
}

void syncAdaptivePositionsFromGlobal() {
  for (int i = 0; i < NUM_POSITIONS; i++) {
    adaptPos[i].enabled = adapt.enabled;
    adaptPos[i].hitsToAdvance = adapt.hitsToAdvance;
    adaptPos[i].missesToDecrease = adapt.missesToDecrease;
    adaptPos[i].stepMm = adapt.stepMm;
    adaptPos[i].decreaseStepMm = adapt.decreaseStepMm;
    adaptPos[i].minDistanceMm = adapt.minDistanceMm;
    adaptPos[i].maxDistanceMm = adapt.maxDistanceMm;
  }
}

void resetAdaptiveDistances() {
  for (int i = 0; i < NUM_POSITIONS; i++) {
    currentDistanceMm[i] = geom.distanceTierMm[positionTierIndex[i]];
    adaptiveHitCounterPerPosition[i] = 0;
    adaptiveMissCounterPerPosition[i] = 0;
  }
}

void refreshRewardHoldState(bool emitCfg) {
  bool held = manualRewardsHeld || autoRewardsHeld;
  if (held == taskRewardsHeld) return;
  taskRewardsHeld = held;
  if (emitCfg) emitConfigKV("task.rewards_held", taskRewardsHeld);
}

void setManualRewardHold(bool held, bool emitCfg, bool emitEvt) {
  bool prev = manualRewardsHeld;
  manualRewardsHeld = held;
  refreshRewardHoldState(emitCfg);
  if (emitEvt && prev != held && cfg.sessionRunning) emitEvent(held ? "manual_reward_hold_on" : "manual_reward_hold_off");
}

void setAutoRewardHold(bool held, bool emitCfg, bool emitEvt) {
  bool prev = autoRewardsHeld;
  autoRewardsHeld = held;
  refreshRewardHoldState(emitCfg);
  if (emitEvt && prev != held && cfg.sessionRunning) emitEvent(held ? "auto_reward_hold_on" : "auto_reward_hold_off");
}

void clearAllRewardHolds(bool emitCfg, bool emitEvt) {
  setManualRewardHold(false, false, emitEvt);
  setAutoRewardHold(false, false, emitEvt);
  refreshRewardHoldState(emitCfg);
}

void updateAutoRewardHoldFromMissStreak(bool emitCfg) {
  if (!cfg.autoHoldAfterMissEnabled) return;
  if (!(cfg.rewardMode == REWARD_MODE_AUTO || cfg.rewardMode == REWARD_MODE_CONTINGENT_OR_AUTO)) return;
  if (cfg.autoHoldAfterMissThreshold < 1) return;
  if (consecutiveMisses >= (int)cfg.autoHoldAfterMissThreshold) {
    autoRewardsHeld = true;
    refreshRewardHoldState(false);
  }
}

float adaptiveMinDistanceForPosition(uint8_t posIdx) {
  float minDist = (adaptUsePerPosition && posIdx < NUM_POSITIONS) ? adaptPos[posIdx].minDistanceMm : adapt.minDistanceMm;
  if (posIdx < NUM_POSITIONS && positionTierIndex[posIdx] == 1) {
    if (geom.distanceTierMm[0] > minDist) minDist = geom.distanceTierMm[0];
  }
  return minDist;
}

float adaptiveMaxDistanceForPosition(uint8_t posIdx) {
  float maxDist = (adaptUsePerPosition && posIdx < NUM_POSITIONS) ? adaptPos[posIdx].maxDistanceMm : adapt.maxDistanceMm;
  if (posIdx < NUM_POSITIONS && positionTierIndex[posIdx] == 0) {
    if (geom.distanceTierMm[1] < maxDist) maxDist = geom.distanceTierMm[1];
  }
  return maxDist;
}

bool adaptiveEnabledForPosition(uint8_t posIdx) {
  if (!adapt.enabled) return false;
  if (!adaptUsePerPosition) return true;
  if (posIdx >= NUM_POSITIONS) return false;
  return adaptPos[posIdx].enabled;
}

int adaptiveHitsToAdvanceForPosition(uint8_t posIdx) {
  if (adaptUsePerPosition && posIdx < NUM_POSITIONS) return adaptPos[posIdx].hitsToAdvance;
  return adapt.hitsToAdvance;
}

int adaptiveMissesToDecreaseForPosition(uint8_t posIdx) {
  if (adaptUsePerPosition && posIdx < NUM_POSITIONS) return adaptPos[posIdx].missesToDecrease;
  return adapt.missesToDecrease;
}

float adaptiveStepForPosition(uint8_t posIdx) {
  if (adaptUsePerPosition && posIdx < NUM_POSITIONS) return adaptPos[posIdx].stepMm;
  return adapt.stepMm;
}

float adaptiveDecreaseStepForPosition(uint8_t posIdx) {
  if (adaptUsePerPosition && posIdx < NUM_POSITIONS) return adaptPos[posIdx].decreaseStepMm;
  return adapt.decreaseStepMm;
}

void emitPositionCode(uint8_t idx) {
  if (PIN_TTL_POS0 != PIN_UNUSED) {
    // Parallel bus available (GB219): 3 latched bits plus a strobe.
    digitalWrite(PIN_TTL_POS0, (idx & 0x01) ? HIGH : LOW);
    digitalWrite(PIN_TTL_POS1, (idx & 0x02) ? HIGH : LOW);
    digitalWrite(PIN_TTL_POS2, (idx & 0x04) ? HIGH : LOW);
    pulsePin(PIN_TTL_POS_STB, POSITION_STROBE_MS);
    return;
  }

  // No parallel bus (2pRAM): send (idx+1) pulses on the strobe line.
  // Decode offline by counting pulses in the burst. Nothing is latched, so no
  // co-tenant actuator net is ever held asserted.
  pulseEndPosStrobe = 0;                    // keep the async pulse timer out of this
  digitalWrite(PIN_TTL_POS_STB, LOW);

  const uint32_t slotMs = POSITION_CODE_PULSE_MS + POSITION_CODE_GAP_MS;
  for (uint8_t i = 0; i <= idx; i++) {
    digitalWrite(PIN_TTL_POS_STB, HIGH);
    serviceWait(POSITION_CODE_PULSE_MS);
    digitalWrite(PIN_TTL_POS_STB, LOW);
    serviceWait(POSITION_CODE_GAP_MS);
  }

  // Pad to a fixed frame so every position costs the same pre-settle time.
  uint32_t usedMs  = (uint32_t)(idx + 1) * slotMs;
  uint32_t frameMs = (uint32_t)NUM_POSITIONS * slotMs;
  if (frameMs > usedMs) serviceWait(frameMs - usedMs);
}

// ============================================================
// Session / trial logic
// ============================================================
void resetSessionStats() {
  pendingRewardTrigger = REWARD_TRIGGER_NONE;
  clearAllRewardHolds(false, false);
  totalTrials = 0;
  totalHits = 0;
  totalMisses = 0;
  totalFreeRewards = 0;
  totalAutoRewards = 0;
  totalRewards = 0;
  currentBlockNumber = 0;
  enlViolationCount = 0;
  sessionWaterDeliveredUL = 0.0f;
  syncPulseCount = 0;
  consecutiveMisses = 0;
  currentBlockPos = -1;
  trialsInCurrentBlock = 0;
  currentBlockSize = 0;
  cycleNumber = 0;
  cycleQueueLen = 0;
  cycleQueueIndex = 0;
  sessionStartMs = 0;
  stopPending = false;
  stopPendingReason = STOP_PENDING_NONE;
  currentTrialPos = -1;
  freeRewardThisTrial = false;
  freeRewardDeliveredThisTrial = false;
  successfulLickThisTrial = false;
  rewardIssuedThisTrial = false;
  freeRewardAtMs = 0;
  clearLatchedLick();

  for (int i = 0; i < NUM_POSITIONS; i++) {
    trialsPerPosition[i] = 0;
    hitsPerPosition[i] = 0;
    missesPerPosition[i] = 0;
    freeRewardsPerPosition[i] = 0;
    adaptiveHitCounterPerPosition[i] = 0;
    adaptiveMissCounterPerPosition[i] = 0;
  }
}

void maybeAdvanceDifficulty(uint8_t posIdx) {
  if (!adaptiveEnabledForPosition(posIdx)) return;
  if (adaptiveHitCounterPerPosition[posIdx] < (uint32_t)adaptiveHitsToAdvanceForPosition(posIdx)) return;

  float nextDist = currentDistanceMm[posIdx] + adaptiveStepForPosition(posIdx);
  float maxDist = adaptiveMaxDistanceForPosition(posIdx);
  if (nextDist > maxDist) nextDist = maxDist;
  adaptiveHitCounterPerPosition[posIdx] = 0;
  if (nextDist <= currentDistanceMm[posIdx] + 1e-6f) return;

  currentDistanceMm[posIdx] = nextDist;
  recomputePosition(posIdx);
  emitEventDetail("adapt_advance", String("pos=") + posIdx + " dist_mm=" + String(currentDistanceMm[posIdx], 3));
}

void maybeDecreaseDifficulty(uint8_t posIdx) {
  if (!adaptiveEnabledForPosition(posIdx)) return;
  if (adaptiveMissCounterPerPosition[posIdx] < (uint32_t)adaptiveMissesToDecreaseForPosition(posIdx)) return;

  float nextDist = currentDistanceMm[posIdx] - adaptiveDecreaseStepForPosition(posIdx);
  float minDist = adaptiveMinDistanceForPosition(posIdx);
  if (nextDist < minDist) nextDist = minDist;
  adaptiveMissCounterPerPosition[posIdx] = 0;
  if (nextDist >= currentDistanceMm[posIdx] - 1e-6f) return;

  currentDistanceMm[posIdx] = nextDist;
  recomputePosition(posIdx);
  emitEventDetail("adapt_decrease", String("pos=") + posIdx + " dist_mm=" + String(currentDistanceMm[posIdx], 3));
}

void registerHit(uint8_t posIdx) {
  totalHits++;
  hitsPerPosition[posIdx]++;
  consecutiveMisses = 0;
  if (adaptiveEnabledForPosition(posIdx)) {
    adaptiveMissCounterPerPosition[posIdx] = 0;
    adaptiveHitCounterPerPosition[posIdx]++;
  } else {
    adaptiveMissCounterPerPosition[posIdx] = 0;
    adaptiveHitCounterPerPosition[posIdx] = 0;
  }
  freeRewardThisTrial = false;
  emitEvent("hit");
  maybeAdvanceDifficulty(posIdx);
}

void registerMiss(uint8_t posIdx) {
  totalMisses++;
  missesPerPosition[posIdx]++;
  consecutiveMisses++;
  if (adaptiveEnabledForPosition(posIdx)) {
    adaptiveHitCounterPerPosition[posIdx] = 0;
    adaptiveMissCounterPerPosition[posIdx]++;
  } else {
    adaptiveHitCounterPerPosition[posIdx] = 0;
    adaptiveMissCounterPerPosition[posIdx] = 0;
  }
  freeRewardThisTrial = false;
  emitEvent("miss");
  updateAutoRewardHoldFromMissStreak(true);
  maybeDecreaseDifficulty(posIdx);
}

bool shouldHoldTaskReward(RewardTriggerType trigger) {
  return false;
}

bool deliverRewardForTrigger(RewardTriggerType trigger) {
  pendingRewardTrigger = trigger;
  bool bypassAutoHoldForManual = (trigger == REWARD_TRIGGER_MANUAL && autoRewardsHeld && !manualRewardsHeld);
  if (taskRewardsHeld && !bypassAutoHoldForManual) {
    if (trigger == REWARD_TRIGGER_FREE) {
      freeRewardDeliveredThisTrial = true;
    }
    pendingRewardTrigger = REWARD_TRIGGER_NONE;
    return true;
  }
  if (trigger == REWARD_TRIGGER_FREE && currentTrialPos >= 0) {
    totalFreeRewards++;
    totalAutoRewards++;
    freeRewardsPerPosition[currentTrialPos]++;
    freeRewardDeliveredThisTrial = true;
    consecutiveMisses = 0;
    adaptiveHitCounterPerPosition[currentTrialPos] = 0;
    adaptiveMissCounterPerPosition[currentTrialPos] = 0;
    emitEvent("free_reward");
  } else if (trigger == REWARD_TRIGGER_AUTO) {
    totalAutoRewards++;
  }

  openRewardValve(cfg.rewardOpenMs, bypassAutoHoldForManual);
  pulsePin(selectedRewardIndicatorPin(), TTL_PULSE_MS);
  totalRewards++;
  sessionWaterDeliveredUL += cfg.estimatedRewardUL;
  emitEvent(trigger == REWARD_TRIGGER_MANUAL ? "manual_reward" : "reward");
  pendingRewardTrigger = REWARD_TRIGGER_NONE;

  if (cfg.sessionRunning && sessionWaterDeliveredUL >= cfg.sessionWaterLimitUL) {
    cfg.sessionRunning = false;
    emitEvent("water_limit_reached");
  }
  return true;
}

void chooseNextBlockPosition() {

  normalizeSchedulerConfig();
  int newPos = -1;
  if (cfg.scheduleMode == SCHEDULE_BALANCED_BLOCK_CYCLES) {
    if (cycleQueueIndex >= cycleQueueLen) buildBalancedCycle();
    if (cycleQueueIndex >= cycleQueueLen) {
      currentBlockPos = -1;
      currentBlockSize = 0;
      return;
    }
    newPos = cycleQueue[cycleQueueIndex++];
  } else {
    int enabled[NUM_POSITIONS];
    int n = 0;
    for (int i = 0; i < NUM_POSITIONS; i++) {
      if (positionNeedsTrials((uint8_t)i)) enabled[n++] = i;
    }
    if (n == 0) {
      currentBlockPos = -1;
      currentBlockSize = 0;
      return;
    }
    newPos = currentBlockPos;
    if (n == 1) {
      newPos = enabled[0];
    } else {
      while (newPos == currentBlockPos) {
        newPos = enabled[random(0, n)];
      }
    }
  }

  currentBlockPos = newPos;
  trialsInCurrentBlock = 0;
  currentBlockSize = sampleBlockSizeForPosition((uint8_t)newPos);
  if (currentBlockSize < 1) {
    currentBlockPos = -1;
    return;
  }
  currentBlockNumber++;
}

void startNextTrial() {
  normalizeSchedulerConfig();
  updateSessionStopChecks();
  bool needNewBlock = (currentBlockPos < 0 || trialsInCurrentBlock >= currentBlockSize);
  if (needNewBlock) {
    if (shouldStopBeforeStartingNextBlock()) {
      cfg.sessionRunning = false;
      runState = ST_IDLE;
      emitEvent("session_complete");
      return;
    }
    chooseNextBlockPosition();
  }

  if (currentBlockPos < 0) {
    if (stopPending || allTargetTrialsReached()) {
      cfg.sessionRunning = false;
      runState = ST_IDLE;
      emitEvent("session_complete");
      return;
    }
    cfg.sessionRunning = false;
    runState = ST_IDLE;
    emitErr("start_trial", "no_enabled_positions");
    return;
  }

  currentTrialPos = currentBlockPos;
  freeRewardThisTrial = false;
  freeRewardDeliveredThisTrial = false;
  successfulLickThisTrial = false;
  rewardIssuedThisTrial = false;
  freeRewardAtMs = 0;
  autoRewardAtMs = 0;

  if (freeRewardCfg.enabled &&
      freeRewardCfg.afterConsecutiveMisses > 0 &&
      consecutiveMisses >= freeRewardCfg.afterConsecutiveMisses) {
    freeRewardThisTrial = true;
    emitEvent("free_reward_trial");
  }

  if (TRIAL_TTL_IS_GATE) {
    digitalWrite(PIN_TTL_TRIAL_STOP, HIGH);   // trial gate opens: move begins
  } else {
    pulsePin(PIN_TTL_TRIAL, TTL_PULSE_MS);
  }
  emitEvent("trial_start");

  bool ok = moveToPositionSafe(positions[currentTrialPos]);
  if (!ok) {
    // Aborted or failed move: close the gate here, since this path never
    // reaches ST_RETURN_TO_DOCK and would otherwise strand it HIGH forever.
    if (TRIAL_TTL_IS_GATE) digitalWrite(PIN_TTL_TRIAL_STOP, LOW);
    cfg.sessionRunning = false;
    runState = ST_IDLE;
    emitErr("move", "failed");
    return;
  }

  emitPositionCode((uint8_t)currentTrialPos);
  emitEventDetail("position", String("idx=") + currentTrialPos);

  totalTrials++;
  trialsPerPosition[currentTrialPos]++;
  trialsInCurrentBlock++;
  updateSessionStopChecks();

  stateStartMs = millis();
  runState = ST_SETTLE;
}

uint32_t sampleITI() {
  return cfg.itiMinMs + random(cfg.itiJitterMs + 1);
}

void setCurrentPosition(float x, float y, float z) {
  axisX.posMM = x;
  axisY.posMM = y;
  axisZ.posMM = z;
  currentTrialPos = -1;
}

// ============================================================
// Cue / reward
// ============================================================
void openRewardValve(uint32_t durMs, bool bypassHold) {
  if (!taskRewardsHeld || bypassHold) {
    digitalWrite(selectedRewardSolenoidPin(), HIGH);
  }
  uint32_t t0 = millis();
  while (millis() - t0 < durMs) {
    handleSerialDuringBlocking();
    serviceCore();
  }
  digitalWrite(selectedRewardSolenoidPin(), LOW);
}

void playCue() {
  uint32_t hz = cueCfg.frequencyHz;
  if (hz < 1) hz = 1;
  uint8_t duty = (uint8_t)constrain((int)roundf(255.0f * ((float)cueCfg.volumePct / 100.0f)), 0, 255);
  analogWriteFrequency(PIN_SPEAKER, (float)hz);
  if (duty == 0) {
    analogWrite(PIN_SPEAKER, 0);
    digitalWrite(PIN_CUE_TTL, LOW);    // silent cue: no tone, no gate
    cueActive = false;
    cueOffAtMs = 0;
    return;
  }
  analogWrite(PIN_SPEAKER, duty);
  // Cue TTL is a GATE, not an 8 ms pulse: it opens and closes with the tone
  // itself, so the DAQ records cue onset, offset and duration on one channel
  // and cannot drift out of step with cue.duration_ms. Driven here and in
  // updateCue() rather than at the call sites so manual cues behave identically.
  digitalWrite(PIN_CUE_TTL, HIGH);
  cueOffAtMs = millis() + cueCfg.durationMs;
  cueActive = true;
}

// Force every level-held output back to its inactive state. Called on STOP so
// a trial gate or a cue gate can never be stranded HIGH by an aborted trial.
void closeTrialAndCueGates() {
  if (TRIAL_TTL_IS_GATE) digitalWrite(PIN_TTL_TRIAL_STOP, LOW);
  analogWrite(PIN_SPEAKER, 0);
  digitalWrite(PIN_CUE_TTL, LOW);
  cueActive = false;
  cueOffAtMs = 0;
}

void cueOnly() {
  playCue();   // gate is driven inside playCue()/updateCue()
  emitEvent("cue_only");
}

void updateCue() {
  if (cueActive && millis() >= cueOffAtMs) {
    analogWrite(PIN_SPEAKER, 0);
    digitalWrite(PIN_CUE_TTL, LOW);    // gate closes with the tone
    cueActive = false;
    cueOffAtMs = 0;
  }
}

// ============================================================
// Lick detection
// ============================================================
void updateLick() {
  bool rawPressed = (digitalRead(selectedLickInputPin()) == LOW);
  bool currentState = lickCfg.activeLow ? rawPressed : !rawPressed;
  lickRawDigital = currentState ? 1 : 0;

  uint32_t now = millis();

  if (currentState != lickCurrent) {
    if (now - lastLickChangeMs >= lickCfg.debounceMs) {
      bool prevState = lickCurrent;
      lickCurrent = currentState;
      lastLickChangeMs = now;
      if (lickCurrent && !prevState) {
        emitEvent("lick_on");
      } else if (!lickCurrent && prevState) {
        emitEvent("lick_off");
      }
      if (lickCurrent && autoRewardsHeld) {
        autoRewardsHeld = false;
        refreshRewardHoldState(false);
      }
      if (lickCurrent && lickSensingEnabled && (now - lastLickOnsetMs >= lickCfg.refractoryMs)) {
        lickOnsetLatched = true;
        lastLickOnsetMs = now;
      }
    }
  }

  if (lickCfg.debug) {
    static uint32_t lastPrint = 0;
    if (millis() - lastPrint >= 50) {
      lastPrint = millis();
      Serial.print("STAT kind=lick raw=");
      Serial.print(lickRawDigital);
      Serial.print(" mode=digital baseline=0 threshold=0.5 lick=");
      Serial.println(lickCurrent ? 1 : 0);
    }
  }
}

bool consumeLickOnset() {
  if (lickOnsetLatched) {
    lickOnsetLatched = false;
    return true;
  }
  return false;
}

// ============================================================
// TTL / sync
// ============================================================
void pulsePin(uint8_t pin, uint32_t durMs) {
  if (pin == PIN_UNUSED) return;   // signal retired on this rig
  digitalWrite(pin, HIGH);
  uint32_t tEnd = millis() + durMs;

  if (pin == PIN_TTL_TRIAL) pulseEndTrial = tEnd;
  else if (pin == PIN_CUE_TTL) pulseEndCue = tEnd;
  else if (pin == PIN_REWARD_LEFT_INDICATOR) pulseEndReward = tEnd;
  else if (pin == PIN_SYNC_OUT) pulseEndSync = tEnd;
  else if (pin == PIN_TTL_TRIAL_STOP) pulseEndTrialStop = tEnd;
  else if (pin == PIN_TTL_POS_STB) pulseEndPosStrobe = tEnd;
}

void updateTTLPulses() {
  uint32_t now = millis();

  if (pulseEndTrial && now >= pulseEndTrial) {
      digitalWrite(PIN_TTL_TRIAL, LOW);
      pulseEndTrial = 0;
  }
  if (pulseEndCue && now >= pulseEndCue) {
      digitalWrite(PIN_CUE_TTL, LOW);
      pulseEndCue = 0;
  }
  if (pulseEndReward && now >= pulseEndReward) {
    digitalWrite(PIN_REWARD_LEFT_INDICATOR, LOW);
      pulseEndReward = 0;
  }
  if (pulseEndSync && now >= pulseEndSync) {
    digitalWrite(PIN_SYNC_OUT, LOW);
    pulseEndSync = 0;
  }
  if (pulseEndTrialStop && now >= pulseEndTrialStop) {
      digitalWrite(PIN_TTL_TRIAL_STOP, LOW);
      pulseEndTrialStop = 0;
  }
  if (pulseEndPosStrobe && now >= pulseEndPosStrobe) {
    digitalWrite(PIN_TTL_POS_STB, LOW);
    pulseEndPosStrobe = 0;
  }
}

void updateSync() {
  if (!cfg.sessionRunning) return;
  uint32_t now = millis();
  if (now >= nextSyncAt) {
    uint32_t pulseMs = syncPulseMinMs;
    if (syncPulseMaxMs > syncPulseMinMs) pulseMs = random(syncPulseMinMs, syncPulseMaxMs + 1);
    pulsePin(PIN_SYNC_OUT, pulseMs);
    syncPulseCount++;

    uint32_t nextInterval = random(syncMinIntervalMs, syncMaxIntervalMs + 1);
    nextSyncAt = now + nextInterval;

    Serial.print("EVT name=sync");
    Serial.print(" t_ms=");
    Serial.print(now);
    Serial.print(" count=");
    Serial.print(syncPulseCount);
    Serial.print(" ttl_ms=");
    Serial.print(pulseMs);
    Serial.print(" next_interval_ms=");
    Serial.println(nextInterval);
  }
}

// ============================================================
// Protocol output
// ============================================================
const char* stateName(RunState s) {
  switch (s) {
    case ST_IDLE: return "idle";
    case ST_MOVE_TO_TARGET: return "move_to_target";
    case ST_SETTLE: return "settle";
    case ST_PRE_CUE: return "pre_cue";
    case ST_WAIT_FOR_LICK: return "wait_for_lick";
    case ST_DELIVER_REWARD: return "deliver_reward";
    case ST_POST_REWARD_HOLD: return "post_reward_hold";
    case ST_RETURN_TO_DOCK: return "return_to_dock";
    case ST_ITI: return "iti";
    default: return "unknown";
  }
}

void emitInfoReady() {
  Serial.print("INFO kind=ready protocol=2 backend=smc02 use_home_switches=");
  Serial.println(USE_HOME_SWITCHES ? 1 : 0);
}

void emitOK(const String& cmd, const String& extra) {
  Serial.print("OK cmd=");
  Serial.print(cmd);
  if (extra.length() > 0) {
    Serial.print(" ");
    Serial.print(extra);
  }
  Serial.println();
}

void emitErr(const String& cmd, const String& code, const String& detail) {
  Serial.print("ERR cmd=");
  Serial.print(cmd);
  Serial.print(" code=");
  Serial.print(code);
  if (detail.length() > 0) {
    Serial.print(" detail=");
    Serial.print(detail);
  }
  Serial.println();
}


const char* rewardTriggerName(RewardTriggerType trigger) {
  switch (trigger) {
    case REWARD_TRIGGER_CONTINGENT: return "contingent";
    case REWARD_TRIGGER_AUTO: return "auto";
    case REWARD_TRIGGER_FREE: return "free";
    case REWARD_TRIGGER_MANUAL: return "manual";
    case REWARD_TRIGGER_CALIBRATION: return "calibration";
    default: return "";
  }
}

const char* eventSourceName(const char* name) {
  if (strcmp(name, "cue_only") == 0 || strcmp(name, "manual_reward") == 0 || strcmp(name, "manual_reference_set") == 0) return "manual";
  if (strcmp(name, "reward_cal_pulse") == 0) return "calibration";
  if (strcmp(name, "sync") == 0) return "sync";
  if (strncmp(name, "button_", 7) == 0) return "button";
  return "task";
}

const char* eventRewardType(const char* name) {
  if (strcmp(name, "reward") == 0) return rewardTriggerName(pendingRewardTrigger);
  if (strcmp(name, "hit") == 0) return "contingent";
  if (strcmp(name, "free_reward") == 0 || strcmp(name, "free_reward_trial") == 0) return "free";
  if (strcmp(name, "manual_reward") == 0) return "manual";
  if (strcmp(name, "reward_cal_pulse") == 0) return "calibration";
  return "";
}

void emitEvent(const char* name) {
  Serial.print("EVT name=");
  Serial.print(name);
  Serial.print(" t_ms=");
  Serial.print(millis());
  Serial.print(" state=");
  Serial.print(stateName(runState));
  Serial.print(" trial=");
  Serial.print(totalTrials);
  Serial.print(" pos=");
  Serial.print(currentTrialPos);
  if (currentTrialPos >= 0) {
    Serial.print(" pos_dist_mm=");
    Serial.print(currentDistanceMm[currentTrialPos], 3);
  }
  Serial.print(" event_source=");
  Serial.print(eventSourceName(name));
  const char* rewardType = eventRewardType(name);
  if (rewardType[0] != 0) {
    Serial.print(" reward_type=");
    Serial.print(rewardType);
  }
  Serial.print(" free_reward_trial=");
  Serial.print(freeRewardThisTrial ? 1 : 0);
  Serial.print(" free_reward_delivered=");
  Serial.println(freeRewardDeliveredThisTrial ? 1 : 0);
}

void emitEventDetail(const char* name, const String& detail) {
  Serial.print("EVT name=");
  Serial.print(name);
  Serial.print(" t_ms=");
  Serial.print(millis());
  if (detail.length() > 0) {
    Serial.print(" ");
    Serial.print(detail);
  }
  Serial.println();
}

void emitStatus() {
  uint32_t now = millis();
  uint32_t cueWaitRemaining = 0;
  uint32_t responseRemaining = 0;

  if (runState == ST_PRE_CUE && cueEligibleAtMs > now) {
    cueWaitRemaining = cueEligibleAtMs - now;
  }
  if (runState == ST_WAIT_FOR_LICK && responseDeadlineMs > now) {
    responseRemaining = responseDeadlineMs - now;
  }

  int enabledCount = 0;
  for (int i = 0; i < NUM_POSITIONS; i++) {
    if (cfg.enabledPositions[i]) enabledCount++;
  }

  Serial.print("STAT kind=status");
  Serial.print(" run=");
  Serial.print(cfg.sessionRunning ? 1 : 0);
  Serial.print(" state=");
  Serial.print(stateName(runState));
  Serial.print(" x_mm=");
  Serial.print(axisX.posMM, 3);
  Serial.print(" y_mm=");
  Serial.print(axisY.posMM, 3);
  Serial.print(" z_mm=");
  Serial.print(axisZ.posMM, 3);
  Serial.print(" current_pos=");
  Serial.print(currentTrialPos);
  Serial.print(" current_pos_dist_mm=");
  if (currentTrialPos >= 0) {
    Serial.print(currentDistanceMm[currentTrialPos], 3);
  } else {
    Serial.print("-1");
  }
  Serial.print(" current_pos_trials=");
  if (currentTrialPos >= 0) {
    Serial.print(trialsPerPosition[currentTrialPos]);
  } else {
    Serial.print("-1");
  }
  Serial.print(" current_pos_target_remaining=");
  if (currentTrialPos >= 0 && cfg.targetTrialsPerPositionEnabled) {
    uint32_t remainingForPos = 0;
    if (trialsPerPosition[currentTrialPos] < cfg.targetTrialsPerPosition) remainingForPos = cfg.targetTrialsPerPosition - trialsPerPosition[currentTrialPos];
    Serial.print(remainingForPos);
  } else {
    Serial.print("-1");
  }
  Serial.print(" block_pos=");
  Serial.print(currentBlockPos);
  Serial.print(" block_trial=");
  Serial.print(trialsInCurrentBlock);
  Serial.print(" block_number=");
  Serial.print(currentBlockNumber);
  Serial.print(" current_block_size=");
  Serial.print((int)currentBlockSize);
  Serial.print(" block_size=");
  Serial.print((int)currentBlockSize);
  Serial.print(" block_size_min=");
  Serial.print((int)cfg.blockSizeMin);
  Serial.print(" block_size_max=");
  Serial.print((int)cfg.blockSizeMax);
  Serial.print(" scheduling_mode=");
  Serial.print(scheduleModeName());
  Serial.print(" stop_mode=");
  Serial.print(stopModeName());
  Serial.print(" target_trials_per_position_enabled=");
  Serial.print(cfg.targetTrialsPerPositionEnabled ? 1 : 0);
  Serial.print(" target_trials_per_position=");
  Serial.print(cfg.targetTrialsPerPosition);
  Serial.print(" max_duration_enabled=");
  Serial.print(cfg.maxDurationEnabled ? 1 : 0);
  Serial.print(" max_duration_min=");
  Serial.print(cfg.maxDurationMin);
  Serial.print(" stop_pending=");
  Serial.print(stopPending ? 1 : 0);
  Serial.print(" total_trials=");
  Serial.print(totalTrials);
  Serial.print(" hits=");
  Serial.print(totalHits);
  Serial.print(" misses=");
  Serial.print(totalMisses);
  Serial.print(" free_rewards=");
  Serial.print(totalFreeRewards);
  Serial.print(" auto_rewards=");
  Serial.print(totalAutoRewards);
  Serial.print(" total_rewards=");
  Serial.print(totalRewards);
  Serial.print(" reward_mode=");
  Serial.print(rewardModeName(cfg.rewardMode));
  Serial.print(" auto_reward_delay_ms=");
  Serial.print(cfg.autoRewardDelayMs);
  Serial.print(" cue_hz=");
  Serial.print(cueCfg.frequencyHz);
  Serial.print(" cue_duration_ms=");
  Serial.print(cueCfg.durationMs);
  Serial.print(" cue_volume_pct=");
  Serial.print(cueCfg.volumePct);
  Serial.print(" water_ul=");
  Serial.print(sessionWaterDeliveredUL, 2);
  Serial.print(" water_limit_ul=");
  Serial.print(cfg.sessionWaterLimitUL, 2);
  Serial.print(" reward_ul=");
  Serial.print(cfg.estimatedRewardUL, 2);
  Serial.print(" rewards_held=");
  Serial.print(taskRewardsHeld ? 1 : 0);
  Serial.print(" auto_hold_after_miss_enabled=");
  Serial.print(cfg.autoHoldAfterMissEnabled ? 1 : 0);
  Serial.print(" auto_hold_after_miss_threshold=");
  Serial.print(cfg.autoHoldAfterMissThreshold);
  Serial.print(" manual_reward_hold_active=");
  Serial.print(manualRewardsHeld ? 1 : 0);
  Serial.print(" auto_reward_hold_active=");
  Serial.print(autoRewardsHeld ? 1 : 0);
  Serial.print(" miss_streak=");
  Serial.print(consecutiveMisses);
  Serial.print(" enl_violations=");
  Serial.print(enlViolationCount);
  Serial.print(" enabled_positions=");
  Serial.print(enabledCount);
  Serial.print(" sync_count=");
  Serial.print(syncPulseCount);
  Serial.print(" sync_state=");
  Serial.print((pulseEndSync && now < pulseEndSync) ? 1 : 0);
  Serial.print(" free_reward_trial=");
  Serial.print(freeRewardThisTrial ? 1 : 0);
  Serial.print(" free_reward_delivered=");
  Serial.print(freeRewardDeliveredThisTrial ? 1 : 0);
  Serial.print(" cue_wait_remaining_ms=");
  Serial.print(cueWaitRemaining);
  Serial.print(" response_remaining_ms=");
  Serial.print(responseRemaining);
  Serial.print(" lick=");
  Serial.print(lickCurrent ? 1 : 0);
  Serial.print(" lick_raw=");
  Serial.println(lickRawDigital);
}

void emitPositions() {
  for (int i = 0; i < NUM_POSITIONS; i++) {
    Serial.print("POS idx=");
    Serial.print(i);
    Serial.print(" x_mm=");
    Serial.print(positions[i].x, 3);
    Serial.print(" y_mm=");
    Serial.print(positions[i].y, 3);
    Serial.print(" z_mm=");
    Serial.print(positions[i].z, 3);
    Serial.print(" enabled=");
    Serial.print(cfg.enabledPositions[i] ? 1 : 0);
    Serial.print(" dist_mm=");
    Serial.print(currentDistanceMm[i], 3);
    Serial.print(" az_deg=");
    Serial.print(geom.azimuthDeg[positionAzIndex[i]], 3);
    Serial.print(" down_deg=");
    Serial.println(geom.downwardAngleDeg, 3);
  }
}

void emitStats() {
  Serial.print("STAT kind=summary");
  Serial.print(" total_trials=");
  Serial.print(totalTrials);
  Serial.print(" hits=");
  Serial.print(totalHits);
  Serial.print(" misses=");
  Serial.print(totalMisses);
  Serial.print(" free_rewards=");
  Serial.print(totalFreeRewards);
  Serial.print(" auto_rewards=");
  Serial.print(totalAutoRewards);
  Serial.print(" total_rewards=");
  Serial.print(totalRewards);
  Serial.print(" reward_mode=");
  Serial.print(rewardModeName(cfg.rewardMode));
  Serial.print(" auto_reward_delay_ms=");
  Serial.print(cfg.autoRewardDelayMs);
  Serial.print(" water_ul=");
  Serial.print(sessionWaterDeliveredUL, 2);
  Serial.print(" rewards_held=");
  Serial.print(taskRewardsHeld ? 1 : 0);
  Serial.print(" auto_hold_after_miss_enabled=");
  Serial.print(cfg.autoHoldAfterMissEnabled ? 1 : 0);
  Serial.print(" auto_hold_after_miss_threshold=");
  Serial.print(cfg.autoHoldAfterMissThreshold);
  Serial.print(" manual_reward_hold_active=");
  Serial.print(manualRewardsHeld ? 1 : 0);
  Serial.print(" auto_reward_hold_active=");
  Serial.print(autoRewardsHeld ? 1 : 0);
  Serial.print(" miss_streak=");
  Serial.print(consecutiveMisses);
  Serial.print(" enl_violations=");
  Serial.print(enlViolationCount);
  Serial.print(" sync_count=");
  Serial.println(syncPulseCount);

  for (int i = 0; i < NUM_POSITIONS; i++) {
    Serial.print("STAT kind=pos");
    Serial.print(" idx=");
    Serial.print(i);
    Serial.print(" enabled=");
    Serial.print(cfg.enabledPositions[i] ? 1 : 0);
    Serial.print(" tier=");
    Serial.print(positionTierIndex[i]);
    Serial.print(" az_index=");
    Serial.print(positionAzIndex[i]);
    Serial.print(" dist_mm=");
    Serial.print(currentDistanceMm[i], 3);
    Serial.print(" trials=");
    Serial.print(trialsPerPosition[i]);
    Serial.print(" hits=");
    Serial.print(hitsPerPosition[i]);
    Serial.print(" misses=");
    Serial.print(missesPerPosition[i]);
    Serial.print(" free_rewards=");
    Serial.print(freeRewardsPerPosition[i]);
    Serial.print(" adaptive_hit_counter=");
    Serial.println(adaptiveHitCounterPerPosition[i]);
  }
}

void emitConfigKV(const String& key, const String& value) {
  Serial.print("CFG key=");
  Serial.print(key);
  Serial.print(" value=");
  Serial.println(value);
}

void emitConfigKV(const String& key, int value) {
  Serial.print("CFG key=");
  Serial.print(key);
  Serial.print(" value=");
  Serial.println(value);
}

void emitConfigKV(const String& key, uint32_t value) {
  Serial.print("CFG key=");
  Serial.print(key);
  Serial.print(" value=");
  Serial.println(value);
}

void emitConfigKV(const String& key, float value, int digits) {
  Serial.print("CFG key=");
  Serial.print(key);
  Serial.print(" value=");
  Serial.println(value, digits);
}

void emitConfigKV(const String& key, bool value) {
  Serial.print("CFG key=");
  Serial.print(key);
  Serial.print(" value=");
  Serial.println(value ? 1 : 0);
}

const char* scheduleModeName() {
  switch (cfg.scheduleMode) {
    case SCHEDULE_BALANCED_BLOCK_CYCLES: return "balanced_block_cycles";
    case SCHEDULE_RANDOM_BLOCKS: return "random_blocks";
    default: return "balanced_block_cycles";
  }
}

const char* stopModeName() {
  switch (cfg.stopMode) {
    case STOP_END_OF_CURRENT_BLOCK: return "end_of_current_block";
    case STOP_END_OF_BALANCED_CYCLE: return "end_of_balanced_cycle";
    default: return "end_of_current_block";
  }
}

void normalizeSchedulerConfig() {
  if (cfg.blockSizeMin < 1) cfg.blockSizeMin = 1;
  if (cfg.blockSizeMax < cfg.blockSizeMin) cfg.blockSizeMax = cfg.blockSizeMin;
  cfg.blockSize = cfg.blockSizeMin;
  if (cfg.targetTrialsPerPosition < 1) cfg.targetTrialsPerPosition = 1;
  if (cfg.maxDurationMin < 1) cfg.maxDurationMin = 1;
  if (syncPulseMinMs < 1) syncPulseMinMs = 1;
  if (syncPulseMaxMs < syncPulseMinMs) syncPulseMaxMs = syncPulseMinMs;
  if (syncMinIntervalMs < 1) syncMinIntervalMs = 1;
  if (syncMaxIntervalMs < syncMinIntervalMs) syncMaxIntervalMs = syncMinIntervalMs;
}

bool positionNeedsTrials(uint8_t posIdx) {
  if (posIdx >= NUM_POSITIONS || !cfg.enabledPositions[posIdx]) return false;
  if (cfg.targetTrialsPerPositionEnabled && trialsPerPosition[posIdx] >= cfg.targetTrialsPerPosition) return false;
  return true;
}

bool allTargetTrialsReached() {
  if (!cfg.targetTrialsPerPositionEnabled) return false;
  bool anyEnabled = false;
  for (int i = 0; i < NUM_POSITIONS; i++) {
    if (!cfg.enabledPositions[i]) continue;
    anyEnabled = true;
    if (trialsPerPosition[i] < cfg.targetTrialsPerPosition) return false;
  }
  return anyEnabled;
}

void buildBalancedCycle() {
  cycleQueueLen = 0;
  cycleQueueIndex = 0;
  for (int i = 0; i < NUM_POSITIONS; i++) if (positionNeedsTrials((uint8_t)i)) cycleQueue[cycleQueueLen++] = i;
  for (int i = cycleQueueLen - 1; i > 0; --i) {
    int j = random(i + 1);
    int tmp = cycleQueue[i];
    cycleQueue[i] = cycleQueue[j];
    cycleQueue[j] = tmp;
  }
  if (cycleQueueLen > 0) cycleNumber++;
}

uint8_t sampleBlockSizeForPosition(uint8_t posIdx) {
  normalizeSchedulerConfig();
  uint8_t sampled = cfg.blockSizeMin;
  if (cfg.blockSizeMax > cfg.blockSizeMin) sampled = (uint8_t)random((long)cfg.blockSizeMin, (long)cfg.blockSizeMax + 1L);
  if (cfg.targetTrialsPerPositionEnabled) {
    uint32_t remaining = 0;
    if (trialsPerPosition[posIdx] < cfg.targetTrialsPerPosition) remaining = cfg.targetTrialsPerPosition - trialsPerPosition[posIdx];
    if (remaining == 0) return 0;
    if (sampled > remaining) sampled = (uint8_t)remaining;
    if (sampled < 1) sampled = 1;
  }
  return sampled;
}

void requestStopPending(StopPendingReason reason) {
  if (stopPending) return;
  stopPending = true;
  stopPendingReason = reason;
  if (reason == STOP_PENDING_MAX_DURATION) emitEvent("max_duration_reached");
  else if (reason == STOP_PENDING_TARGET_REACHED) emitEvent("target_trials_reached");
}

void updateSessionStopChecks() {
  if (!cfg.sessionRunning) return;
  if (cfg.maxDurationEnabled && sessionStartMs > 0) {
    uint32_t elapsed = millis() - sessionStartMs;
    uint32_t maxMs = cfg.maxDurationMin * 60000UL;
    if (elapsed >= maxMs) requestStopPending(STOP_PENDING_MAX_DURATION);
  }
  if (cfg.targetTrialsPerPositionEnabled && allTargetTrialsReached()) requestStopPending(STOP_PENDING_TARGET_REACHED);
}

bool shouldStopBeforeStartingNextBlock() {
  if (!stopPending) return false;
  if (cfg.stopMode == STOP_END_OF_CURRENT_BLOCK) return true;
  if (cfg.scheduleMode != SCHEDULE_BALANCED_BLOCK_CYCLES) return true;
  return cycleQueueIndex >= cycleQueueLen;
}

void emitConfig() {
  emitConfigKV("task.reward_ms", cfg.rewardOpenMs);
  emitConfigKV("task.reward_ul", cfg.estimatedRewardUL, 3);
  emitConfigKV("task.water_limit_ul", cfg.sessionWaterLimitUL, 3);
  emitConfigKV("task.reward_mode", String(rewardModeName(cfg.rewardMode)));
  emitConfigKV("task.auto_reward_delay_ms", cfg.autoRewardDelayMs);
  emitConfigKV("task.auto_hold_after_miss_enabled", cfg.autoHoldAfterMissEnabled);
  emitConfigKV("task.auto_hold_after_miss_threshold", cfg.autoHoldAfterMissThreshold);
  emitConfigKV("task.rewards_held", taskRewardsHeld);
  emitConfigKV("task.reward_contingent", cfg.rewardMode == REWARD_MODE_CONTINGENT);
  emitConfigKV("task.enforce_no_lick", cfg.enforceNoLick);
  emitConfigKV("task.manual_reward_allowed", cfg.manualRewardAllowed);
  emitConfigKV("task.settle_ms", cfg.settleMs);
  emitConfigKV("task.post_reward_hold_ms", cfg.postRewardHoldMs);
  emitConfigKV("task.pre_cue_min_ms", cfg.preCueMinMs);
  emitConfigKV("task.pre_cue_max_ms", cfg.preCueMaxMs);
  emitConfigKV("task.response_window_ms", cfg.responseWindowMs);
  emitConfigKV("task.iti_min_ms", cfg.itiMinMs);
  emitConfigKV("task.iti_jitter_ms", cfg.itiJitterMs);
  emitConfigKV("task.block_size", cfg.blockSize);
  emitConfigKV("task.block_size_min", (int)cfg.blockSizeMin);
  emitConfigKV("task.block_size_max", (int)cfg.blockSizeMax);
  emitConfigKV("task.target_trials_per_position_enabled", cfg.targetTrialsPerPositionEnabled);
  emitConfigKV("task.target_trials_per_position", cfg.targetTrialsPerPosition);
  emitConfigKV("task.max_duration_enabled", cfg.maxDurationEnabled);
  emitConfigKV("task.max_duration_min", cfg.maxDurationMin);
  emitConfigKV("task.scheduling_mode", String(scheduleModeName()));
  emitConfigKV("task.stop_mode", String(stopModeName()));
  emitConfigKV("sync.min_pulse_ms", syncPulseMinMs);
  emitConfigKV("sync.max_pulse_ms", syncPulseMaxMs);
  emitConfigKV("sync.min_interval_ms", syncMinIntervalMs);
  emitConfigKV("sync.max_interval_ms", syncMaxIntervalMs);
  emitConfigKV("cue.frequency_hz", cueCfg.frequencyHz);
  emitConfigKV("cue.duration_ms", cueCfg.durationMs);
  emitConfigKV("cue.volume_pct", (int)cueCfg.volumePct);
  for (int i = 0; i < NUM_POSITIONS; i++) {
    emitConfigKV(String("task.enable_pos") + i, cfg.enabledPositions[i]);
  }

  emitConfigKV("geom.dist_close_mm", geom.distanceTierMm[0], 3);
  emitConfigKV("geom.dist_far_mm", geom.distanceTierMm[1], 3);
  emitConfigKV("geom.az_center_deg", geom.azimuthDeg[0], 3);
  emitConfigKV("geom.az_left_deg", geom.azimuthDeg[1], 3);
  emitConfigKV("geom.az_right_deg", geom.azimuthDeg[2], 3);
  emitConfigKV("geom.down_angle_deg", geom.downwardAngleDeg, 3);
  emitConfigKV("geom.head_roll_deg", geom.headRollDeg, 3);

  emitConfigKV("motion.mouth_origin.x_mm", mouthOrigin.x, 3);
  emitConfigKV("motion.mouth_origin.y_mm", mouthOrigin.y, 3);
  emitConfigKV("motion.mouth_origin.z_mm", mouthOrigin.z, 3);
  emitConfigKV("motion.dock.x_mm", dockPosition.x, 3);
  emitConfigKV("motion.dock.y_mm", dockPosition.y, 3);
  emitConfigKV("motion.dock.z_mm", dockPosition.z, 3);
  emitConfigKV("motion.safe_z_mm", safePosition.z, 3);
  emitConfigKV("motion.use_home_switches", USE_HOME_SWITCHES);

  emitConfigKV("adapt.enabled", adapt.enabled);
  emitConfigKV("adapt.use_per_position", adaptUsePerPosition);
  emitConfigKV("adapt.hits_to_advance", adapt.hitsToAdvance);
  emitConfigKV("adapt.misses_to_decrease", adapt.missesToDecrease);
  emitConfigKV("adapt.step_mm", adapt.stepMm, 3);
  emitConfigKV("adapt.decrease_step_mm", adapt.decreaseStepMm, 3);
  emitConfigKV("adapt.min_distance_mm", adapt.minDistanceMm, 3);
  emitConfigKV("adapt.max_distance_mm", adapt.maxDistanceMm, 3);
  for (int i = 0; i < NUM_POSITIONS; i++) {
    emitConfigKV(String("adapt.pos") + i + ".enabled", adaptPos[i].enabled);
    emitConfigKV(String("adapt.pos") + i + ".hits_to_advance", adaptPos[i].hitsToAdvance);
    emitConfigKV(String("adapt.pos") + i + ".misses_to_decrease", adaptPos[i].missesToDecrease);
    emitConfigKV(String("adapt.pos") + i + ".step_mm", adaptPos[i].stepMm, 3);
    emitConfigKV(String("adapt.pos") + i + ".decrease_step_mm", adaptPos[i].decreaseStepMm, 3);
    emitConfigKV(String("adapt.pos") + i + ".min_distance_mm", adaptPos[i].minDistanceMm, 3);
    emitConfigKV(String("adapt.pos") + i + ".max_distance_mm", adaptPos[i].maxDistanceMm, 3);
  }

  emitConfigKV("free_reward.enabled", freeRewardCfg.enabled);
  emitConfigKV("free_reward.after_misses", freeRewardCfg.afterConsecutiveMisses);
  emitConfigKV("free_reward.delay_ms", freeRewardCfg.delayAfterCueMs);

  emitConfigKV("lick.mode", String("digital"));
  emitConfigKV("lick.active_low", lickCfg.activeLow);
  emitConfigKV("lick.debounce_ms", lickCfg.debounceMs);
  emitConfigKV("lick.refractory_ms", lickCfg.refractoryMs);
  emitConfigKV("lick.debug", lickCfg.debug);
  emitConfigKV("lick.threshold_counts", lickCfg.thresholdCounts);
  emitConfigKV("lick.hysteresis_counts", lickCfg.hysteresisCounts);
  emitConfigKV("lick.polarity", lickCfg.polarity);
  emitConfigKV("lick.baseline_alpha", lickCfg.baselineAlpha, 5);

  emitConfigKV("motion.axis.x.ms_per_mm_pos", axisX.msPerMMPos, 3);
  emitConfigKV("motion.axis.x.ms_per_mm_neg", axisX.msPerMMNeg, 3);
  emitConfigKV("motion.axis.x.overhead_ms", axisX.overheadMs, 3);
  emitConfigKV("motion.axis.x.cw_is_positive", axisX.cwIsPositive);

  emitConfigKV("motion.axis.y.ms_per_mm_pos", axisY.msPerMMPos, 3);
  emitConfigKV("motion.axis.y.ms_per_mm_neg", axisY.msPerMMNeg, 3);
  emitConfigKV("motion.axis.y.overhead_ms", axisY.overheadMs, 3);
  emitConfigKV("motion.axis.y.cw_is_positive", axisY.cwIsPositive);

  emitConfigKV("motion.axis.z.ms_per_mm_pos", axisZ.msPerMMPos, 3);
  emitConfigKV("motion.axis.z.ms_per_mm_neg", axisZ.msPerMMNeg, 3);
  emitConfigKV("motion.axis.z.overhead_ms", axisZ.overheadMs, 3);
  emitConfigKV("motion.axis.z.cw_is_positive", axisZ.cwIsPositive);

  auto emitBackend = [&](const char* axisName, BackendAxisMeta &m) {
    emitConfigKV(String("backend.axis.") + axisName + ".mode", String("P0") + String(m.mode));
    emitConfigKV(String("backend.axis.") + axisName + ".microstep", m.microstep);
    emitConfigKV(String("backend.axis.") + axisName + ".lead_mm_rev", m.leadMMRev, 4);
    emitConfigKV(String("backend.axis.") + axisName + ".rpm", m.rpm, 3);
    float pulsesPerRev = 200.0f * (float)m.microstep;
    emitConfigKV(String("backend.axis.") + axisName + ".pulses_per_rev", pulsesPerRev, 1);
    emitConfigKV(String("backend.axis.") + axisName + ".f09_value", pulsesPerRev / 10.0f, 1);
    float mmPerSec = (m.rpm / 60.0f) * m.leadMMRev;
    emitConfigKV(String("backend.axis.") + axisName + ".theoretical_mm_s", mmPerSec, 4);
    float theorMsPerMM = (mmPerSec > 0.000001f) ? (1000.0f / mmPerSec) : 0.0f;
    emitConfigKV(String("backend.axis.") + axisName + ".theoretical_ms_per_mm", theorMsPerMM, 3);
  };

  emitBackend("x", metaX);
  emitBackend("y", metaY);
  emitBackend("z", metaZ);
}

// ============================================================
// Parsing helpers
// ============================================================
String getArg(const String& line, const String& key) {
  String pattern = key + "=";
  int idx = line.indexOf(pattern);
  if (idx < 0) return "";
  idx += pattern.length();
  int end = line.indexOf(' ', idx);
  if (end < 0) end = line.length();
  return line.substring(idx, end);
}

bool parseBoolValue(const String& s, bool &out) {
  if (s.equalsIgnoreCase("1") || s.equalsIgnoreCase("true") || s.equalsIgnoreCase("on")) {
    out = true;
    return true;
  }
  if (s.equalsIgnoreCase("0") || s.equalsIgnoreCase("false") || s.equalsIgnoreCase("off")) {
    out = false;
    return true;
  }
  return false;
}

bool parseIntValue(const String& s, int &out) {
  if (s.length() == 0) return false;
  out = s.toInt();
  return true;
}

bool parseUIntValue(const String& s, uint32_t &out) {
  if (s.length() == 0) return false;
  long v = s.toInt();
  if (v < 0) v = 0;
  out = (uint32_t)v;
  return true;
}

bool parseFloatValue(const String& s, float &out) {
  if (s.length() == 0) return false;
  out = s.toFloat();
  return true;
}

Axis* getAxisByName(const String& s) {
  if (s.equalsIgnoreCase("x")) return &axisX;
  if (s.equalsIgnoreCase("y")) return &axisY;
  if (s.equalsIgnoreCase("z")) return &axisZ;
  return nullptr;
}

BackendAxisMeta* getMetaByAxisName(const String& s) {
  if (s.equalsIgnoreCase("x")) return &metaX;
  if (s.equalsIgnoreCase("y")) return &metaY;
  if (s.equalsIgnoreCase("z")) return &metaZ;
  return nullptr;
}

String normalizeLegacySetKey(const String& key) {
  if (key.equalsIgnoreCase("REWARD_MS")) return "task.reward_ms";
  if (key.equalsIgnoreCase("REWARD_UL")) return "task.reward_ul";
  if (key.equalsIgnoreCase("WATER_LIMIT_UL")) return "task.water_limit_ul";
  if (key.equalsIgnoreCase("REWARD_MODE")) return "task.reward_mode";
  if (key.equalsIgnoreCase("AUTO_REWARD_DELAY_MS")) return "task.auto_reward_delay_ms";
  if (key.equalsIgnoreCase("REWARD_CONTINGENT")) return "task.reward_contingent";
  if (key.equalsIgnoreCase("AUTO_REWARD_ENABLED")) return "task.auto_reward_enabled";
  if (key.equalsIgnoreCase("ENFORCE_NO_LICK")) return "task.enforce_no_lick";
  if (key.equalsIgnoreCase("MANUAL_REWARD_ALLOWED")) return "task.manual_reward_allowed";
  if (key.equalsIgnoreCase("SETTLE_MS")) return "task.settle_ms";
  if (key.equalsIgnoreCase("POST_REWARD_HOLD_MS")) return "task.post_reward_hold_ms";
  if (key.equalsIgnoreCase("PRE_CUE_MIN_MS")) return "task.pre_cue_min_ms";
  if (key.equalsIgnoreCase("PRE_CUE_MAX_MS")) return "task.pre_cue_max_ms";
  if (key.equalsIgnoreCase("RESPONSE_WINDOW_MS")) return "task.response_window_ms";
  if (key.equalsIgnoreCase("ITI_MIN_MS")) return "task.iti_min_ms";
  if (key.equalsIgnoreCase("ITI_JITTER_MS")) return "task.iti_jitter_ms";
  if (key.equalsIgnoreCase("BLOCK_SIZE")) return "task.block_size";
  if (key.equalsIgnoreCase("BLOCK_SIZE_MIN")) return "task.block_size_min";
  if (key.equalsIgnoreCase("BLOCK_SIZE_MAX")) return "task.block_size_max";
  if (key.equalsIgnoreCase("TARGET_TRIALS_PER_POSITION")) return "task.target_trials_per_position";
  if (key.equalsIgnoreCase("TARGET_TRIALS_PER_POSITION_ENABLED")) return "task.target_trials_per_position_enabled";
  if (key.equalsIgnoreCase("MAX_DURATION_MIN")) return "task.max_duration_min";
  if (key.equalsIgnoreCase("MAX_DURATION_ENABLED")) return "task.max_duration_enabled";
  if (key.equalsIgnoreCase("SCHEDULING_MODE")) return "task.scheduling_mode";
  if (key.equalsIgnoreCase("STOP_MODE")) return "task.stop_mode";
  if (key.equalsIgnoreCase("CUE_FREQUENCY_HZ")) return "cue.frequency_hz";
  if (key.equalsIgnoreCase("CUE_DURATION_MS")) return "cue.duration_ms";
  if (key.equalsIgnoreCase("CUE_VOLUME_PCT")) return "cue.volume_pct";
  if (key.equalsIgnoreCase("ADAPT_ENABLED")) return "adapt.enabled";
  if (key.equalsIgnoreCase("HITS_TO_ADVANCE")) return "adapt.hits_to_advance";
  if (key.equalsIgnoreCase("ADAPT_STEP_MM")) return "adapt.step_mm";
  if (key.equalsIgnoreCase("MAX_DISTANCE_MM")) return "adapt.max_distance_mm";
  if (key.equalsIgnoreCase("MISSES_TO_DECREASE")) return "adapt.misses_to_decrease";
  if (key.equalsIgnoreCase("DECREASE_STEP_MM")) return "adapt.decrease_step_mm";
  if (key.equalsIgnoreCase("MIN_DISTANCE_MM")) return "adapt.min_distance_mm";
  if (key.equalsIgnoreCase("FREE_REWARD_ENABLED")) return "free_reward.enabled";
  if (key.equalsIgnoreCase("FREE_REWARD_AFTER_MISSES")) return "free_reward.after_misses";
  if (key.equalsIgnoreCase("FREE_REWARD_DELAY_MS")) return "free_reward.delay_ms";
  if (key.equalsIgnoreCase("LICK_ACTIVE_LOW")) return "lick.active_low";
  if (key.equalsIgnoreCase("LICK_DEBOUNCE_MS")) return "lick.debounce_ms";
  if (key.equalsIgnoreCase("LICK_REFRACTORY_MS")) return "lick.refractory_ms";
  if (key.equalsIgnoreCase("LICK_DEBUG")) return "lick.debug";
  return key;
}

// ============================================================
// Command handlers
// ============================================================
void handleHelp() {
  Serial.println("INFO kind=help commands=PING,HELP,START,STOP,HOME,CUE,REWARD,CUEREWARD,HOLDREWARDS,RESUMEREWARDS,RESETSESSION,GET,SET,MOVE,CAL  move_modes=jog,xyz,pos");
}

void handleGet(const String& kind) {
  if (kind.equalsIgnoreCase("status")) {
    emitStatus();
    return;
  }
  if (kind.equalsIgnoreCase("positions")) {
    emitPositions();
    return;
  }
  if (kind.equalsIgnoreCase("config")) {
    emitConfig();
    return;
  }
  if (kind.equalsIgnoreCase("stats")) {
    emitStats();
    return;
  }
  if (kind.equalsIgnoreCase("all")) {
    emitStatus();
    emitPositions();
    emitConfig();
    emitStats();
    return;
  }
  emitErr("get", "bad_kind", kind);
}

bool handleSet(const String& key, const String& value) {
  bool btmp;
  int itmp;
  uint32_t utmp;
  float ftmp;

  if (key == "task.reward_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    cfg.rewardOpenMs = utmp; return true;
  }
  if (key == "task.reward_ul") {
    if (!parseFloatValue(value, ftmp)) return false;
    cfg.estimatedRewardUL = ftmp; return true;
  }
  if (key == "task.water_limit_ul") {
    if (!parseFloatValue(value, ftmp)) return false;
    cfg.sessionWaterLimitUL = ftmp; return true;
  }
  if (key == "task.reward_mode") {
    if (value.equalsIgnoreCase("contingent")) {
      cfg.rewardMode = REWARD_MODE_CONTINGENT; return true;
    }
    if (value.equalsIgnoreCase("auto_after_delay") || value.equalsIgnoreCase("auto")) {
      cfg.rewardMode = REWARD_MODE_AUTO; return true;
    }
    if (value.equalsIgnoreCase("contingent_or_auto_after_delay") || value.equalsIgnoreCase("contingent_or_auto")) {
      cfg.rewardMode = REWARD_MODE_CONTINGENT_OR_AUTO; return true;
    }
    return false;
  }
  if (key == "task.auto_reward_delay_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    cfg.autoRewardDelayMs = utmp; return true;
  }
  if (key == "task.auto_hold_after_miss_enabled") {
    if (!parseBoolValue(value, btmp)) return false;
    cfg.autoHoldAfterMissEnabled = btmp;
    if (!btmp) setAutoRewardHold(false, false);
    return true;
  }
  if (key == "task.auto_hold_after_miss_threshold") {
    if (!parseUIntValue(value, utmp)) return false;
    if (utmp < 1) utmp = 1;
    cfg.autoHoldAfterMissThreshold = utmp;
    return true;
  }
  if (key == "task.rewards_held" || key == "task.reward_hold") {
    if (!parseBoolValue(value, btmp)) return false;
    if (btmp) setManualRewardHold(true, false);
    else clearAllRewardHolds(false);
    return true;
  }
  if (key == "task.reward_contingent") {
    if (!parseBoolValue(value, btmp)) return false;
    cfg.rewardMode = btmp ? REWARD_MODE_CONTINGENT : REWARD_MODE_AUTO; return true;
  }
  if (key == "task.auto_reward_enabled") {
    if (!parseBoolValue(value, btmp)) return false;
    cfg.rewardMode = btmp ? REWARD_MODE_AUTO : REWARD_MODE_CONTINGENT; return true;
  }
  if (key == "task.enforce_no_lick") {
    if (!parseBoolValue(value, btmp)) return false;
    cfg.enforceNoLick = btmp; return true;
  }
  if (key == "task.manual_reward_allowed") {
    if (!parseBoolValue(value, btmp)) return false;
    cfg.manualRewardAllowed = btmp; return true;
  }
  if (key == "task.settle_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    cfg.settleMs = utmp; return true;
  }
  if (key == "task.post_reward_hold_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    cfg.postRewardHoldMs = utmp; return true;
  }
  if (key == "task.pre_cue_min_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    cfg.preCueMinMs = utmp; return true;
  }
  if (key == "task.pre_cue_max_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    cfg.preCueMaxMs = utmp; return true;
  }
  if (key == "task.response_window_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    cfg.responseWindowMs = utmp; return true;
  }
  if (key == "task.iti_min_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    cfg.itiMinMs = utmp; return true;
  }
  if (key == "task.iti_jitter_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    cfg.itiJitterMs = utmp; return true;
  }
  if (key == "task.block_size") {
    if (!parseIntValue(value, itmp)) return false;
    if (itmp < 1) itmp = 1;
    cfg.blockSize = (uint8_t)itmp;
    cfg.blockSizeMin = (uint8_t)itmp;
    cfg.blockSizeMax = (uint8_t)itmp;
    return true;
  }
  if (key == "task.block_size_min") {
    if (!parseIntValue(value, itmp)) return false;
    if (itmp < 1) itmp = 1;
    cfg.blockSizeMin = (uint8_t)itmp; normalizeSchedulerConfig(); return true;
  }
  if (key == "task.block_size_max") {
    if (!parseIntValue(value, itmp)) return false;
    if (itmp < 1) itmp = 1;
    cfg.blockSizeMax = (uint8_t)itmp; normalizeSchedulerConfig(); return true;
  }
  if (key == "task.target_trials_per_position_enabled") {
    if (!parseBoolValue(value, btmp)) return false;
    cfg.targetTrialsPerPositionEnabled = btmp; return true;
  }
  if (key == "task.target_trials_per_position") {
    if (!parseUIntValue(value, utmp)) return false;
    if (utmp < 1) utmp = 1;
    cfg.targetTrialsPerPosition = utmp; return true;
  }
  if (key == "task.max_duration_enabled") {
    if (!parseBoolValue(value, btmp)) return false;
    cfg.maxDurationEnabled = btmp; return true;
  }
  if (key == "task.max_duration_min") {
    if (!parseUIntValue(value, utmp)) return false;
    if (utmp < 1) utmp = 1;
    cfg.maxDurationMin = utmp; return true;
  }
  if (key == "task.scheduling_mode") {
    if (value.equalsIgnoreCase("balanced_block_cycles") || value.equalsIgnoreCase("balanced")) { cfg.scheduleMode = SCHEDULE_BALANCED_BLOCK_CYCLES; return true; }
    if (value.equalsIgnoreCase("random_blocks") || value.equalsIgnoreCase("random")) { cfg.scheduleMode = SCHEDULE_RANDOM_BLOCKS; return true; }
    return false;
  }
  if (key == "task.stop_mode") {
    if (value.equalsIgnoreCase("end_of_current_block") || value.equalsIgnoreCase("block")) { cfg.stopMode = STOP_END_OF_CURRENT_BLOCK; return true; }
    if (value.equalsIgnoreCase("end_of_balanced_cycle") || value.equalsIgnoreCase("cycle")) { cfg.stopMode = STOP_END_OF_BALANCED_CYCLE; return true; }
    return false;
  }
  if (key == "sync.pulse_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    syncPulseMinMs = utmp; syncPulseMaxMs = utmp; normalizeSchedulerConfig(); return true;
  }
  if (key == "sync.min_pulse_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    syncPulseMinMs = utmp; normalizeSchedulerConfig(); return true;
  }
  if (key == "sync.max_pulse_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    syncPulseMaxMs = utmp; normalizeSchedulerConfig(); return true;
  }
  if (key == "sync.min_interval_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    syncMinIntervalMs = utmp; normalizeSchedulerConfig(); return true;
  }
  if (key == "sync.max_interval_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    syncMaxIntervalMs = utmp; normalizeSchedulerConfig(); return true;
  }
  if (key == "cue.frequency_hz") {
    if (!parseUIntValue(value, utmp)) return false;
    cueCfg.frequencyHz = utmp > 0 ? utmp : 1;
    return true;
  }
  if (key == "cue.duration_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    cueCfg.durationMs = utmp;
    return true;
  }
  if (key == "cue.volume_pct") {
    if (!parseIntValue(value, itmp)) return false;
    if (itmp < 0) itmp = 0;
    if (itmp > 100) itmp = 100;
    cueCfg.volumePct = (uint8_t)itmp;
    return true;
  }

  if (key.startsWith("task.enable_pos")) {
    String idxStr = key.substring(String("task.enable_pos").length());
    int idx = idxStr.toInt();
    if (idx < 0 || idx >= NUM_POSITIONS) return false;
    if (!parseBoolValue(value, btmp)) return false;
    cfg.enabledPositions[idx] = btmp; return true;
  }

  if (key == "geom.dist_close_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    geom.distanceTierMm[0] = ftmp;
    resetAdaptiveDistances();
    recomputeAllGeneratedPositions();
    return true;
  }
  if (key == "geom.dist_far_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    geom.distanceTierMm[1] = ftmp;
    resetAdaptiveDistances();
    recomputeAllGeneratedPositions();
    return true;
  }
  if (key == "geom.az_center_deg") {
    if (!parseFloatValue(value, ftmp)) return false;
    geom.azimuthDeg[0] = ftmp;
    recomputeAllGeneratedPositions();
    return true;
  }
  if (key == "geom.az_left_deg") {
    if (!parseFloatValue(value, ftmp)) return false;
    geom.azimuthDeg[1] = ftmp;
    recomputeAllGeneratedPositions();
    return true;
  }
  if (key == "geom.az_right_deg") {
    if (!parseFloatValue(value, ftmp)) return false;
    geom.azimuthDeg[2] = ftmp;
    recomputeAllGeneratedPositions();
    return true;
  }
  if (key == "geom.down_angle_deg") {
    if (!parseFloatValue(value, ftmp)) return false;
    geom.downwardAngleDeg = ftmp;
    recomputeAllGeneratedPositions();
    return true;
  }
  if (key == "geom.head_roll_deg") {
    if (!parseFloatValue(value, ftmp)) return false;
    geom.headRollDeg = ftmp;
    recomputeAllGeneratedPositions();
    return true;
  }

  if (key == "motion.mouth_origin.x_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    mouthOrigin.x = ftmp; recomputeAllGeneratedPositions(); return true;
  }
  if (key == "motion.mouth_origin.y_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    mouthOrigin.y = ftmp; recomputeAllGeneratedPositions(); return true;
  }
  if (key == "motion.mouth_origin.z_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    mouthOrigin.z = ftmp; recomputeAllGeneratedPositions(); return true;
  }
  if (key == "motion.dock.x_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    dockPosition.x = ftmp; return true;
  }
  if (key == "motion.dock.y_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    dockPosition.y = ftmp; return true;
  }
  if (key == "motion.dock.z_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    dockPosition.z = ftmp; return true;
  }
  if (key == "motion.safe_z_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    safePosition.z = ftmp; return true;
  }

  if (key == "adapt.enabled") {
    if (!parseBoolValue(value, btmp)) return false;
    adapt.enabled = btmp; if (!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true;
  }
  if (key == "adapt.use_per_position") {
    if (!parseBoolValue(value, btmp)) return false;
    adaptUsePerPosition = btmp; if (!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true;
  }
  if (key == "adapt.hits_to_advance") {
    if (!parseIntValue(value, itmp)) return false;
    if (itmp < 1) itmp = 1;
    adapt.hitsToAdvance = itmp; if (!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true;
  }
  if (key == "adapt.misses_to_decrease") {
    if (!parseIntValue(value, itmp)) return false;
    if (itmp < 1) itmp = 1;
    adapt.missesToDecrease = itmp; if (!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true;
  }
  if (key == "adapt.step_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    if (ftmp < 0.0f) ftmp = 0.0f;
    adapt.stepMm = ftmp; if (!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true;
  }
  if (key == "adapt.decrease_step_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    if (ftmp < 0.0f) ftmp = 0.0f;
    adapt.decreaseStepMm = ftmp; if (!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true;
  }
  if (key == "adapt.min_distance_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    adapt.minDistanceMm = ftmp; if (!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true;
  }
  if (key == "adapt.max_distance_mm") {
    if (!parseFloatValue(value, ftmp)) return false;
    adapt.maxDistanceMm = ftmp; if (!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true;
  }
  if (key.startsWith("adapt.pos")) {
    int dotIx = key.indexOf('.', String("adapt.pos").length());
    if (dotIx < 0) return false;
    int idx = key.substring(String("adapt.pos").length(), dotIx).toInt();
    if (idx < 0 || idx >= NUM_POSITIONS) return false;
    String subkey = key.substring(dotIx + 1);
    if (subkey == "enabled") { if (!parseBoolValue(value, btmp)) return false; adaptPos[idx].enabled = btmp; return true; }
    if (subkey == "hits_to_advance") { if (!parseIntValue(value, itmp)) return false; if (itmp < 1) itmp = 1; adaptPos[idx].hitsToAdvance = itmp; return true; }
    if (subkey == "misses_to_decrease") { if (!parseIntValue(value, itmp)) return false; if (itmp < 1) itmp = 1; adaptPos[idx].missesToDecrease = itmp; return true; }
    if (subkey == "step_mm") { if (!parseFloatValue(value, ftmp)) return false; if (ftmp < 0.0f) ftmp = 0.0f; adaptPos[idx].stepMm = ftmp; return true; }
    if (subkey == "decrease_step_mm") { if (!parseFloatValue(value, ftmp)) return false; if (ftmp < 0.0f) ftmp = 0.0f; adaptPos[idx].decreaseStepMm = ftmp; return true; }
    if (subkey == "min_distance_mm") { if (!parseFloatValue(value, ftmp)) return false; adaptPos[idx].minDistanceMm = ftmp; return true; }
    if (subkey == "max_distance_mm") { if (!parseFloatValue(value, ftmp)) return false; adaptPos[idx].maxDistanceMm = ftmp; return true; }
    return false;
  }

  if (key == "free_reward.enabled") {
    if (!parseBoolValue(value, btmp)) return false;
    freeRewardCfg.enabled = btmp; return true;
  }
  if (key == "free_reward.after_misses") {
    if (!parseIntValue(value, itmp)) return false;
    freeRewardCfg.afterConsecutiveMisses = itmp; return true;
  }
  if (key == "free_reward.delay_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    freeRewardCfg.delayAfterCueMs = utmp; return true;
  }

  if (key == "lick.active_low") {
    if (!parseBoolValue(value, btmp)) return false;
    lickCfg.activeLow = btmp; return true;
  }
  if (key == "lick.debounce_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    lickCfg.debounceMs = utmp; return true;
  }
  if (key == "lick.threshold_counts") {
    if (!parseIntValue(value, itmp)) return false;
    lickCfg.thresholdCounts = itmp; return true;
  }
  if (key == "lick.hysteresis_counts") {
    if (!parseIntValue(value, itmp)) return false;
    lickCfg.hysteresisCounts = itmp; return true;
  }
  if (key == "lick.polarity") {
    if (!parseIntValue(value, itmp)) return false;
    lickCfg.polarity = itmp;
    lickCfg.activeLow = (itmp < 0); return true;
  }
  if (key == "lick.baseline_alpha") {
    if (!parseFloatValue(value, ftmp)) return false;
    if (ftmp < 0.0f) ftmp = 0.0f;
    if (ftmp > 1.0f) ftmp = 1.0f;
    lickCfg.baselineAlpha = ftmp; return true;
  }
  if (key == "lick.refractory_ms") {
    if (!parseUIntValue(value, utmp)) return false;
    lickCfg.refractoryMs = utmp; return true;
  }
  if (key == "lick.debug") {
    if (!parseBoolValue(value, btmp)) return false;
    lickCfg.debug = btmp; return true;
  }


  if (key == "motion.axis.x.ms_per_mm_pos") {
    if (!parseFloatValue(value, ftmp)) return false;
    axisX.msPerMMPos = ftmp; return true;
  }
  if (key == "motion.axis.x.ms_per_mm_neg") {
    if (!parseFloatValue(value, ftmp)) return false;
    axisX.msPerMMNeg = ftmp; return true;
  }
  if (key == "motion.axis.x.overhead_ms") {
    if (!parseFloatValue(value, ftmp)) return false;
    axisX.overheadMs = ftmp; return true;
  }
  if (key == "motion.axis.x.cw_is_positive") {
    if (!parseBoolValue(value, btmp)) return false;
    axisX.cwIsPositive = btmp; return true;
  }

  if (key == "motion.axis.y.ms_per_mm_pos") {
    if (!parseFloatValue(value, ftmp)) return false;
    axisY.msPerMMPos = ftmp; return true;
  }
  if (key == "motion.axis.y.ms_per_mm_neg") {
    if (!parseFloatValue(value, ftmp)) return false;
    axisY.msPerMMNeg = ftmp; return true;
  }
  if (key == "motion.axis.y.overhead_ms") {
    if (!parseFloatValue(value, ftmp)) return false;
    axisY.overheadMs = ftmp; return true;
  }
  if (key == "motion.axis.y.cw_is_positive") {
    if (!parseBoolValue(value, btmp)) return false;
    axisY.cwIsPositive = btmp; return true;
  }

  if (key == "motion.axis.z.ms_per_mm_pos") {
    if (!parseFloatValue(value, ftmp)) return false;
    axisZ.msPerMMPos = ftmp; return true;
  }
  if (key == "motion.axis.z.ms_per_mm_neg") {
    if (!parseFloatValue(value, ftmp)) return false;
    axisZ.msPerMMNeg = ftmp; return true;
  }
  if (key == "motion.axis.z.overhead_ms") {
    if (!parseFloatValue(value, ftmp)) return false;
    axisZ.overheadMs = ftmp; return true;
  }
  if (key == "motion.axis.z.cw_is_positive") {
    if (!parseBoolValue(value, btmp)) return false;
    axisZ.cwIsPositive = btmp; return true;
  }

  if (key.startsWith("backend.axis.")) {
    String rest = key.substring(String("backend.axis.").length());
    int dot = rest.indexOf('.');
    if (dot < 0) return false;
    String axisName = rest.substring(0, dot);
    String leaf = rest.substring(dot + 1);
    BackendAxisMeta* m = getMetaByAxisName(axisName);
    if (!m) return false;

    if (leaf == "mode") {
      if (value.equalsIgnoreCase("P02")) {
        m->mode = 2;
        return true;
      }
      if (!parseIntValue(value, itmp)) return false;
      m->mode = itmp;
      return true;
    }
    if (leaf == "microstep") {
      if (!parseIntValue(value, itmp)) return false;
      if (itmp < 1) itmp = 1;
      m->microstep = itmp;
      return true;
    }
    if (leaf == "lead_mm_rev") {
      if (!parseFloatValue(value, ftmp)) return false;
      m->leadMMRev = ftmp;
      return true;
    }
    if (leaf == "rpm") {
      if (!parseFloatValue(value, ftmp)) return false;
      m->rpm = ftmp;
      return true;
    }
  }

  return false;
}

void handleMove(const String& rest) {
  abortMotion = false;   // a fresh motion request clears any latched abort
  String mode = getArg(rest, "mode");
  if (mode.length() == 0) {
    emitErr("move", "missing_mode");
    return;
  }

  if (mode.equalsIgnoreCase("jog")) {
    String axisName = getArg(rest, "axis");
    String mmStr = getArg(rest, "mm");
    Axis* a = getAxisByName(axisName);
    float mm;
    if (!a) {
      emitErr("move", "bad_axis", axisName);
      return;
    }
    if (!parseFloatValue(mmStr, mm)) {
      emitErr("move", "bad_mm", mmStr);
      return;
    }
    if (!moveAxisRelative(*a, mm)) {
      emitErr("move", "failed");
      return;
    }
    emitOK("move", "mode=jog axis=" + axisName + " mm=" + String(mm, 3));
    emitStatus();
    return;
  }

  if (mode.equalsIgnoreCase("pos")) {
    int idx;
    if (!parseIntValue(getArg(rest, "idx"), idx)) {
      emitErr("move", "missing_idx");
      return;
    }
    if (idx < 0 || idx >= NUM_POSITIONS) {
      emitErr("move", "bad_idx", String(idx));
      return;
    }
    if (!moveToPositionSafe(positions[idx])) {
      emitErr("move", "failed");
      return;
    }
    currentTrialPos = idx;
    emitPositionCode((uint8_t)currentTrialPos);
    emitEventDetail("position", String("idx=") + currentTrialPos + " manual=1");
    emitOK("move", "mode=pos idx=" + String(idx));
    emitStatus();
    return;
  }

  if (mode.equalsIgnoreCase("xyz")) {
    float x, y, z;
    if (!parseFloatValue(getArg(rest, "x"), x) ||
        !parseFloatValue(getArg(rest, "y"), y) ||
        !parseFloatValue(getArg(rest, "z"), z)) {
      emitErr("move", "missing_xyz");
      return;
    }
    Vec3 target = {x, y, z};
    if (!moveToPositionSafe(target)) {
      emitErr("move", "failed");
      return;
    }
    currentTrialPos = -1;
    emitOK("move", "mode=xyz");
    emitStatus();
    return;
  }

  emitErr("move", "bad_mode", mode);
}

void handleCal(const String& rest) {
  String kind = getArg(rest, "kind");
  if (kind.equalsIgnoreCase("reward")) {
    uint32_t pulses = 0;
    if (!parseUIntValue(getArg(rest, "pulses"), pulses)) {
      emitErr("cal", "missing_pulses");
      return;
    }
    for (uint32_t i = 0; i < pulses; i++) {
      pendingRewardTrigger = REWARD_TRIGGER_CALIBRATION;
      openRewardValve(cfg.rewardOpenMs);
      pulsePin(selectedRewardIndicatorPin(), TTL_PULSE_MS);
      totalRewards++;
      sessionWaterDeliveredUL += cfg.estimatedRewardUL;
      emitEvent("reward_cal_pulse");
      pendingRewardTrigger = REWARD_TRIGGER_NONE;
      serviceWait(100);
    }
    emitOK("cal", "kind=reward pulses=" + String(pulses));
    return;
  }

  emitErr("cal", "bad_kind", kind);
}

// ============================================================
// Serial protocol
// ============================================================
bool handleSerialDuringBlocking() {
  String line;
  if (!readSerialLineNonBlocking(line)) return false;

  int sp = line.indexOf(' ');
  String cmd = (sp < 0) ? line : line.substring(0, sp);
  String rest = (sp < 0) ? "" : line.substring(sp + 1);
  rest.trim();

  if (cmd.equalsIgnoreCase("PING")) {
    emitOK("ping", "t_ms=" + String(millis()));
    return true;
  }
  if (cmd.equalsIgnoreCase("HELP")) {
    handleHelp();
    return true;
  }
  if (cmd.equalsIgnoreCase("HOLDREWARDS")) {
    setManualRewardHold(true, true);
    emitOK("holdrewards");
    return true;
  }
  if (cmd.equalsIgnoreCase("RESUMEREWARDS")) {
    clearAllRewardHolds(true);
    emitOK("resumerewards");
    return true;
  }
  if (cmd.equalsIgnoreCase("SET")) {
    int eq = rest.indexOf('=');
    String key;
    String value;
    if (eq >= 0) {
      key = rest.substring(0, eq);
      value = rest.substring(eq + 1);
    } else {
      emitErr("set", "missing_equals");
      return true;
    }
    key.trim();
    value.trim();
    if (key == "task.rewards_held" || key == "task.reward_hold") {
      if (!handleSet(key, value)) emitErr("set", "bad_key_or_value", key);
      else emitOK("set", "key=" + key + " value=" + value);
    } else {
      emitErr("busy", "motion_wait", "SET");
    }
    return true;
  }
  if (cmd.equalsIgnoreCase("GET")) {
    String kind = getArg(rest, "kind");
    if (kind.length() == 0) emitErr("get", "missing_kind");
    else handleGet(kind);
    return true;
  }
  if (cmd.equalsIgnoreCase("STOP")) {
    abortMotion = true;          // unwind the whole move sequence, not just this segment
    cfg.sessionRunning = false;
    runState = ST_IDLE;
    closeTrialAndCueGates();
    smcAllRelease(axisX);
    smcAllRelease(axisY);
    smcAllRelease(axisZ);
    emitOK("stop");
    return true;
  }

  emitErr("busy", "motion_wait", cmd);
  return true;
}

bool readSerialLineNonBlocking(String &line) {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line = serialRxBuffer;
      serialRxBuffer = "";
      line.trim();
      if (line.length() == 0) continue;
      return true;
    }
    if (c >= 32 && c <= 126) {
      if (serialRxBuffer.length() < 160) serialRxBuffer += c;
      else serialRxBuffer = "";
    }
  }
  return false;
}

void handleSerial() {
  String line;
  if (!readSerialLineNonBlocking(line)) return;

  int sp = line.indexOf(' ');
  String cmd = (sp < 0) ? line : line.substring(0, sp);
  String rest = (sp < 0) ? "" : line.substring(sp + 1);
  rest.trim();

  if (cmd.equalsIgnoreCase("PING")) {
    emitOK("ping", "t_ms=" + String(millis()));
    return;
  }
  if (cmd.equalsIgnoreCase("HELP")) {
    handleHelp();
    return;
  }
  if (cmd.equalsIgnoreCase("START")) {
    abortMotion = false;
    resetSessionStats();
    resetAdaptiveDistances();
    recomputeAllGeneratedPositions();
    normalizeSchedulerConfig();
    chooseNextBlockPosition();
    cfg.sessionRunning = true;
    sessionStartMs = millis();
    nextSyncAt = millis() + random(syncMinIntervalMs, syncMaxIntervalMs + 1);
    runState = ST_MOVE_TO_TARGET;
    emitOK("start");
    return;
  }
  if (cmd.equalsIgnoreCase("STOP")) {
    cfg.sessionRunning = false;
    runState = ST_IDLE;
    abortMotion = true;          // cleared by the next START / MOVE / HOME / RESETSESSION
    closeTrialAndCueGates();
    allAxesStop();
    emitOK("stop");
    return;
  }
  if (cmd.equalsIgnoreCase("HOME")) {
    abortMotion = false;
    if (!USE_HOME_SWITCHES) {
      emitErr("home", "disabled", "no_home_switches");
    } else if (homeAllAxes()) {
      emitOK("home");
    } else {
      emitErr("home", "failed");
    }
    return;
  }
  if (cmd.equalsIgnoreCase("SETCURRENT")) {
    float x, y, z;
    if (!parseFloatValue(getArg(rest, "x"), x) ||
        !parseFloatValue(getArg(rest, "y"), y) ||
        !parseFloatValue(getArg(rest, "z"), z)) {
      emitErr("setcurrent", "missing_xyz");
      return;
    }
    abortMotion = false;   // re-referencing after an aborted move re-arms motion
    setCurrentPosition(x, y, z);
    emitEventDetail("manual_reference_set", String("x_mm=") + String(x, 3) + " y_mm=" + String(y, 3) + " z_mm=" + String(z, 3));
    emitOK("setcurrent", String("x=") + String(x, 3) + " y=" + String(y, 3) + " z=" + String(z, 3));
    emitStatus();
    return;
  }
  if (cmd.equalsIgnoreCase("REWARD")) {
    if (!cfg.manualRewardAllowed) {
      emitErr("reward", "manual_disabled");
      return;
    }
    deliverRewardForTrigger(REWARD_TRIGGER_MANUAL);
    emitOK("reward");
    return;
  }
  if (cmd.equalsIgnoreCase("CUEREWARD")) {
    if (!cfg.manualRewardAllowed) {
      emitErr("cuereward", "manual_disabled");
      return;
    }
    playCue();
    emitEvent("cue_only");
    deliverRewardForTrigger(REWARD_TRIGGER_MANUAL);
    emitOK("cuereward");
    return;
  }
  if (cmd.equalsIgnoreCase("CUE")) {
    cueOnly();
    emitOK("cue");
    return;
  }
  if (cmd.equalsIgnoreCase("HOLDREWARDS")) {
    setManualRewardHold(true, true);
    emitOK("holdrewards");
    return;
  }
  if (cmd.equalsIgnoreCase("RESUMEREWARDS")) {
    clearAllRewardHolds(true);
    emitOK("resumerewards");
    return;
  }
  if (cmd.equalsIgnoreCase("RESETSESSION")) {
    abortMotion = false;
    resetSessionStats();
    resetAdaptiveDistances();
    recomputeAllGeneratedPositions();
    normalizeSchedulerConfig();
    nextSyncAt = millis() + random(syncMinIntervalMs, syncMaxIntervalMs + 1);
    emitOK("resetsession");
    return;
  }
  if (cmd.equalsIgnoreCase("GET")) {
    String kind = getArg(rest, "kind");
    if (kind.length() == 0) {
      emitErr("get", "missing_kind");
      return;
    }
    handleGet(kind);
    return;
  }
  if (cmd.equalsIgnoreCase("SET")) {
    int eq = rest.indexOf('=');
    String key;
    String value;
    if (eq >= 0) {
      key = rest.substring(0, eq);
      value = rest.substring(eq + 1);
    } else {
      int sp = rest.indexOf(' ');
      if (sp < 0) {
        emitErr("set", "missing_equals");
        return;
      }
      key = rest.substring(0, sp);
      value = rest.substring(sp + 1);
      key = normalizeLegacySetKey(key);
    }
    key.trim();
    value.trim();
    if (!handleSet(key, value)) {
      emitErr("set", "bad_key_or_value", key);
      return;
    }
    emitOK("set", "key=" + key + " value=" + value);
    return;
  }
  if (cmd.equalsIgnoreCase("MOVE")) {
    handleMove(rest);
    return;
  }
  if (cmd.equalsIgnoreCase("CAL")) {
    handleCal(rest);
    return;
  }
  if (cmd.equalsIgnoreCase("CALREWARD")) {
    String pulsesStr = rest;
    pulsesStr.trim();
    if (pulsesStr.length() == 0) {
      emitErr("cal", "missing_pulses");
      return;
    }
    handleCal(String("kind=reward pulses=") + pulsesStr);
    return;
  }

  emitErr("unknown", "bad_cmd", cmd);
}
