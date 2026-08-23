// v39 (2026-08-23): based on v38 interrupt-driven lick-onset capture, plus auto reward-hold now clears
// on any detected lick level, not only on a new lick_on edge. This lets rewards resume if the animal
// re-contacts or continues contacting the spout between trials after an auto-hold was triggered.
// Serial protocol otherwise unchanged from v38.
#include <Wire.h>
#include <ZaberShield.h>
#include <ZaberConnection.h>
#include <ZaberCommand.h>
#include <EEPROM.h>
#include <math.h>

// ============================================================
// Arduino Mega 2560 / Elegoo Mega firmware
// Backend: Zaber X-AS01 shield + daisy-chained Zaber controllers
// Uses the Zaber Shield / Connection / Command headers
// Assumes one axis per device (device IDs configurable for X/Y/Z)
// One digital lick input, one active solenoid, one movable spout
// ============================================================

// ---------- Zaber shield ----------
using namespace Zaber;
Shield shield(ZABERSHIELD_ADDRESS_AA);
Connection connection(shield);

// ---------- I/O pins ----------
static const uint8_t PIN_LICK_IN = 2;        // digital lick input from breakout board
static const uint8_t PIN_TTL_REWARD = 3;      // reward TTL to DAQ
static const uint8_t PIN_SYNC = 4;
static const uint8_t PIN_CUE_TTL = 5;         // cue/event TTL to DAQ
static const uint8_t PIN_TRIAL_START = 6;
static const uint8_t PIN_STARTSTOP_BUTTON = 7; // shield pushbutton on D7

// The physical shield pushbutton is DISABLED: sessions start and stop from the host
// only, so the button cannot start or halt a task. Gated inside updateButton() rather
// than at its call sites, so every path is covered (main loop, reward-valve wait).
// NOTE: with this false there is no LOCAL abort -- if the host link drops mid-session
// nothing on the bench can stop the stages. Flip to true to restore the button.
static const bool ENABLE_STARTSTOP_BUTTON = false;
static const uint8_t PIN_SOLENOID = 8;        // active solenoid output
static const uint8_t PIN_TTL_TRIAL_STOP = 9;  // trial-stop TTL to DAQ
static const uint8_t PIN_TTL_POS0 = 23;
static const uint8_t PIN_TTL_POS1 = 29;   // D29 on the widefield bench (WIRING_NOTES); pin 24 is the
                                          // co-tenant's DelayIndicatorPin and must not be driven
static const uint8_t PIN_TTL_POS2 = 25;
static const uint8_t PIN_TTL_POS_STB = 26;
static const uint8_t PIN_CUE_AUDIO = 11;      // actual audio tone output to amplifier

// ---------- Timing ----------
static const uint32_t TTL_PULSE_MS = 5;
static const uint32_t TRIAL_STOP_PULSE_MS = 8;

// ---------- Early prototypes ----------
// These were relied on the Arduino IDE's auto-generated prototypes in v36.
// Declared explicitly so the sketch also builds under a plain C++ compiler and
// does not depend on preprocessor behaviour.
void emitEvent(const char* name);
void emitConfigKV(const String& key, bool value);
void zRefreshAllAxisPosMM();
void chooseNextBlockPosition();
static const uint32_t POSITION_STROBE_MS = 10;
static const uint32_t DEFAULT_SETTLE_MS = 150;
static const uint32_t DEFAULT_POST_REWARD_HOLD_MS = 10000;
static const uint32_t DEFAULT_PRE_CUE_MIN_MS = 3000;
static const uint32_t DEFAULT_PRE_CUE_MAX_MS = 5000;
static const uint32_t DEFAULT_RESPONSE_WINDOW_MS = 5000;
static const uint32_t DEFAULT_ITI_MIN_MS = 1500;
static const uint32_t DEFAULT_ITI_JITTER_MS = 1500;
static const uint32_t DEFAULT_AUTO_REWARD_DELAY_MS = 500;
static const uint32_t DEFAULT_TARGET_TRIALS_PER_POSITION = 50;
static const uint32_t DEFAULT_MAX_DURATION_MIN = 60;
static const uint32_t BUTTON_DEBOUNCE_MS = 40;

static const uint32_t SYNC_PULSE_MS_DEFAULT = 2;
static const uint32_t SYNC_MIN_INTERVAL_MS_DEFAULT = 70;
static const uint32_t SYNC_MAX_INTERVAL_MS_DEFAULT = 170;

// ---------- Geometry ----------
static const uint8_t NUM_POSITIONS = 6;
static const uint8_t NUM_DISTANCE_TIERS = 2;
static const uint8_t NUM_AZIMUTHS = 3;

struct Vec3 {
  float x;
  float y;
  float z;
};

struct ZAxis {
  const char* name;
  uint8_t deviceId;
  uint8_t axisNumber;       // usually 1 for one-axis controllers; included for future expansion
  float posMM;
  float unitsPerMM;         // native controller units per mm
};

struct GeometryConfig {
  float distanceTierMm[NUM_DISTANCE_TIERS];
  float azimuthDeg[NUM_AZIMUTHS];
  float downwardAngleDeg;
  float headRollDeg;
};

struct AdaptiveConfig {
  bool enabled;
  int hitsToAdvance;
  int missesToDecrease;
  float stepMm;
  float decreaseStepMm;
  float minDistanceMm;
  float maxDistanceMm;
};

struct AdaptivePositionConfig {
  bool enabled;
  int hitsToAdvance;
  int missesToDecrease;
  float stepMm;
  float decreaseStepMm;
  float minDistanceMm;
  float maxDistanceMm;
};

struct FreeRewardConfig {
  bool enabled;
  int afterConsecutiveMisses;
  uint32_t delayAfterCueMs;
};

enum RewardMode {
  REWARD_CONTINGENT = 0,
  REWARD_AUTO_AFTER_DELAY = 1,
  REWARD_CONTINGENT_OR_AUTO = 2
};

enum ScheduleMode {
  SCHEDULE_BALANCED_BLOCK_CYCLES = 0,
  SCHEDULE_RANDOM_BLOCKS = 1
};

enum StopMode {
  STOP_END_OF_CURRENT_BLOCK = 0,
  STOP_END_OF_BALANCED_CYCLE = 1
};

enum StopPendingReason {
  STOP_PENDING_NONE = 0,
  STOP_PENDING_MAX_DURATION = 1,
  STOP_PENDING_TARGET_REACHED = 2
};

struct TaskConfig {
  bool sessionRunning;
  bool enforceNoLick;
  bool manualRewardAllowed;

  uint32_t rewardOpenMs;
  float estimatedRewardUL;
  float sessionWaterLimitUL;

  uint32_t cueFrequencyHz;
  uint32_t cueDurationMs;
  int cueVolumePct; // stored / logged; actual loudness should be set on the external amplifier

  uint32_t settleMs;
  uint32_t postRewardHoldMs;
  uint32_t preCueMinMs;
  uint32_t preCueMaxMs;
  uint32_t responseWindowMs;
  uint32_t itiMinMs;
  uint32_t itiJitterMs;
  uint32_t autoRewardDelayMs;
  bool autoHoldAfterMissEnabled;
  uint32_t autoHoldAfterMissThreshold;

  uint8_t blockSize;
  uint8_t blockSizeMin;
  uint8_t blockSizeMax;
  bool targetTrialsPerPositionEnabled;
  uint32_t targetTrialsPerPosition;
  bool maxDurationEnabled;
  uint32_t maxDurationMin;
  ScheduleMode scheduleMode;
  StopMode stopMode;
  RewardMode rewardMode;
  bool enabledPositions[NUM_POSITIONS];
};

struct LickConfig {
  bool activeLow;
  uint32_t debounceMs;
  bool debug;
  // accepted for GUI compatibility, not used for digital lick
  int thresholdCounts;
  int hysteresisCounts;
  int polarity;
  float baselineAlpha;
  uint32_t refractoryMs;
};

// ---------- Globals ----------
ZAxis axisX = {"x", 1, 2, 0.0f, 5249.34f};
ZAxis axisY = {"y", 1, 1, 0.0f, 5249.34f};
ZAxis axisZ = {"z", 1, 3, 0.0f, 5249.34f};

GeometryConfig geom = {{3.0f, 6.0f}, {0.0f, -45.0f, 45.0f}, 30.0f, 15.0f};
AdaptiveConfig adapt = {false, 2, 2, 0.5f, 0.5f, 3.0f, 8.0f};
bool adaptUsePerPosition = false;
AdaptivePositionConfig adaptPos[NUM_POSITIONS];
FreeRewardConfig freeRewardCfg = {true, 6, DEFAULT_AUTO_REWARD_DELAY_MS};
TaskConfig cfg = {
  false, true, true,
  25, 2.5f, 1000.0f,
  10000, 100, 50,
  DEFAULT_SETTLE_MS, DEFAULT_POST_REWARD_HOLD_MS,
  DEFAULT_PRE_CUE_MIN_MS, DEFAULT_PRE_CUE_MAX_MS,
  DEFAULT_RESPONSE_WINDOW_MS,
  DEFAULT_ITI_MIN_MS, DEFAULT_ITI_JITTER_MS,
  DEFAULT_AUTO_REWARD_DELAY_MS,
  false,
  3,
  5,
  5,
  5,
  true,
  DEFAULT_TARGET_TRIALS_PER_POSITION,
  false,
  DEFAULT_MAX_DURATION_MIN,
  SCHEDULE_BALANCED_BLOCK_CYCLES,
  STOP_END_OF_CURRENT_BLOCK,
  REWARD_CONTINGENT,
  {true, true, true, true, true, true}
};
LickConfig lickCfg = {true, 20, false, 0, 0, -1, 0.0f, 20};

Vec3 mouthOrigin = {0.0f, 0.0f, 0.0f};
Vec3 dockPosition = {0.0f, -10.0f, -5.0f};
Vec3 safePosition = {0.0f, 0.0f, -5.0f};

struct PersistedMotionConfig {
  uint32_t magic;
  uint16_t version;
  Vec3 mouthOrigin;
  Vec3 dockPosition;
  Vec3 safePosition;
  uint8_t axisXDeviceId;
  uint8_t axisYDeviceId;
  uint8_t axisZDeviceId;
  uint8_t axisXAxisNumber;
  uint8_t axisYAxisNumber;
  uint8_t axisZAxisNumber;
  float axisXUnitsPerMM;
  float axisYUnitsPerMM;
  float axisZUnitsPerMM;
};

static const uint32_t PERSISTED_MOTION_MAGIC = 0x5A425231UL; // "ZBR1"
static const uint16_t PERSISTED_MOTION_VERSION = 1;
static const int EEPROM_ADDR_PERSISTED_MOTION = 0;

PersistedMotionConfig lastSavedMotionConfig;

PersistedMotionConfig currentPersistedMotionConfig() {
  PersistedMotionConfig cfgPersist;
  cfgPersist.magic = PERSISTED_MOTION_MAGIC;
  cfgPersist.version = PERSISTED_MOTION_VERSION;
  cfgPersist.mouthOrigin = mouthOrigin;
  cfgPersist.dockPosition = dockPosition;
  cfgPersist.safePosition = safePosition;
  cfgPersist.axisXDeviceId = axisX.deviceId;
  cfgPersist.axisYDeviceId = axisY.deviceId;
  cfgPersist.axisZDeviceId = axisZ.deviceId;
  cfgPersist.axisXAxisNumber = axisX.axisNumber;
  cfgPersist.axisYAxisNumber = axisY.axisNumber;
  cfgPersist.axisZAxisNumber = axisZ.axisNumber;
  cfgPersist.axisXUnitsPerMM = axisX.unitsPerMM;
  cfgPersist.axisYUnitsPerMM = axisY.unitsPerMM;
  cfgPersist.axisZUnitsPerMM = axisZ.unitsPerMM;
  return cfgPersist;
}

bool persistedMotionConfigEquals(const PersistedMotionConfig &a, const PersistedMotionConfig &b) {
  const uint8_t *pa = reinterpret_cast<const uint8_t*>(&a);
  const uint8_t *pb = reinterpret_cast<const uint8_t*>(&b);
  for (unsigned int i = 0; i < sizeof(PersistedMotionConfig); i++) {
    if (pa[i] != pb[i]) return false;
  }
  return true;
}

void savePersistedMotionConfigIfChanged() {
  PersistedMotionConfig currentCfg = currentPersistedMotionConfig();
  if (persistedMotionConfigEquals(currentCfg, lastSavedMotionConfig)) return;
  EEPROM.put(EEPROM_ADDR_PERSISTED_MOTION, currentCfg);
  lastSavedMotionConfig = currentCfg;
}

void loadPersistedMotionConfig() {
  PersistedMotionConfig stored;
  EEPROM.get(EEPROM_ADDR_PERSISTED_MOTION, stored);
  if (stored.magic != PERSISTED_MOTION_MAGIC || stored.version != PERSISTED_MOTION_VERSION) {
    lastSavedMotionConfig = currentPersistedMotionConfig();
    return;
  }

  mouthOrigin = stored.mouthOrigin;
  dockPosition = stored.dockPosition;
  safePosition = stored.safePosition;
  axisX.deviceId = stored.axisXDeviceId;
  axisY.deviceId = stored.axisYDeviceId;
  axisZ.deviceId = stored.axisZDeviceId;
  axisX.axisNumber = stored.axisXAxisNumber;
  axisY.axisNumber = stored.axisYAxisNumber;
  axisZ.axisNumber = stored.axisZAxisNumber;
  axisX.unitsPerMM = stored.axisXUnitsPerMM;
  axisY.unitsPerMM = stored.axisYUnitsPerMM;
  axisZ.unitsPerMM = stored.axisZUnitsPerMM;
  lastSavedMotionConfig = stored;
}

Vec3 positions[NUM_POSITIONS];
float currentDistanceMm[NUM_POSITIONS];
uint8_t positionTierIndex[NUM_POSITIONS] = {0, 0, 0, 1, 1, 1};
uint8_t positionAzIndex[NUM_POSITIONS]   = {0, 1, 2, 0, 1, 2};

uint32_t totalTrials = 0;
uint32_t totalHits = 0;
uint32_t totalMisses = 0;
uint32_t totalFreeRewards = 0;
uint32_t totalRewards = 0;
uint32_t totalAutoRewards = 0;
uint32_t enlViolations = 0;
uint32_t syncPulseCount = 0;
float sessionWaterDeliveredUL = 0.0f;

uint32_t trialsPerPosition[NUM_POSITIONS];
uint32_t hitsPerPosition[NUM_POSITIONS];
uint32_t missesPerPosition[NUM_POSITIONS];
uint32_t freeRewardsPerPosition[NUM_POSITIONS];
uint32_t adaptiveHitCounterPerPosition[NUM_POSITIONS];
uint32_t adaptiveMissCounterPerPosition[NUM_POSITIONS];

int currentBlockPos = -1;
uint32_t trialsInCurrentBlock = 0;
uint8_t currentBlockSize = 0;
uint32_t blockNumber = 0;
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
uint32_t responseDeadlineMs = 0;
uint32_t itiEndAtMs = 0;
uint32_t cueEligibleAtMs = 0;
uint32_t stateStartMs = 0;

bool lickSensingEnabled = false;
bool lickCurrent = false;
volatile bool lickOnsetLatched = false;
// v38: lick-onset interrupt state (written by lickISR()).
volatile uint8_t  isrOnsetPending = 0;   // onsets captured by the ISR, drained in updateLick()
volatile uint32_t isrLastOnsetMs  = 0;   // ISR-side refractory anchor

enum RewardTriggerType {
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
uint8_t lickRawDigital = 0;

bool buttonPrev = true;
uint32_t buttonLastEdgeMs = 0;

uint32_t pulseEndTrial = 0;
uint32_t pulseEndCue = 0;
uint32_t pulseEndReward = 0;
uint32_t pulseEndSync = 0;
uint32_t pulseEndPosStrobe = 0;
uint32_t pulseEndTrialStop = 0;

bool taskRewardsHeld = false;
bool manualRewardsHeld = false;
bool autoRewardsHeld = false;
String serialRxBuffer = "";

uint32_t nextSyncAt = 0;
uint32_t syncPulseMinMs = SYNC_PULSE_MS_DEFAULT;
uint32_t syncPulseMaxMs = SYNC_PULSE_MS_DEFAULT;
uint32_t syncMinIntervalMs = SYNC_MIN_INTERVAL_MS_DEFAULT;
uint32_t syncMaxIntervalMs = SYNC_MAX_INTERVAL_MS_DEFAULT;

unsigned long lastLickDebugMs = 0;

// ---------- State machine ----------
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

const char* stateName(RunState s);
bool shouldHoldTaskReward(RewardTriggerType trigger);
bool deliverRewardForTrigger(RewardTriggerType trigger);

RunState runState = ST_IDLE;

inline void clearLatchedLick() {
  lickOnsetLatched = false;
}

// ---------- Helpers ----------
float mmToUnits(const ZAxis& a, float mm) {
  if (a.name && a.name[0] == 'z') {
    return -mm * a.unitsPerMM;
  }
  return mm * a.unitsPerMM;
}

long mmToUnitsLong(const ZAxis& a, float mm) {
  return (long)lround(mmToUnits(a, mm));
}

float unitsToMM(const ZAxis& a, long units) {
  if (a.unitsPerMM == 0.0f) return 0.0f;
  float mm = ((float)units) / a.unitsPerMM;
  if (a.name && a.name[0] == 'z') return -mm;
  return mm;
}

void recomputePosition(uint8_t idx) {
  uint8_t azIx = positionAzIndex[idx];
  float r = currentDistanceMm[idx];
  float azDeg = geom.azimuthDeg[azIx];

  float phi = azDeg * PI / 180.0f;
  float theta = geom.downwardAngleDeg * PI / 180.0f;
  float roll = geom.headRollDeg * PI / 180.0f;

  float x0 = r * cos(theta) * sin(phi);
  float y0 = -r * cos(theta) * cos(phi);
  float z0 = -r * sin(theta);

  // Rotate the full mouth-relative spout vector around the fore-aft axis.
  // Positive roll lowers mouse-right.
  float x1 = x0 * cos(roll) + z0 * sin(roll);
  float z1 = -x0 * sin(roll) + z0 * cos(roll);

  positions[idx].x = mouthOrigin.x + x1;
  positions[idx].y = mouthOrigin.y + y0;
  positions[idx].z = mouthOrigin.z + z1;
}

void recomputeAllGeneratedPositions() {
  for (int i = 0; i < NUM_POSITIONS; i++) recomputePosition(i);
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

void refreshRewardHoldState(bool emitCfg=false) {
  bool held = manualRewardsHeld || autoRewardsHeld;
  if (held == taskRewardsHeld) return;
  taskRewardsHeld = held;
  if (emitCfg) emitConfigKV("task.rewards_held", taskRewardsHeld);
}

void setManualRewardHold(bool held, bool emitCfg=true, bool emitEvt=true) {
  bool prev = manualRewardsHeld;
  manualRewardsHeld = held;
  refreshRewardHoldState(emitCfg);
  if (emitEvt && prev != held && cfg.sessionRunning) emitEvent(held ? "manual_reward_hold_on" : "manual_reward_hold_off");
}

void setAutoRewardHold(bool held, bool emitCfg=true, bool emitEvt=true) {
  bool prev = autoRewardsHeld;
  autoRewardsHeld = held;
  refreshRewardHoldState(emitCfg);
  if (emitEvt && prev != held && cfg.sessionRunning) emitEvent(held ? "auto_reward_hold_on" : "auto_reward_hold_off");
}

void clearAllRewardHolds(bool emitCfg=true, bool emitEvt=true) {
  setManualRewardHold(false, false, emitEvt);
  setAutoRewardHold(false, false, emitEvt);
  refreshRewardHoldState(emitCfg);
}

void updateAutoRewardHoldFromMissStreak(bool emitCfg=true) {
  (void)emitCfg;
  if (!cfg.autoHoldAfterMissEnabled) return;
  if (!(cfg.rewardMode == REWARD_AUTO_AFTER_DELAY || cfg.rewardMode == REWARD_CONTINGENT_OR_AUTO)) return;
  if (cfg.autoHoldAfterMissThreshold < 1) return;
  if (consecutiveMisses >= (int)cfg.autoHoldAfterMissThreshold) {
    autoRewardsHeld = true;
    bool rawPressed = (digitalRead(PIN_LICK_IN) == LOW);
    bool lickLineActive = lickCfg.activeLow ? rawPressed : !rawPressed;
    if (lickLineActive) autoRewardsHeld = false;
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

void resetSessionStats() {
  pendingRewardTrigger = REWARD_TRIGGER_NONE;
  clearAllRewardHolds(false, false);
  totalTrials = 0;
  totalHits = 0;
  totalMisses = 0;
  totalFreeRewards = 0;
  totalRewards = 0;
  totalAutoRewards = 0;
  enlViolations = 0;
  syncPulseCount = 0;
  sessionWaterDeliveredUL = 0.0f;
  consecutiveMisses = 0;
  currentBlockPos = -1;
  trialsInCurrentBlock = 0;
  currentBlockSize = 0;
  blockNumber = 0;
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
  for (int i = 0; i < NUM_POSITIONS; i++) {
    trialsPerPosition[i] = 0;
    hitsPerPosition[i] = 0;
    missesPerPosition[i] = 0;
    freeRewardsPerPosition[i] = 0;
    adaptiveHitCounterPerPosition[i] = 0;
    adaptiveMissCounterPerPosition[i] = 0;
  }
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

const char* rewardModeName() {
  switch (cfg.rewardMode) {
    case REWARD_CONTINGENT: return "contingent";
    case REWARD_AUTO_AFTER_DELAY: return "auto_after_delay";
    case REWARD_CONTINGENT_OR_AUTO: return "contingent_or_auto_after_delay";
    default: return "contingent";
  }
}

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

// ---------- Protocol emit ----------
void emitInfoReady() {
  Serial.print("INFO kind=ready protocol=2 backend=mega_zaber ");
  Serial.print("x_dev="); Serial.print(axisX.deviceId); Serial.print(" x_axis="); Serial.print(axisX.axisNumber);
  Serial.print(" y_dev="); Serial.print(axisY.deviceId); Serial.print(" y_axis="); Serial.print(axisY.axisNumber);
  Serial.print(" z_dev="); Serial.print(axisZ.deviceId); Serial.print(" z_axis="); Serial.println(axisZ.axisNumber);
}

void emitOK(const String& cmd, const String& extra = "") {
  Serial.print("OK cmd=");
  Serial.print(cmd);
  if (extra.length() > 0) {
    Serial.print(" ");
    Serial.print(extra);
  }
  Serial.println();
}

void emitErr(const String& cmd, const String& code, const String& detail = "") {
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

void emitConfigKV(const String& key, const String& value) {
  Serial.print("CFG key=");
  Serial.print(key);
  Serial.print(" value=");
  Serial.println(value);
}
void emitConfigKV(const String& key, bool value) { emitConfigKV(key, value ? "1" : "0"); }
void emitConfigKV(const String& key, int value) { emitConfigKV(key, String(value)); }
void emitConfigKV(const String& key, uint32_t value) { emitConfigKV(key, String(value)); }
void emitConfigKV(const String& key, float value, int digits=3) { emitConfigKV(key, String(value, digits)); }

void emitStatus() {
  zRefreshAllAxisPosMM();
  uint32_t now = millis();
  uint32_t cueWaitRemaining = 0;
  uint32_t responseRemaining = 0;
  if (runState == ST_PRE_CUE && cueEligibleAtMs > now) cueWaitRemaining = cueEligibleAtMs - now;
  if (runState == ST_WAIT_FOR_LICK && responseDeadlineMs > now) responseRemaining = responseDeadlineMs - now;
  int enabledCount = 0;
  for (int i = 0; i < NUM_POSITIONS; i++) if (cfg.enabledPositions[i]) enabledCount++;

  Serial.print("STAT kind=status");
  Serial.print(" run="); Serial.print(cfg.sessionRunning ? 1 : 0);
  Serial.print(" state="); Serial.print(stateName(runState));
  Serial.print(" reward_mode="); Serial.print(rewardModeName());
  Serial.print(" x_mm="); Serial.print(axisX.posMM, 3);
  Serial.print(" y_mm="); Serial.print(axisY.posMM, 3);
  Serial.print(" z_mm="); Serial.print(axisZ.posMM, 3);
  Serial.print(" current_pos="); Serial.print(currentTrialPos);
  Serial.print(" current_pos_dist_mm=");
  if (currentTrialPos >= 0) Serial.print(currentDistanceMm[currentTrialPos], 3); else Serial.print("-1");
  Serial.print(" current_pos_trials=");
  if (currentTrialPos >= 0) Serial.print(trialsPerPosition[currentTrialPos]); else Serial.print("-1");
  Serial.print(" current_pos_target_remaining=");
  if (currentTrialPos >= 0 && cfg.targetTrialsPerPositionEnabled) {
    uint32_t remainingForPos = 0;
    if (trialsPerPosition[currentTrialPos] < cfg.targetTrialsPerPosition) remainingForPos = cfg.targetTrialsPerPosition - trialsPerPosition[currentTrialPos];
    Serial.print(remainingForPos);
  } else {
    Serial.print("-1");
  }
  Serial.print(" block_pos="); Serial.print(currentBlockPos);
  Serial.print(" block_trial="); Serial.print(trialsInCurrentBlock);
  Serial.print(" block_number="); Serial.print(blockNumber);
  Serial.print(" current_block_size="); Serial.print(currentBlockSize);
  Serial.print(" block_size="); Serial.print(currentBlockSize);
  Serial.print(" block_size_min="); Serial.print(cfg.blockSizeMin);
  Serial.print(" block_size_max="); Serial.print(cfg.blockSizeMax);
  Serial.print(" scheduling_mode="); Serial.print(scheduleModeName());
  Serial.print(" stop_mode="); Serial.print(stopModeName());
  Serial.print(" target_trials_per_position_enabled="); Serial.print(cfg.targetTrialsPerPositionEnabled ? 1 : 0);
  Serial.print(" target_trials_per_position="); Serial.print(cfg.targetTrialsPerPosition);
  Serial.print(" max_duration_enabled="); Serial.print(cfg.maxDurationEnabled ? 1 : 0);
  Serial.print(" max_duration_min="); Serial.print(cfg.maxDurationMin);
  Serial.print(" stop_pending="); Serial.print(stopPending ? 1 : 0);
  Serial.print(" total_trials="); Serial.print(totalTrials);
  Serial.print(" hits="); Serial.print(totalHits);
  Serial.print(" misses="); Serial.print(totalMisses);
  Serial.print(" free_rewards="); Serial.print(totalFreeRewards);
  Serial.print(" auto_rewards="); Serial.print(totalAutoRewards);
  Serial.print(" total_rewards="); Serial.print(totalRewards);
  Serial.print(" water_ul="); Serial.print(sessionWaterDeliveredUL, 2);
  Serial.print(" water_limit_ul="); Serial.print(cfg.sessionWaterLimitUL, 2);
  Serial.print(" reward_ul="); Serial.print(cfg.estimatedRewardUL, 2);
  Serial.print(" rewards_held="); Serial.print(taskRewardsHeld ? 1 : 0);
  Serial.print(" auto_hold_after_miss_enabled="); Serial.print(cfg.autoHoldAfterMissEnabled ? 1 : 0);
  Serial.print(" auto_hold_after_miss_threshold="); Serial.print(cfg.autoHoldAfterMissThreshold);
  Serial.print(" manual_reward_hold_active="); Serial.print(manualRewardsHeld ? 1 : 0);
  Serial.print(" auto_reward_hold_active="); Serial.print(autoRewardsHeld ? 1 : 0);
  Serial.print(" miss_streak="); Serial.print(consecutiveMisses);
  Serial.print(" enabled_positions="); Serial.print(enabledCount);
  Serial.print(" sync_count="); Serial.print(syncPulseCount);
  Serial.print(" sync_state="); Serial.print((pulseEndSync && now < pulseEndSync) ? 1 : 0);
  Serial.print(" enl_violations="); Serial.print(enlViolations);
  Serial.print(" free_reward_trial="); Serial.print(freeRewardThisTrial ? 1 : 0);
  Serial.print(" free_reward_delivered="); Serial.print(freeRewardDeliveredThisTrial ? 1 : 0);
  Serial.print(" cue_wait_remaining_ms="); Serial.print(cueWaitRemaining);
  Serial.print(" response_remaining_ms="); Serial.print(responseRemaining);
  Serial.print(" lick="); Serial.print(lickCurrent ? 1 : 0);
  Serial.print(" lick_raw="); Serial.println(lickRawDigital);
}

void emitPositions() {
  for (int i = 0; i < NUM_POSITIONS; i++) {
    Serial.print("POS idx="); Serial.print(i);
    Serial.print(" x_mm="); Serial.print(positions[i].x, 3);
    Serial.print(" y_mm="); Serial.print(positions[i].y, 3);
    Serial.print(" z_mm="); Serial.print(positions[i].z, 3);
    Serial.print(" enabled="); Serial.print(cfg.enabledPositions[i] ? 1 : 0);
    Serial.print(" dist_mm="); Serial.print(currentDistanceMm[i], 3);
    Serial.print(" az_deg="); Serial.print(geom.azimuthDeg[positionAzIndex[i]], 3);
    Serial.print(" down_deg="); Serial.println(geom.downwardAngleDeg, 3);
  }
}

void emitStats() {
  Serial.print("STAT kind=summary");
  Serial.print(" total_trials="); Serial.print(totalTrials);
  Serial.print(" hits="); Serial.print(totalHits);
  Serial.print(" misses="); Serial.print(totalMisses);
  Serial.print(" free_rewards="); Serial.print(totalFreeRewards);
  Serial.print(" auto_rewards="); Serial.print(totalAutoRewards);
  Serial.print(" total_rewards="); Serial.print(totalRewards);
  Serial.print(" water_ul="); Serial.print(sessionWaterDeliveredUL, 2);
  Serial.print(" rewards_held="); Serial.print(taskRewardsHeld ? 1 : 0);
  Serial.print(" auto_hold_after_miss_enabled="); Serial.print(cfg.autoHoldAfterMissEnabled ? 1 : 0);
  Serial.print(" auto_hold_after_miss_threshold="); Serial.print(cfg.autoHoldAfterMissThreshold);
  Serial.print(" manual_reward_hold_active="); Serial.print(manualRewardsHeld ? 1 : 0);
  Serial.print(" auto_reward_hold_active="); Serial.print(autoRewardsHeld ? 1 : 0);
  Serial.print(" miss_streak="); Serial.print(consecutiveMisses);
  Serial.print(" sync_count="); Serial.print(syncPulseCount);
  Serial.print(" enl_violations="); Serial.print(enlViolations);
  Serial.print(" block_number="); Serial.println(blockNumber);

  for (int i = 0; i < NUM_POSITIONS; i++) {
    Serial.print("STAT kind=pos idx="); Serial.print(i);
    Serial.print(" enabled="); Serial.print(cfg.enabledPositions[i] ? 1 : 0);
    Serial.print(" tier="); Serial.print(positionTierIndex[i]);
    Serial.print(" az_index="); Serial.print(positionAzIndex[i]);
    Serial.print(" dist_mm="); Serial.print(currentDistanceMm[i], 3);
    Serial.print(" trials="); Serial.print(trialsPerPosition[i]);
    Serial.print(" hits="); Serial.print(hitsPerPosition[i]);
    Serial.print(" misses="); Serial.print(missesPerPosition[i]);
    Serial.print(" free_rewards="); Serial.print(freeRewardsPerPosition[i]);
    Serial.print(" adaptive_hit_counter="); Serial.print(adaptiveHitCounterPerPosition[i]);
    Serial.print(" adaptive_miss_counter="); Serial.println(adaptiveMissCounterPerPosition[i]);
  }
}

void emitConfig() {
  emitConfigKV("backend.type", "mega_zaber");
  emitConfigKV("task.reward_ms", cfg.rewardOpenMs);
  emitConfigKV("task.reward_ul", cfg.estimatedRewardUL, 3);
  emitConfigKV("task.water_limit_ul", cfg.sessionWaterLimitUL, 3);
  emitConfigKV("task.enforce_no_lick", cfg.enforceNoLick);
  emitConfigKV("task.manual_reward_allowed", cfg.manualRewardAllowed);
  emitConfigKV("task.settle_ms", cfg.settleMs);
  emitConfigKV("task.post_reward_hold_ms", cfg.postRewardHoldMs);
  emitConfigKV("task.pre_cue_min_ms", cfg.preCueMinMs);
  emitConfigKV("task.pre_cue_max_ms", cfg.preCueMaxMs);
  emitConfigKV("task.response_window_ms", cfg.responseWindowMs);
  emitConfigKV("task.iti_min_ms", cfg.itiMinMs);
  emitConfigKV("task.iti_jitter_ms", cfg.itiJitterMs);
  emitConfigKV("task.reward_mode", rewardModeName());
  emitConfigKV("task.auto_reward_delay_ms", cfg.autoRewardDelayMs);
  emitConfigKV("task.auto_hold_after_miss_enabled", cfg.autoHoldAfterMissEnabled);
  emitConfigKV("task.auto_hold_after_miss_threshold", cfg.autoHoldAfterMissThreshold);
  emitConfigKV("task.rewards_held", taskRewardsHeld);
  emitConfigKV("task.block_size", cfg.blockSize);
  emitConfigKV("task.block_size_min", cfg.blockSizeMin);
  emitConfigKV("task.block_size_max", cfg.blockSizeMax);
  emitConfigKV("task.target_trials_per_position_enabled", cfg.targetTrialsPerPositionEnabled);
  emitConfigKV("task.target_trials_per_position", cfg.targetTrialsPerPosition);
  emitConfigKV("task.max_duration_enabled", cfg.maxDurationEnabled);
  emitConfigKV("task.max_duration_min", cfg.maxDurationMin);
  emitConfigKV("task.scheduling_mode", scheduleModeName());
  emitConfigKV("task.stop_mode", stopModeName());
  for (int i = 0; i < NUM_POSITIONS; i++) emitConfigKV(String("task.enable_pos") + i, cfg.enabledPositions[i]);

  emitConfigKV("cue.frequency_hz", cfg.cueFrequencyHz);
  emitConfigKV("cue.duration_ms", cfg.cueDurationMs);
  emitConfigKV("cue.volume_pct", cfg.cueVolumePct);
  emitConfigKV("cue.ttl_pin", (int)PIN_CUE_TTL);
  emitConfigKV("cue.audio_pin", (int)PIN_CUE_AUDIO);

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

  emitConfigKV("lick.active_low", lickCfg.activeLow);
  emitConfigKV("lick.debounce_ms", lickCfg.debounceMs);
  emitConfigKV("lick.debug", lickCfg.debug);
  emitConfigKV("lick.threshold_counts", lickCfg.thresholdCounts);
  emitConfigKV("lick.hysteresis_counts", lickCfg.hysteresisCounts);
  emitConfigKV("lick.polarity", lickCfg.polarity);
  emitConfigKV("lick.baseline_alpha", lickCfg.baselineAlpha, 5);
  emitConfigKV("lick.refractory_ms", lickCfg.refractoryMs);

  emitConfigKV("zaber.axis.x.device_id", axisX.deviceId);
  emitConfigKV("zaber.axis.y.device_id", axisY.deviceId);
  emitConfigKV("zaber.axis.z.device_id", axisZ.deviceId);
  emitConfigKV("zaber.axis.x.axis_number", (int)axisX.axisNumber);
  emitConfigKV("zaber.axis.y.axis_number", (int)axisY.axisNumber);
  emitConfigKV("zaber.axis.z.axis_number", (int)axisZ.axisNumber);
  emitConfigKV("zaber.axis.x.units_per_mm", axisX.unitsPerMM, 5);
  emitConfigKV("zaber.axis.y.units_per_mm", axisY.unitsPerMM, 5);
  emitConfigKV("zaber.axis.z.units_per_mm", axisZ.unitsPerMM, 5);

  emitConfigKV("sync.min_pulse_ms", syncPulseMinMs);
  emitConfigKV("sync.max_pulse_ms", syncPulseMaxMs);
  emitConfigKV("sync.min_interval_ms", syncMinIntervalMs);
  emitConfigKV("sync.max_interval_ms", syncMaxIntervalMs);
}


void emitZaberConfig() {
  emitConfigKV("backend.type", "mega_zaber");
  emitConfigKV("zaber.axis.x.device_id", axisX.deviceId);
  emitConfigKV("zaber.axis.y.device_id", axisY.deviceId);
  emitConfigKV("zaber.axis.z.device_id", axisZ.deviceId);
  emitConfigKV("zaber.axis.x.axis_number", axisX.axisNumber);
  emitConfigKV("zaber.axis.y.axis_number", axisY.axisNumber);
  emitConfigKV("zaber.axis.z.axis_number", axisZ.axisNumber);
  emitConfigKV("zaber.axis.x.units_per_mm", axisX.unitsPerMM, 5);
  emitConfigKV("zaber.axis.y.units_per_mm", axisY.unitsPerMM, 5);
  emitConfigKV("zaber.axis.z.units_per_mm", axisZ.unitsPerMM, 5);
}
// ---------- TTL ----------
void pulsePin(uint8_t pin, uint32_t durMs) {
  digitalWrite(pin, HIGH);
  uint32_t tEnd = millis() + durMs;
  if (pin == PIN_TRIAL_START) pulseEndTrial = tEnd;
  else if (pin == PIN_CUE_TTL) pulseEndCue = tEnd;
  else if (pin == PIN_TTL_REWARD) pulseEndReward = tEnd;
  else if (pin == PIN_SYNC) pulseEndSync = tEnd;
  else if (pin == PIN_TTL_POS_STB) pulseEndPosStrobe = tEnd;
else if (pin == PIN_TTL_TRIAL_STOP) pulseEndTrialStop = tEnd;
}

void updateTTLPulses() {
  uint32_t now = millis();
  if (pulseEndTrial && now >= pulseEndTrial) { digitalWrite(PIN_TRIAL_START, LOW); pulseEndTrial = 0; }
  if (pulseEndCue && now >= pulseEndCue) { digitalWrite(PIN_CUE_TTL, LOW); pulseEndCue = 0; }
  if (pulseEndReward && now >= pulseEndReward) { digitalWrite(PIN_TTL_REWARD, LOW); pulseEndReward = 0; }
  if (pulseEndSync && now >= pulseEndSync) { digitalWrite(PIN_SYNC, LOW); pulseEndSync = 0; }
  if (pulseEndPosStrobe && now >= pulseEndPosStrobe) { digitalWrite(PIN_TTL_POS_STB, LOW); pulseEndPosStrobe = 0; }
if (pulseEndTrialStop && now >= pulseEndTrialStop) { digitalWrite(PIN_TTL_TRIAL_STOP, LOW); pulseEndTrialStop = 0; }
}

void emitPositionCode(uint8_t idx) {
  digitalWrite(PIN_TTL_POS0, (idx & 0x01) ? HIGH : LOW);
  digitalWrite(PIN_TTL_POS1, (idx & 0x02) ? HIGH : LOW);
  digitalWrite(PIN_TTL_POS2, (idx & 0x04) ? HIGH : LOW);
  pulsePin(PIN_TTL_POS_STB, POSITION_STROBE_MS);
}

void updateSync() {
  if (!cfg.sessionRunning) return;
  uint32_t now = millis();
  if (now >= nextSyncAt) {
    uint32_t pulseMin = syncPulseMinMs;
    uint32_t pulseMax = syncPulseMaxMs;
    if (pulseMax < pulseMin) { uint32_t tmp = pulseMin; pulseMin = pulseMax; pulseMax = tmp; }
    uint32_t pulseMs = (pulseMax > pulseMin) ? random(pulseMin, pulseMax + 1) : pulseMin;
    pulsePin(PIN_SYNC, pulseMs);
    syncPulseCount++;
    uint32_t intMin = syncMinIntervalMs;
    uint32_t intMax = syncMaxIntervalMs;
    if (intMax < intMin) { uint32_t tmp = intMin; intMin = intMax; intMax = tmp; }
    uint32_t nextInterval = (intMax > intMin) ? random(intMin, intMax + 1) : intMin;
    nextSyncAt = now + nextInterval;
    Serial.print("EVT name=sync t_ms=");
    Serial.print(now);
    Serial.print(" count=");
    Serial.print(syncPulseCount);
    Serial.print(" ttl_ms=");
    Serial.print(pulseMs);
    Serial.print(" next_interval_ms=");
    Serial.println(nextInterval);
  }
}

// ---------- Forward declarations for functions used before definition ----------
void updateLick();
void updateButton();
void updateTTLPulses();
void updateSync();
bool handleSerialDuringMotionWait();
void stopAllStagesAndRefresh();

// Set by any stop path (STOP command or the shield pushbutton). Every multi-axis
// move checks it so a stop unwinds the whole sequence rather than only the
// segment that happens to be running. Cleared by START / MOVE / HOME / RESETSESSION.
volatile bool abortMotion = false;

// True only while zWaitIdle()/zWaitIdleByAddress() owns the Zaber connection.
//
// The Zaber link is request/response: the wait loop issues "get pos" and blocks for
// that device's reply. Issuing MORE commands from inside the loop -- which is what a
// STOP arriving over serial during a move would do, since handleSerialDuringMotionWait()
// runs from serviceDuringZaberWait() -- interleaves six extra commands with a reply in
// flight, and from then on every reply is matched to the wrong request. So a stop
// requested during a wait only SETS A FLAG; the move path issues the actual stop once
// the wait has returned and the connection is free again.
volatile bool inZaberWait = false;
volatile bool stagesStopPending = false;

void requestStopStages();
void closeTrialAndCueGates();

// ---------- Cue / reward ----------
void playCue() {
  if (cfg.cueVolumePct <= 0) return; // mute; on Mega/Zaber, actual loudness is set with the external amplifier knob
  tone(PIN_CUE_AUDIO, cfg.cueFrequencyHz, cfg.cueDurationMs);
  // Cue TTL is a GATE spanning the whole tone, not a 5 ms marker. Driven here
  // rather than at the call sites so it tracks cue.duration_ms automatically and
  // a muted cue produces no gate. updateTTLPulses() closes it.
  pulsePin(PIN_CUE_TTL, cfg.cueDurationMs);
}

void cueOnly() {
  playCue();   // cue gate is driven inside playCue()
  emitEvent("cue_only");
}

// Force every level-held output back to its inactive state. Called on STOP so an
// aborted trial can never strand the cue gate HIGH: since v37 the cue TTL spans the
// tone (pulsePin with cue.duration_ms), so without this a stop mid-cue leaves the DAQ
// cue line high for the remainder of that duration. Ported from the Teensy rigs, which
// have guarded this since v37. NB the Zaber's trial TTLs are short pulses, not gates,
// so they are left to updateTTLPulses() -- clearing them here would truncate a live pulse.
void closeTrialAndCueGates() {
  noTone(PIN_CUE_AUDIO);
  digitalWrite(PIN_CUE_TTL, LOW);
  pulseEndCue = 0;
}

void openRewardValve(uint32_t durMs, bool bypassHold=false) {
  if (!taskRewardsHeld || bypassHold) {
    digitalWrite(PIN_SOLENOID, HIGH);
  }
  pulsePin(PIN_TTL_REWARD, TTL_PULSE_MS);
  uint32_t t0 = millis();
  while (millis() - t0 < durMs) {
    handleSerialDuringMotionWait();
    updateTTLPulses();
    updateLick();
    updateButton();
    updateSync();
  }
  digitalWrite(PIN_SOLENOID, LOW);
}

// ---------- Zaber motion ----------
void updateLick();
void updateTTLPulses();
void updateSync();
bool handleSerialDuringMotionWait();
void serviceDuringZaberWait();
Result zMoveAbsResult(const ZAxis& axis, long posUnits) {
  return connection.genericCommand(Command("move abs", posUnits), axis.deviceId, axis.axisNumber);
}

Result zMoveRelResult(const ZAxis& axis, long deltaUnits) {
  return connection.genericCommand(Command("move rel", deltaUnits), axis.deviceId, axis.axisNumber);
}

Result zHomeResult(const ZAxis& axis) {
  return connection.genericCommand("home", axis.deviceId, axis.axisNumber);
}

bool zSendMoveAbs(const ZAxis& axis, long posUnits) {
  Result r = zMoveAbsResult(axis, posUnits);
  return r.getError() == Result::OK;
}

bool zSendMoveRel(const ZAxis& axis, long deltaUnits) {
  Result r = zMoveRelResult(axis, deltaUnits);
  return r.getError() == Result::OK;
}

bool zSendHome(const ZAxis& axis) {
  Result r = zHomeResult(axis);
  return r.getError() == Result::OK;
}

bool zGetPosUnits(const ZAxis& axis, long &outPos) {
  Result r = connection.genericCommand("get pos", axis.deviceId, axis.axisNumber);
  if (r.getError() != Result::OK) return false;
  outPos = r.getDataInt();
  return true;
}

bool zRefreshAxisPosMM(ZAxis &axis) {
  long posUnits = 0;
  if (!zGetPosUnits(axis, posUnits)) return false;
  axis.posMM = unitsToMM(axis, posUnits);
  return true;
}

void zRefreshAllAxisPosMM() {
  zRefreshAxisPosMM(axisX);
  zRefreshAxisPosMM(axisY);
  zRefreshAxisPosMM(axisZ);
}

// A stop must reach the STAGES, not just the state machine. In v36 the STOP
// handler only set runState = ST_IDLE, while zWaitIdle() kept polling until the
// axis finished travelling to its commanded target: the stage ran the move to
// completion after the operator had already pressed stop.
//
// Issuing "stop" makes the controller decelerate and report IDLE, which lets
// zWaitIdle() return promptly, and then we read true positions back off the
// encoders. Closed-loop means no estimation and no manual re-referencing.
// Ask for the stages to stop. Safe to call from anywhere, including from inside a
// Zaber wait -- see inZaberWait above.
void requestStopStages() {
  abortMotion = true;
  closeTrialAndCueGates();   // immediate: a digitalWrite, safe even inside a Zaber wait
  if (inZaberWait) {
    stagesStopPending = true;   // deferred: moveAxisAbsMM issues it once the wait returns
    return;
  }
  stopAllStagesAndRefresh();
}

void stopAllStagesAndRefresh() {
  abortMotion = true;
  stagesStopPending = false;
  connection.genericCommand("stop", axisX.deviceId, axisX.axisNumber);
  connection.genericCommand("stop", axisY.deviceId, axisY.axisNumber);
  connection.genericCommand("stop", axisZ.deviceId, axisZ.axisNumber);
  zRefreshAllAxisPosMM();
  emitEventDetail("stages_stopped",
                  String("x_mm=") + String(axisX.posMM, 3) +
                  " y_mm=" + String(axisY.posMM, 3) +
                  " z_mm=" + String(axisZ.posMM, 3));
}

void serviceDuringZaberWait() {
  // Keep host control responsive for safe commands while blocking motion completes.
  handleSerialDuringMotionWait();
  updateTTLPulses();
  updateLick();
  updateSync();
  // updateButton() is deliberately NOT called here: the physical button must not be
  // able to halt a running task. It is disabled outright anyway
  // (ENABLE_STARTSTOP_BUTTON), so this is belt-and-braces -- re-enabling the button
  // should not silently make it live mid-move as well.
}

bool zWaitIdle(const ZAxis& axis) {
  inZaberWait = true;
  while (true) {
    Result r = connection.genericCommand("get pos", axis.deviceId, axis.axisNumber);
    if (r.getError() != Result::OK) { inZaberWait = false; return false; }
    if (r.getStatus() == Result::IDLE) { inZaberWait = false; return true; }
    serviceDuringZaberWait();
    if (abortMotion) { inZaberWait = false; return true; }   // caller stops the stages
    delay(1);
  }
  inZaberWait = false;
  return true;
}

bool zWaitIdleByAddress(uint8_t deviceId, uint8_t axisNumber) {
  inZaberWait = true;
  while (true) {
    Result r = connection.genericCommand("get pos", deviceId, axisNumber);
    if (r.getError() != Result::OK) { inZaberWait = false; return false; }
    if (r.getStatus() == Result::IDLE) { inZaberWait = false; return true; }
    serviceDuringZaberWait();
    if (abortMotion) { inZaberWait = false; return true; }   // caller stops the stages
    delay(1);
  }
  inZaberWait = false;
  return true;
}

bool zSendMoveRelByAddress(uint8_t deviceId, uint8_t axisNumber, long deltaUnits) {
  Result r = connection.genericCommand(Command("move rel", deltaUnits), deviceId, axisNumber);
  return r.getError() == Result::OK;
}

bool zSendHomeByAddress(uint8_t deviceId, uint8_t axisNumber) {
  Result r = connection.genericCommand("home", deviceId, axisNumber);
  return r.getError() == Result::OK;
}

bool moveAxisAbsMM(ZAxis &axis, float targetMM) {
  if (abortMotion) return false;
  long units = mmToUnitsLong(axis, targetMM);
  if (!zSendMoveAbs(axis, units)) return false;
  if (!zWaitIdle(axis)) return false;

  if (abortMotion) {
    // Aborted part-way. The wait returned without issuing anything, so the connection
    // is ours again: issue the deferred stop now (no-op if a stop already ran).
    if (stagesStopPending) stopAllStagesAndRefresh();
    // Unlike the open-loop SMC02 rigs we do not have to estimate anything: the Zaber is
    // closed-loop, so read the true position back off the encoder and report exactly
    // where the stage actually stopped.
    float requested = targetMM;
    if (zRefreshAxisPosMM(axis)) {
      emitEventDetail("move_aborted",
                      String("axis=") + axis.name +
                      " requested_mm=" + String(requested, 3) +
                      " actual_mm=" + String(axis.posMM, 3));
    } else {
      emitEventDetail("move_aborted",
                      String("axis=") + axis.name +
                      " requested_mm=" + String(requested, 3) +
                      " actual_mm=unknown_get_pos_failed");
    }
    return false;
  }

  axis.posMM = targetMM;
  return true;
}

bool moveAxisRelMM(ZAxis &axis, float deltaMM) {
  long units = mmToUnitsLong(axis, deltaMM);
  if (!zSendMoveRel(axis, units)) return false;
  if (!zWaitIdle(axis)) return false;
  axis.posMM += deltaMM;
  return true;
}

bool homeAllAxes() {
  Result rx = zHomeResult(axisX);
  if (rx.getError() != Result::OK) {
    emitErr("home", "failed", String("axis=X device=") + axisX.deviceId + " axis_number=" + axisX.axisNumber + " zaber_error=" + rx.getErrorString());
    return false;
  }
  zWaitIdle(axisX);

  Result ry = zHomeResult(axisY);
  if (ry.getError() != Result::OK) {
    emitErr("home", "failed", String("axis=Y device=") + axisY.deviceId + " axis_number=" + axisY.axisNumber + " zaber_error=" + ry.getErrorString());
    return false;
  }
  zWaitIdle(axisY);

  Result rz = zHomeResult(axisZ);
  if (rz.getError() != Result::OK) {
    emitErr("home", "failed", String("axis=Z device=") + axisZ.deviceId + " axis_number=" + axisZ.axisNumber + " zaber_error=" + rz.getErrorString());
    return false;
  }
  zWaitIdle(axisZ);

  axisX.posMM = 0.0f; axisY.posMM = 0.0f; axisZ.posMM = 0.0f;
  emitOK("home", String("X=dev") + axisX.deviceId + "/ax" + axisX.axisNumber +
                 " Y=dev" + axisY.deviceId + "/ax" + axisY.axisNumber +
                 " Z=dev" + axisZ.deviceId + "/ax" + axisZ.axisNumber);
  return true;
}

bool moveToPositionSafe(const Vec3& target) {
  // Each moveAxisAbsMM returns false on abort, so a stop part-way through
  // unwinds the remaining segments instead of driving on to the next axis.
  if (abortMotion) return false;
  if (!moveAxisAbsMM(axisZ, safePosition.z)) return false;
  if (!moveAxisAbsMM(axisX, target.x)) return false;
  if (!moveAxisAbsMM(axisY, target.y)) return false;
  if (!moveAxisAbsMM(axisZ, target.z)) return false;
  return true;
}

bool moveToNamedPosition(uint8_t idx) {
  if (idx >= NUM_POSITIONS) return false;
  currentTrialPos = idx;
  bool ok = moveToPositionSafe(positions[idx]);
  if (ok) {
    emitPositionCode(idx);
    emitEventDetail("position", String("idx=") + idx);
  }
  return ok;
}

// ---------- Lick / button ----------
// v38: a pin-change interrupt latches every lick ONSET the instant it happens, so no lick is
// lost to loop/serial/Zaber latency or the v37 debounce lockout (which dropped brief/fast
// contacts -> ~16% of real ENL licks the DAQ recorded never reset the ENL). Serial is unsafe in
// an ISR, so the ISR only counts onsets; updateLick() (main loop) drains them to emit lick_on and
// arm the ENL latch. Attached on CHANGE + level test so it honours lick.active_low at runtime.
void lickISR() {
  bool pressed = (digitalRead(PIN_LICK_IN) == LOW);
  if (!(lickCfg.activeLow ? pressed : !pressed)) return;   // onset edge only
  uint32_t now = millis();
  if (now - isrLastOnsetMs >= (uint32_t)lickCfg.refractoryMs) {
    isrLastOnsetMs = now;
    if (isrOnsetPending < 255) isrOnsetPending++;
  }
}

void updateLick() {
  bool rawPressed = (digitalRead(PIN_LICK_IN) == LOW);
  bool currentState = lickCfg.activeLow ? rawPressed : !rawPressed;
  lickRawDigital = currentState ? 1 : 0;

  uint32_t now = millis();

  // v38: ONSET from the ISR (never missed) -- drain every captured onset.
  uint8_t pending;
  noInterrupts(); pending = isrOnsetPending; isrOnsetPending = 0; interrupts();
  while (pending) {
    pending--;
    if (!lickCurrent) { lickCurrent = true; lastLickChangeMs = now; }
    emitEvent("lick_on");
    if (autoRewardsHeld) {
      autoRewardsHeld = false;
      refreshRewardHoldState(false);
    }
    if (lickSensingEnabled) {
      lickOnsetLatched = true;
      lastLickOnsetMs = now;
    }
  }

  // OFFSET (release) from the poll, debounced.
  if (!currentState && lickCurrent && (now - lastLickChangeMs >= lickCfg.debounceMs)) {
    lickCurrent = false;
    lastLickChangeMs = now;
    emitEvent("lick_off");
  }

  if (lickCfg.debug && now - lastLickDebugMs >= 50) {
    lastLickDebugMs = now;
    Serial.print("STAT kind=lick raw=");
    Serial.print(lickRawDigital);
    Serial.print(" baseline=0 threshold=0.5 lick=");
    Serial.println(lickCurrent ? 1 : 0);
  }
}

bool consumeLickOnset() {
  if (lickOnsetLatched) {
    lickOnsetLatched = false;
    return true;
  }
  return false;
}

void updateButton() {
  if (!ENABLE_STARTSTOP_BUTTON) return;   // button does nothing; host-only start/stop
  bool level = digitalRead(PIN_STARTSTOP_BUTTON); // HIGH unpressed due shield pullup
  uint32_t now = millis();
  if (level != buttonPrev && now - buttonLastEdgeMs >= BUTTON_DEBOUNCE_MS) {
    buttonLastEdgeMs = now;
    buttonPrev = level;
    if (level == LOW) {
      if (!cfg.sessionRunning) {
        abortMotion = false;
        resetSessionStats();
        resetAdaptiveDistances();
        recomputeAllGeneratedPositions();
        chooseNextBlockPosition();
        cfg.sessionRunning = true;
        nextSyncAt = millis() + random(syncMinIntervalMs, syncMaxIntervalMs + 1);
        runState = ST_MOVE_TO_TARGET;
        emitEvent("button_start");
      } else {
        requestStopStages();   // between moves, so this stops the stages immediately
        cfg.sessionRunning = false;
        runState = ST_IDLE;
        emitEvent("button_stop");
      }
    }
  }
}

// ---------- Trial bookkeeping ----------
const char* rewardTriggerName(RewardTriggerType trigger);

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
  emitEvent("hit");
  freeRewardThisTrial = false;
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
  emitEvent("miss");
  freeRewardThisTrial = false;
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
    if (cycleQueueIndex >= cycleQueueLen) { currentBlockPos = -1; currentBlockSize = 0; return; }
    newPos = cycleQueue[cycleQueueIndex++];
  } else {
    int enabled[NUM_POSITIONS];
    int n = 0;
    for (int i = 0; i < NUM_POSITIONS; i++) if (positionNeedsTrials((uint8_t)i)) enabled[n++] = i;
    if (n == 0) { currentBlockPos = -1; currentBlockSize = 0; return; }
    newPos = currentBlockPos;
    if (n == 1) newPos = enabled[0];
    else while (newPos == currentBlockPos) newPos = enabled[random(0, n)];
  }

  currentBlockPos = newPos;
  trialsInCurrentBlock = 0;
  currentBlockSize = sampleBlockSizeForPosition((uint8_t)newPos);
  if (currentBlockSize < 1) { currentBlockPos = -1; return; }
  blockNumber++;
}

uint32_t sampleITI() {
  return cfg.itiMinMs + random(cfg.itiJitterMs + 1);
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
  clearLatchedLick();

  if (freeRewardCfg.enabled && freeRewardCfg.afterConsecutiveMisses > 0 && consecutiveMisses >= freeRewardCfg.afterConsecutiveMisses) {
    freeRewardThisTrial = true;
    emitEvent("free_reward_trial");
  }

  pulsePin(PIN_TRIAL_START, TTL_PULSE_MS);
  emitEvent("trial_start");

  bool ok = moveToPositionSafe(positions[currentTrialPos]);
  if (!ok) {
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

// ---------- Parsing ----------
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
  if (s == "1" || s.equalsIgnoreCase("true") || s.equalsIgnoreCase("on")) { out = true; return true; }
  if (s == "0" || s.equalsIgnoreCase("false") || s.equalsIgnoreCase("off")) { out = false; return true; }
  return false;
}

bool parseIntValue(const String& s, int &out) { if (s.length()==0) return false; out=s.toInt(); return true; }
bool parseUIntValue(const String& s, uint32_t &out) { if (s.length()==0) return false; long v=s.toInt(); if (v<0) v=0; out=(uint32_t)v; return true; }
bool parseFloatValue(const String& s, float &out) { if (s.length()==0) return false; out=s.toFloat(); return true; }

bool handleSet(const String& key, const String& value) {
  bool b; int i; uint32_t u; float f;
  if (key == "task.reward_ms") { if(!parseUIntValue(value,u)) return false; cfg.rewardOpenMs=u; return true; }
  if (key == "task.reward_ul") { if(!parseFloatValue(value,f)) return false; cfg.estimatedRewardUL=f; return true; }
  if (key == "task.water_limit_ul") { if(!parseFloatValue(value,f)) return false; cfg.sessionWaterLimitUL=f; return true; }
  if (key == "task.enforce_no_lick") { if(!parseBoolValue(value,b)) return false; cfg.enforceNoLick=b; return true; }
  if (key == "task.manual_reward_allowed") { if(!parseBoolValue(value,b)) return false; cfg.manualRewardAllowed=b; return true; }
  if (key == "task.settle_ms") { if(!parseUIntValue(value,u)) return false; cfg.settleMs=u; return true; }
  if (key == "task.post_reward_hold_ms") { if(!parseUIntValue(value,u)) return false; cfg.postRewardHoldMs=u; return true; }
  if (key == "task.pre_cue_min_ms") { if(!parseUIntValue(value,u)) return false; cfg.preCueMinMs=u; return true; }
  if (key == "task.pre_cue_max_ms") { if(!parseUIntValue(value,u)) return false; cfg.preCueMaxMs=u; return true; }
  if (key == "task.response_window_ms") { if(!parseUIntValue(value,u)) return false; cfg.responseWindowMs=u; return true; }
  if (key == "task.iti_min_ms") { if(!parseUIntValue(value,u)) return false; cfg.itiMinMs=u; return true; }
  if (key == "task.iti_jitter_ms") { if(!parseUIntValue(value,u)) return false; cfg.itiJitterMs=u; return true; }
  if (key == "task.auto_reward_delay_ms") { if(!parseUIntValue(value,u)) return false; cfg.autoRewardDelayMs=u; return true; }
  if (key == "task.auto_hold_after_miss_enabled") { if(!parseBoolValue(value,b)) return false; cfg.autoHoldAfterMissEnabled=b; if(!b) setAutoRewardHold(false, false); return true; }
  if (key == "task.auto_hold_after_miss_threshold") { if(!parseUIntValue(value,u)) return false; if(u<1)u=1; cfg.autoHoldAfterMissThreshold=u; return true; }
  if (key == "task.rewards_held" || key == "task.reward_hold") {
    if(!parseBoolValue(value,b)) return false;
    if (b) setManualRewardHold(true, false);
    else clearAllRewardHolds(false);
    return true;
  }
  if (key == "task.block_size") { if(!parseIntValue(value,i)) return false; if(i<1)i=1; cfg.blockSize=(uint8_t)i; cfg.blockSizeMin=(uint8_t)i; cfg.blockSizeMax=(uint8_t)i; return true; }
  if (key == "task.block_size_min") { if(!parseIntValue(value,i)) return false; if(i<1)i=1; cfg.blockSizeMin=(uint8_t)i; normalizeSchedulerConfig(); return true; }
  if (key == "task.block_size_max") { if(!parseIntValue(value,i)) return false; if(i<1)i=1; cfg.blockSizeMax=(uint8_t)i; normalizeSchedulerConfig(); return true; }
  if (key == "task.target_trials_per_position_enabled") { if(!parseBoolValue(value,b)) return false; cfg.targetTrialsPerPositionEnabled=b; return true; }
  if (key == "task.target_trials_per_position") { if(!parseUIntValue(value,u)) return false; if(u<1)u=1; cfg.targetTrialsPerPosition=u; return true; }
  if (key == "task.max_duration_enabled") { if(!parseBoolValue(value,b)) return false; cfg.maxDurationEnabled=b; return true; }
  if (key == "task.max_duration_min") { if(!parseUIntValue(value,u)) return false; if(u<1)u=1; cfg.maxDurationMin=u; return true; }
  if (key == "task.scheduling_mode") { if (value == "balanced_block_cycles" || value == "balanced") { cfg.scheduleMode = SCHEDULE_BALANCED_BLOCK_CYCLES; return true; } if (value == "random_blocks" || value == "random") { cfg.scheduleMode = SCHEDULE_RANDOM_BLOCKS; return true; } return false; }
  if (key == "task.stop_mode") { if (value == "end_of_current_block" || value == "block") { cfg.stopMode = STOP_END_OF_CURRENT_BLOCK; return true; } if (value == "end_of_balanced_cycle" || value == "cycle") { cfg.stopMode = STOP_END_OF_BALANCED_CYCLE; return true; } return false; }
  if (key == "task.reward_mode") {
    if (value == "contingent") cfg.rewardMode = REWARD_CONTINGENT;
    else if (value == "auto_after_delay") cfg.rewardMode = REWARD_AUTO_AFTER_DELAY;
    else if (value == "contingent_or_auto_after_delay") cfg.rewardMode = REWARD_CONTINGENT_OR_AUTO;
    else return false;
    return true;
  }
  if (key.startsWith("task.enable_pos")) {
    int idx = key.substring(String("task.enable_pos").length()).toInt();
    if (idx < 0 || idx >= NUM_POSITIONS) return false;
    if (!parseBoolValue(value,b)) return false;
    cfg.enabledPositions[idx]=b; return true;
  }

  if (key == "cue.frequency_hz") { if(!parseUIntValue(value,u)) return false; cfg.cueFrequencyHz=u; return true; }
  if (key == "cue.duration_ms") { if(!parseUIntValue(value,u)) return false; cfg.cueDurationMs=u; return true; }
  if (key == "cue.volume_pct") { if(!parseIntValue(value,i)) return false; if(i<0)i=0; if(i>100)i=100; cfg.cueVolumePct=i; return true; }

  if (key == "geom.dist_close_mm") { if(!parseFloatValue(value,f)) return false; geom.distanceTierMm[0]=f; resetAdaptiveDistances(); recomputeAllGeneratedPositions(); return true; }
  if (key == "geom.dist_far_mm") { if(!parseFloatValue(value,f)) return false; geom.distanceTierMm[1]=f; resetAdaptiveDistances(); recomputeAllGeneratedPositions(); return true; }
  if (key == "geom.az_center_deg") { if(!parseFloatValue(value,f)) return false; geom.azimuthDeg[0]=f; recomputeAllGeneratedPositions(); return true; }
  if (key == "geom.az_left_deg") { if(!parseFloatValue(value,f)) return false; geom.azimuthDeg[1]=f; recomputeAllGeneratedPositions(); return true; }
  if (key == "geom.az_right_deg") { if(!parseFloatValue(value,f)) return false; geom.azimuthDeg[2]=f; recomputeAllGeneratedPositions(); return true; }
  if (key == "geom.down_angle_deg") { if(!parseFloatValue(value,f)) return false; geom.downwardAngleDeg=f; recomputeAllGeneratedPositions(); return true; }
  if (key == "geom.head_roll_deg") { if(!parseFloatValue(value,f)) return false; geom.headRollDeg=f; recomputeAllGeneratedPositions(); return true; }

  if (key == "motion.mouth_origin.x_mm") { if(!parseFloatValue(value,f)) return false; mouthOrigin.x=f; recomputeAllGeneratedPositions(); savePersistedMotionConfigIfChanged(); return true; }
  if (key == "motion.mouth_origin.y_mm") { if(!parseFloatValue(value,f)) return false; mouthOrigin.y=f; recomputeAllGeneratedPositions(); savePersistedMotionConfigIfChanged(); return true; }
  if (key == "motion.mouth_origin.z_mm") { if(!parseFloatValue(value,f)) return false; mouthOrigin.z=f; recomputeAllGeneratedPositions(); savePersistedMotionConfigIfChanged(); return true; }
  if (key == "motion.dock.x_mm") { if(!parseFloatValue(value,f)) return false; dockPosition.x=f; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "motion.dock.y_mm") { if(!parseFloatValue(value,f)) return false; dockPosition.y=f; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "motion.dock.z_mm") { if(!parseFloatValue(value,f)) return false; dockPosition.z=f; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "motion.safe_z_mm") { if(!parseFloatValue(value,f)) return false; safePosition.z=f; savePersistedMotionConfigIfChanged(); return true; }

  if (key == "adapt.enabled") { if(!parseBoolValue(value,b)) return false; adapt.enabled=b; if(!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true; }
  if (key == "adapt.use_per_position") { if(!parseBoolValue(value,b)) return false; adaptUsePerPosition=b; if(!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true; }
  if (key == "adapt.hits_to_advance") { if(!parseIntValue(value,i)) return false; if(i<1)i=1; adapt.hitsToAdvance=i; if(!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true; }
  if (key == "adapt.misses_to_decrease") { if(!parseIntValue(value,i)) return false; if(i<1)i=1; adapt.missesToDecrease=i; if(!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true; }
  if (key == "adapt.step_mm") { if(!parseFloatValue(value,f)) return false; if(f<0)f=0; adapt.stepMm=f; if(!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true; }
  if (key == "adapt.decrease_step_mm") { if(!parseFloatValue(value,f)) return false; if(f<0)f=0; adapt.decreaseStepMm=f; if(!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true; }
  if (key == "adapt.min_distance_mm") { if(!parseFloatValue(value,f)) return false; adapt.minDistanceMm=f; if(!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true; }
  if (key == "adapt.max_distance_mm") { if(!parseFloatValue(value,f)) return false; adapt.maxDistanceMm=f; if(!adaptUsePerPosition) syncAdaptivePositionsFromGlobal(); return true; }
  if (key.startsWith("adapt.pos")) {
    int dotIx = key.indexOf('.', String("adapt.pos").length());
    if (dotIx < 0) return false;
    int idx = key.substring(String("adapt.pos").length(), dotIx).toInt();
    if (idx < 0 || idx >= NUM_POSITIONS) return false;
    String subkey = key.substring(dotIx + 1);
    if (subkey == "enabled") { if(!parseBoolValue(value,b)) return false; adaptPos[idx].enabled=b; return true; }
    if (subkey == "hits_to_advance") { if(!parseIntValue(value,i)) return false; if(i<1)i=1; adaptPos[idx].hitsToAdvance=i; return true; }
    if (subkey == "misses_to_decrease") { if(!parseIntValue(value,i)) return false; if(i<1)i=1; adaptPos[idx].missesToDecrease=i; return true; }
    if (subkey == "step_mm") { if(!parseFloatValue(value,f)) return false; if(f<0)f=0; adaptPos[idx].stepMm=f; return true; }
    if (subkey == "decrease_step_mm") { if(!parseFloatValue(value,f)) return false; if(f<0)f=0; adaptPos[idx].decreaseStepMm=f; return true; }
    if (subkey == "min_distance_mm") { if(!parseFloatValue(value,f)) return false; adaptPos[idx].minDistanceMm=f; return true; }
    if (subkey == "max_distance_mm") { if(!parseFloatValue(value,f)) return false; adaptPos[idx].maxDistanceMm=f; return true; }
    return false;
  }

  if (key == "free_reward.enabled") { if(!parseBoolValue(value,b)) return false; freeRewardCfg.enabled=b; return true; }
  if (key == "free_reward.after_misses") { if(!parseIntValue(value,i)) return false; freeRewardCfg.afterConsecutiveMisses=i; return true; }
  if (key == "free_reward.delay_ms") { if(!parseUIntValue(value,u)) return false; freeRewardCfg.delayAfterCueMs=u; return true; }

  if (key == "lick.active_low") { if(!parseBoolValue(value,b)) return false; lickCfg.activeLow=b; return true; }
  if (key == "lick.debounce_ms") { if(!parseUIntValue(value,u)) return false; lickCfg.debounceMs=u; return true; }
  if (key == "lick.debug") { if(!parseBoolValue(value,b)) return false; lickCfg.debug=b; return true; }
  if (key == "lick.threshold_counts") { if(!parseIntValue(value,i)) return false; lickCfg.thresholdCounts=i; return true; }
  if (key == "lick.hysteresis_counts") { if(!parseIntValue(value,i)) return false; lickCfg.hysteresisCounts=i; return true; }
  if (key == "lick.polarity") { if(!parseIntValue(value,i)) return false; lickCfg.polarity=i; return true; }
  if (key == "lick.baseline_alpha") { if(!parseFloatValue(value,f)) return false; lickCfg.baselineAlpha=f; return true; }
  if (key == "lick.refractory_ms") { if(!parseUIntValue(value,u)) return false; lickCfg.refractoryMs=u; return true; }

  if (key == "zaber.axis.x.device_id") { if(!parseIntValue(value,i)) return false; axisX.deviceId=(uint8_t)i; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "zaber.axis.y.device_id") { if(!parseIntValue(value,i)) return false; axisY.deviceId=(uint8_t)i; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "zaber.axis.z.device_id") { if(!parseIntValue(value,i)) return false; axisZ.deviceId=(uint8_t)i; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "zaber.axis.x.axis_number") { if(!parseIntValue(value,i)) return false; axisX.axisNumber=(uint8_t)i; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "zaber.axis.y.axis_number") { if(!parseIntValue(value,i)) return false; axisY.axisNumber=(uint8_t)i; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "zaber.axis.z.axis_number") { if(!parseIntValue(value,i)) return false; axisZ.axisNumber=(uint8_t)i; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "zaber.axis.x.units_per_mm") { if(!parseFloatValue(value,f)) return false; axisX.unitsPerMM=f; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "zaber.axis.y.units_per_mm") { if(!parseFloatValue(value,f)) return false; axisY.unitsPerMM=f; savePersistedMotionConfigIfChanged(); return true; }
  if (key == "zaber.axis.z.units_per_mm") { if(!parseFloatValue(value,f)) return false; axisZ.unitsPerMM=f; savePersistedMotionConfigIfChanged(); return true; }

  if (key == "sync.pulse_ms") { if(!parseUIntValue(value,u)) return false; syncPulseMinMs=u; syncPulseMaxMs=u; return true; }
  if (key == "sync.min_pulse_ms") { if(!parseUIntValue(value,u)) return false; syncPulseMinMs=u; return true; }
  if (key == "sync.max_pulse_ms") { if(!parseUIntValue(value,u)) return false; syncPulseMaxMs=u; return true; }
  if (key == "sync.min_interval_ms") { if(!parseUIntValue(value,u)) return false; syncMinIntervalMs=u; return true; }
  if (key == "sync.max_interval_ms") { if(!parseUIntValue(value,u)) return false; syncMaxIntervalMs=u; return true; }

  return false;
}

void handleGet(const String& kind) {
  if (kind == "status") { emitStatus(); return; }
  if (kind == "positions") { emitPositions(); return; }
  if (kind == "config") { emitConfig(); return; }
  if (kind == "zaberconfig") { emitZaberConfig(); return; }
  if (kind == "stats") { emitStats(); return; }
  if (kind == "all") { emitStatus(); emitPositions(); emitConfig(); emitStats(); return; }
  emitErr("get", "bad_kind", kind);
}

void handleMove(const String& rest) {
  abortMotion = false;   // a fresh motion request clears any latched abort
  String mode = getArg(rest, "mode");
  if (mode.length() == 0) { emitErr("move", "missing_mode"); return; }

  if (mode == "jog") {
    String axisName = getArg(rest, "axis");
    String mmStr = getArg(rest, "mm");
    float mm;
    if (!parseFloatValue(mmStr, mm)) { emitErr("move", "bad_mm", mmStr); return; }
    ZAxis* a = nullptr;
    if (axisName == "x") a = &axisX; else if (axisName == "y") a = &axisY; else if (axisName == "z") a = &axisZ;
    if (!a) { emitErr("move", "bad_axis", axisName); return; }
    Result rr = zMoveRelResult(*a, mmToUnitsLong(*a, mm));
    if (rr.getError() != Result::OK) {
      emitErr("move", "failed", String("mode=jog axis=") + axisName + " device=" + a->deviceId + " axis_number=" + a->axisNumber + " zaber_error=" + rr.getErrorString());
      return;
    }
    zWaitIdle(*a);
    a->posMM += mm;
    emitOK("move", String("mode=jog axis=") + axisName + " device=" + a->deviceId + " axis_number=" + a->axisNumber + " mm=" + String(mm, 3));
    emitStatus();
    return;
  }

  if (mode == "xyz") {
    float x,y,z;
    if (!parseFloatValue(getArg(rest, "x"), x) || !parseFloatValue(getArg(rest, "y"), y) || !parseFloatValue(getArg(rest, "z"), z)) {
      emitErr("move", "missing_xyz"); return;
    }
    Vec3 target = {x,y,z};
    if (!moveToPositionSafe(target)) { emitErr("move", "failed", "mode=xyz"); return; }
    emitOK("move", "mode=xyz");
    emitStatus();
    return;
  }

  if (mode == "pos") {
    int idx;
    if (!parseIntValue(getArg(rest, "idx"), idx) || idx < 0 || idx >= NUM_POSITIONS) {
      emitErr("move", "bad_pos_idx"); return;
    }
    if (!moveToNamedPosition((uint8_t)idx)) { emitErr("move", "failed", String("mode=pos idx=") + idx); return; }
    emitOK("move", String("mode=pos idx=") + idx);
    emitStatus();
    return;
  }

  if (mode == "device") {
    int dev, axisNum; float mm;
    if (!parseIntValue(getArg(rest, "device"), dev) || !parseIntValue(getArg(rest, "axis"), axisNum) || !parseFloatValue(getArg(rest, "mm"), mm)) {
      emitErr("move", "missing_device_axis_or_mm"); return;
    }
    float upm = axisX.unitsPerMM;
    if ((uint8_t)dev == axisY.deviceId && (uint8_t)axisNum == axisY.axisNumber) upm = axisY.unitsPerMM;
    if ((uint8_t)dev == axisZ.deviceId && (uint8_t)axisNum == axisZ.axisNumber) upm = axisZ.unitsPerMM;
    long units = (long)lround(mm * upm);
    Result rr = connection.genericCommand(Command("move rel", units), (uint8_t)dev, (uint8_t)axisNum);
    if (rr.getError() != Result::OK) {
      emitErr("move", "failed", String("mode=device device=") + dev + " axis=" + axisNum + " zaber_error=" + rr.getErrorString());
      return;
    }
    if (!zWaitIdleByAddress((uint8_t)dev, (uint8_t)axisNum)) { emitErr("move", "failed", String("mode=device device=") + dev + " axis=" + axisNum + " detail=wait_idle_failed"); return; }
    emitOK("move", String("mode=device device=") + dev + " axis=" + axisNum + " mm=" + String(mm, 3));
    emitStatus();
    return;
  }

  emitErr("move", "bad_mode", mode);
}

void handleCal(const String& rest) {
  String kind = getArg(rest, "kind");
  if (kind == "reward") {
    uint32_t pulses = 0;
    if (!parseUIntValue(getArg(rest, "pulses"), pulses)) { emitErr("cal", "missing_pulses"); return; }
    for (uint32_t i = 0; i < pulses; i++) {
      pendingRewardTrigger = REWARD_TRIGGER_CALIBRATION;
      openRewardValve(cfg.rewardOpenMs);
      totalRewards++;
      sessionWaterDeliveredUL += cfg.estimatedRewardUL;
      emitEvent("reward_cal_pulse");
      pendingRewardTrigger = REWARD_TRIGGER_NONE;
      delay(100);
    }
    emitOK("cal", String("kind=reward pulses=") + pulses);
    return;
  }
  emitErr("cal", "bad_kind", kind);
}



bool isPrintableAsciiLine(const String& s) {
  for (unsigned int i = 0; i < s.length(); i++) {
    char c = s.charAt(i);
    if (c == '\r' || c == '\n' || c == '\t') continue;
    if (c < 32 || c > 126) return false;
  }
  return true;
}

bool isValidCommandToken(const String& s) {
  if (s.length() == 0) return false;
  for (unsigned int i = 0; i < s.length(); i++) {
    char c = s.charAt(i);
    bool isUpper = (c >= 'A' && c <= 'Z');
    bool isLower = (c >= 'a' && c <= 'z');
    if (!(isUpper || isLower)) return false;
  }
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

bool handleSerialDuringMotionWait() {
  String line;
  if (!readSerialLineNonBlocking(line)) return false;
  if (!isPrintableAsciiLine(line)) return false;

  int sp = line.indexOf(' ');
  String cmd = (sp < 0) ? line : line.substring(0, sp);
  if (!isValidCommandToken(cmd)) return false;
  String rest = (sp < 0) ? "" : line.substring(sp + 1);
  rest.trim();

  if (cmd.equalsIgnoreCase("PING")) { emitOK("ping", String("t_ms=") + millis()); return true; }
  if (cmd.equalsIgnoreCase("HELP")) { Serial.println("INFO kind=help commands=PING,HELP,START,STOP,HOME,REWARD,CUEREWARD,HOLDREWARDS,RESUMEREWARDS,RESETSESSION,GET,SET,MOVE,CAL,CUE (MOVE mode=device device=N axis=M mm=X supported)"); return true; }
  if (cmd.equalsIgnoreCase("HOLDREWARDS")) { setManualRewardHold(true, true); emitOK("holdrewards"); return true; }
  if (cmd.equalsIgnoreCase("RESUMEREWARDS")) { clearAllRewardHolds(true); emitOK("resumerewards"); return true; }
  if (cmd.equalsIgnoreCase("SET")) {
    int eq = rest.indexOf('=');
    if (eq < 0) { emitErr("set", "missing_equals"); return true; }
    String key = rest.substring(0, eq); key.trim();
    String value = rest.substring(eq + 1); value.trim();
    if (key == "task.rewards_held" || key == "task.reward_hold") {
      if (!handleSet(key, value)) emitErr("set", "bad_key_or_value", key);
      else emitOK("set", String("key=") + key + " value=" + value);
    } else {
      emitErr("busy", "motion_wait", "SET");
    }
    return true;
  }
  if (cmd.equalsIgnoreCase("GET")) {
    String kind = getArg(rest, "kind");
    if (kind.length() == 0) { emitErr("get", "missing_kind"); }
    else { handleGet(kind); }
    return true;
  }
  if (cmd.equalsIgnoreCase("STOP")) {
    requestStopStages();   // halt the STAGES, not just the state machine (deferred if mid-wait)
    cfg.sessionRunning = false;
    stopPending = false;
    stopPendingReason = STOP_PENDING_NONE;
    lickSensingEnabled = false;
    pendingRewardTrigger = REWARD_TRIGGER_NONE;
    runState = ST_IDLE;
    emitOK("stop");
    return true;
  }

  emitErr("busy", "motion_wait", cmd);
  return true;
}
void handleSerial() {
  String line;
  if (!readSerialLineNonBlocking(line)) return;
  if (!isPrintableAsciiLine(line)) return;

  int sp = line.indexOf(' ');
  String cmd = (sp < 0) ? line : line.substring(0, sp);
  if (!isValidCommandToken(cmd)) return;
  String rest = (sp < 0) ? "" : line.substring(sp + 1);
  rest.trim();

  if (cmd.equalsIgnoreCase("PING")) { emitOK("ping", String("t_ms=") + millis()); return; }
  if (cmd.equalsIgnoreCase("HELP")) { Serial.println("INFO kind=help commands=PING,HELP,START,STOP,HOME,REWARD,CUEREWARD,HOLDREWARDS,RESUMEREWARDS,RESETSESSION,GET,SET,MOVE,CAL,CUE (MOVE mode=device device=N axis=M mm=X supported)"); return; }
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
  if (cmd.equalsIgnoreCase("STOP")) { requestStopStages(); cfg.sessionRunning = false; runState = ST_IDLE; emitOK("stop"); return; }
  if (cmd.equalsIgnoreCase("HOME")) {
    abortMotion = false; homeAllAxes(); return; }
  if (cmd.equalsIgnoreCase("REWARD")) {
    if (!cfg.manualRewardAllowed) { emitErr("reward", "manual_disabled"); return; }
    deliverRewardForTrigger(REWARD_TRIGGER_MANUAL);
    emitOK("reward");
    return;
  }
  if (cmd.equalsIgnoreCase("CUEREWARD")) {
    if (!cfg.manualRewardAllowed) { emitErr("cuereward", "manual_disabled"); return; }
    playCue();   // cue gate is driven inside playCue()
    emitEvent("cue_only");
    deliverRewardForTrigger(REWARD_TRIGGER_MANUAL);
    emitOK("cuereward");
    return;
  }
  if (cmd.equalsIgnoreCase("CUE")) { cueOnly(); emitOK("cue"); return; }
  if (cmd.equalsIgnoreCase("HOLDREWARDS")) { setManualRewardHold(true, true); emitOK("holdrewards"); return; }
  if (cmd.equalsIgnoreCase("RESUMEREWARDS")) { clearAllRewardHolds(true); emitOK("resumerewards"); return; }
  if (cmd.equalsIgnoreCase("RESETSESSION")) {
    abortMotion = false; resetSessionStats(); resetAdaptiveDistances(); recomputeAllGeneratedPositions(); normalizeSchedulerConfig(); nextSyncAt = millis() + random(syncMinIntervalMs, syncMaxIntervalMs + 1); emitOK("resetsession"); return; }
  if (cmd.equalsIgnoreCase("GET")) { String kind=getArg(rest,"kind"); if(kind.length()==0){emitErr("get","missing_kind"); return;} handleGet(kind); return; }
  if (cmd.equalsIgnoreCase("SET")) {
    int eq = rest.indexOf('=');
    if (eq < 0) { emitErr("set", "missing_equals"); return; }
    String key = rest.substring(0, eq); key.trim();
    String value = rest.substring(eq + 1); value.trim();
    if (!handleSet(key, value)) { emitErr("set", "bad_key_or_value", key); return; }
    emitOK("set", String("key=") + key + " value=" + value); return;
  }
  if (cmd.equalsIgnoreCase("MOVE")) { handleMove(rest); return; }
  if (cmd.equalsIgnoreCase("CAL")) { handleCal(rest); return; }

  emitErr("unknown", "bad_cmd", cmd);
}

// ---------- Core ----------
void updateButton();
void updateLick();
void updateTTLPulses();
void updateSync();

void serviceCore() {
  updateTTLPulses();
  updateLick();
  updateButton();
  updateSync();
}

void setup() {
  // Preload safe inactive levels before enabling outputs, so reset/boot does not
  // momentarily energize the solenoid or other TTL lines during serial connect.
  digitalWrite(PIN_SOLENOID, LOW);
  digitalWrite(PIN_TTL_REWARD, LOW);
  digitalWrite(PIN_TTL_TRIAL_STOP, LOW);
  digitalWrite(PIN_SYNC, LOW);
  digitalWrite(PIN_CUE_TTL, LOW);
  digitalWrite(PIN_TRIAL_START, LOW);
  digitalWrite(PIN_CUE_AUDIO, LOW);
  digitalWrite(PIN_TTL_POS0, LOW);
  digitalWrite(PIN_TTL_POS1, LOW);
  digitalWrite(PIN_TTL_POS2, LOW);
  digitalWrite(PIN_TTL_POS_STB, LOW);

  pinMode(PIN_SOLENOID, OUTPUT);
  pinMode(PIN_TTL_REWARD, OUTPUT);
  pinMode(PIN_TTL_TRIAL_STOP, OUTPUT);
  pinMode(PIN_SYNC, OUTPUT);
  pinMode(PIN_CUE_TTL, OUTPUT);
  pinMode(PIN_TRIAL_START, OUTPUT);
  pinMode(PIN_CUE_AUDIO, OUTPUT);
  pinMode(PIN_TTL_POS0, OUTPUT);
  pinMode(PIN_TTL_POS1, OUTPUT);
  pinMode(PIN_TTL_POS2, OUTPUT);
  pinMode(PIN_TTL_POS_STB, OUTPUT);

  shield.begin(115200);
  Serial.begin(115200);
  randomSeed(analogRead(A15));

  pinMode(PIN_LICK_IN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_LICK_IN), lickISR, CHANGE);  // v38: capture every lick onset
  pinMode(PIN_STARTSTOP_BUTTON, INPUT); // shield provides pull-up on D7

  lickCurrent = false;
  buttonPrev = digitalRead(PIN_STARTSTOP_BUTTON);

  loadPersistedMotionConfig();
  zRefreshAllAxisPosMM();
  syncAdaptivePositionsFromGlobal();
  resetAdaptiveDistances();
  resetSessionStats();
  recomputeAllGeneratedPositions();
  normalizeSchedulerConfig();
  nextSyncAt = millis() + random(syncMinIntervalMs, syncMaxIntervalMs + 1);
  savePersistedMotionConfigIfChanged();

  emitInfoReady();
}

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
        enlViolations++;
        emitEvent("pre_cue_reset_by_lick");
      }
      if (millis() >= cueEligibleAtMs) {
        playCue();   // cue gate is driven inside playCue()
        emitEvent("cue");
        responseDeadlineMs = millis() + cfg.responseWindowMs;
        if (freeRewardThisTrial) freeRewardAtMs = millis() + freeRewardCfg.delayAfterCueMs;
        runState = ST_WAIT_FOR_LICK;
        stateStartMs = millis();
      }
      break;

    case ST_WAIT_FOR_LICK:
      if (cfg.rewardMode == REWARD_CONTINGENT) {
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
      } else if (cfg.rewardMode == REWARD_AUTO_AFTER_DELAY) {
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
        if (!rewardIssuedThisTrial && millis() - stateStartMs >= cfg.autoRewardDelayMs) {
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
      } else { // contingent_or_auto
        if (!rewardIssuedThisTrial && consumeLickOnset()) {
          successfulLickThisTrial = true;
          if (currentTrialPos >= 0) registerHit((uint8_t)currentTrialPos);
          pendingRewardTrigger = REWARD_TRIGGER_CONTINGENT;
          runState = ST_DELIVER_REWARD;
          stateStartMs = millis();
        } else if (!rewardIssuedThisTrial && millis() - stateStartMs >= cfg.autoRewardDelayMs) {
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
      pulsePin(PIN_TTL_TRIAL_STOP, TRIAL_STOP_PULSE_MS);
      emitEvent("trial_stop_ttl");
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
        if (cfg.sessionRunning) runState = ST_MOVE_TO_TARGET;
        else runState = ST_IDLE;
      }
      break;
  }
}
