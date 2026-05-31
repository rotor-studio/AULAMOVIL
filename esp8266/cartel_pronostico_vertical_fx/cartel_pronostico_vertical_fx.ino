#include <Adafruit_GFX.h>
#include <Adafruit_NeoMatrix.h>
#include <Adafruit_NeoPixel.h>
#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>

#ifndef PSTR
#define PSTR
#endif

const char* WIFI_SSID = "CHANGE_WIFI_SSID";
const char* WIFI_PASSWORD = "CHANGE_WIFI_PASSWORD";
const char* API_URL = "http://192.168.1.109:8000/api/sign/latest";

const unsigned long WIFI_TIMEOUT_MS = 15000;
const unsigned long POLL_INTERVAL_MS = 30000;
const unsigned long SCROLL_DELAY_MS = 160;
const unsigned long RAIN_STEP_MS = 120;
const uint8_t CHAR_HEIGHT = 8;
const int8_t CHAR_LEFT_MARGIN = 1;
const uint8_t FONT_WIDTH = 6;
const uint8_t FONT_HEIGHT = 7;
const uint8_t DROP_COUNT = 5;

#define PIN D8

Adafruit_NeoMatrix matrix = Adafruit_NeoMatrix(
  64,
  8,
  PIN,
  NEO_MATRIX_TOP + NEO_MATRIX_LEFT + NEO_MATRIX_COLUMNS + NEO_MATRIX_ZIGZAG,
  NEO_GRB + NEO_KHZ800
);

struct SignPayload {
  bool ok;
  char headline[65];
  char line1[65];
  char line2[65];
  float pm10;
  uint8_t brightness;
  uint8_t color[3];
};

struct RainDrop {
  int16_t x;
  int16_t y;
  uint8_t speed;
};

StaticJsonDocument<2048> doc;
SignPayload currentPayload = {
  false,
  "SIN DATOS",
  "",
  "",
  0.0f,
  24,
  {46, 204, 113}
};

RainDrop drops[DROP_COUNT];

unsigned long lastPollMs = 0;
unsigned long lastScrollMs = 0;
unsigned long lastRainMs = 0;
uint8_t currentPage = 0;
int16_t scrollY = 0;
String displayText = "SIN DATOS";
bool newDataReceived = false;
uint16_t currentTextColor = matrix.Color(255, 255, 255);

static const uint8_t FONT_SPACE[FONT_HEIGHT] PROGMEM = {0, 0, 0, 0, 0, 0, 0};
static const uint8_t FONT_QUESTION[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x01, 0x06, 0x08, 0x00, 0x08};
static const uint8_t FONT_DOT[FONT_HEIGHT] PROGMEM = {0, 0, 0, 0, 0, 0x0C, 0x0C};
static const uint8_t FONT_COMMA[FONT_HEIGHT] PROGMEM = {0, 0, 0, 0, 0, 0x0C, 0x08};
static const uint8_t FONT_COLON[FONT_HEIGHT] PROGMEM = {0, 0x0C, 0x0C, 0, 0x0C, 0x0C, 0};
static const uint8_t FONT_DASH[FONT_HEIGHT] PROGMEM = {0, 0, 0x3F, 0, 0, 0, 0};
static const uint8_t FONT_SLASH[FONT_HEIGHT] PROGMEM = {0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0};
static const uint8_t FONT_0[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x23, 0x25, 0x29, 0x31, 0x1E};
static const uint8_t FONT_1[FONT_HEIGHT] PROGMEM = {0x08, 0x18, 0x08, 0x08, 0x08, 0x08, 0x1C};
static const uint8_t FONT_2[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x01, 0x06, 0x18, 0x20, 0x3F};
static const uint8_t FONT_3[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x01, 0x0E, 0x01, 0x21, 0x1E};
static const uint8_t FONT_4[FONT_HEIGHT] PROGMEM = {0x04, 0x0C, 0x14, 0x24, 0x3F, 0x04, 0x04};
static const uint8_t FONT_5[FONT_HEIGHT] PROGMEM = {0x3F, 0x20, 0x3E, 0x01, 0x01, 0x21, 0x1E};
static const uint8_t FONT_6[FONT_HEIGHT] PROGMEM = {0x0E, 0x10, 0x20, 0x3E, 0x21, 0x21, 0x1E};
static const uint8_t FONT_7[FONT_HEIGHT] PROGMEM = {0x3F, 0x21, 0x02, 0x04, 0x08, 0x08, 0x08};
static const uint8_t FONT_8[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x21, 0x1E, 0x21, 0x21, 0x1E};
static const uint8_t FONT_9[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x21, 0x1F, 0x01, 0x02, 0x1C};
static const uint8_t FONT_A[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x21, 0x3F, 0x21, 0x21, 0x21};
static const uint8_t FONT_B[FONT_HEIGHT] PROGMEM = {0x3E, 0x21, 0x21, 0x3E, 0x21, 0x21, 0x3E};
static const uint8_t FONT_C[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x20, 0x20, 0x20, 0x21, 0x1E};
static const uint8_t FONT_D[FONT_HEIGHT] PROGMEM = {0x3C, 0x22, 0x21, 0x21, 0x21, 0x22, 0x3C};
static const uint8_t FONT_E[FONT_HEIGHT] PROGMEM = {0x3F, 0x20, 0x20, 0x3E, 0x20, 0x20, 0x3F};
static const uint8_t FONT_F[FONT_HEIGHT] PROGMEM = {0x3F, 0x20, 0x20, 0x3E, 0x20, 0x20, 0x20};
static const uint8_t FONT_G[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x20, 0x27, 0x21, 0x21, 0x1F};
static const uint8_t FONT_H[FONT_HEIGHT] PROGMEM = {0x21, 0x21, 0x21, 0x3F, 0x21, 0x21, 0x21};
static const uint8_t FONT_I[FONT_HEIGHT] PROGMEM = {0x1E, 0x08, 0x08, 0x08, 0x08, 0x08, 0x1E};
static const uint8_t FONT_J[FONT_HEIGHT] PROGMEM = {0x07, 0x02, 0x02, 0x02, 0x22, 0x22, 0x1C};
static const uint8_t FONT_K[FONT_HEIGHT] PROGMEM = {0x21, 0x22, 0x24, 0x38, 0x24, 0x22, 0x21};
static const uint8_t FONT_L[FONT_HEIGHT] PROGMEM = {0x20, 0x20, 0x20, 0x20, 0x20, 0x20, 0x3F};
static const uint8_t FONT_M[FONT_HEIGHT] PROGMEM = {0x21, 0x33, 0x2D, 0x2D, 0x21, 0x21, 0x21};
static const uint8_t FONT_N[FONT_HEIGHT] PROGMEM = {0x21, 0x31, 0x29, 0x25, 0x23, 0x21, 0x21};
static const uint8_t FONT_O[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x21, 0x21, 0x21, 0x21, 0x1E};
static const uint8_t FONT_P[FONT_HEIGHT] PROGMEM = {0x3E, 0x21, 0x21, 0x3E, 0x20, 0x20, 0x20};
static const uint8_t FONT_Q[FONT_HEIGHT] PROGMEM = {0x1E, 0x21, 0x21, 0x21, 0x25, 0x22, 0x1D};
static const uint8_t FONT_R[FONT_HEIGHT] PROGMEM = {0x3E, 0x21, 0x21, 0x3E, 0x24, 0x22, 0x21};
static const uint8_t FONT_S[FONT_HEIGHT] PROGMEM = {0x1F, 0x20, 0x20, 0x1E, 0x01, 0x01, 0x3E};
static const uint8_t FONT_T[FONT_HEIGHT] PROGMEM = {0x3F, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08};
static const uint8_t FONT_U[FONT_HEIGHT] PROGMEM = {0x21, 0x21, 0x21, 0x21, 0x21, 0x21, 0x1E};
static const uint8_t FONT_V[FONT_HEIGHT] PROGMEM = {0x21, 0x21, 0x21, 0x21, 0x12, 0x12, 0x0C};
static const uint8_t FONT_W[FONT_HEIGHT] PROGMEM = {0x21, 0x21, 0x21, 0x2D, 0x2D, 0x33, 0x21};
static const uint8_t FONT_X[FONT_HEIGHT] PROGMEM = {0x21, 0x12, 0x0C, 0x0C, 0x0C, 0x12, 0x21};
static const uint8_t FONT_Y[FONT_HEIGHT] PROGMEM = {0x21, 0x12, 0x0C, 0x08, 0x08, 0x08, 0x08};
static const uint8_t FONT_Z[FONT_HEIGHT] PROGMEM = {0x3F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x3F};

void safeCopy(char* dest, size_t size, const char* src) {
  if (!dest || size == 0) return;
  if (!src) src = "";
  strncpy(dest, src, size - 1);
  dest[size - 1] = '\0';
}

void normalizeText(char* text) {
  if (!text) return;

  for (size_t i = 0; text[i] != '\0'; i++) {
    char c = text[i];

    if (c >= 'a' && c <= 'z') {
      text[i] = c - 32;
      continue;
    }

    if (c < 32 || c > 126) {
      text[i] = ' ';
    }
  }
}

uint16_t colorForPm10(float pm10) {
  if (pm10 <= 12.0f) {
    return matrix.Color(0, 255, 0);
  } else if (pm10 <= 35.4f) {
    return matrix.Color(255, 255, 0);
  } else if (pm10 <= 55.4f) {
    return matrix.Color(255, 165, 0);
  } else if (pm10 <= 150.0f) {
    return matrix.Color(255, 69, 0);
  } else if (pm10 <= 300.0f) {
    return matrix.Color(255, 0, 0);
  }

  return matrix.Color(128, 0, 128);
}

void logWiFiStatus() {
  Serial.print("[wifi] status=");
  Serial.print(WiFi.status());
  Serial.print(" ip=");
  Serial.print(WiFi.localIP());
  Serial.print(" rssi=");
  Serial.println(WiFi.RSSI());
}

bool connectWiFi() {
  Serial.print("[wifi] conectando a ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < WIFI_TIMEOUT_MS) {
    delay(250);
    Serial.print(".");
    yield();
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] no conectado");
    logWiFiStatus();
    return false;
  }

  Serial.println("[wifi] conectado");
  logWiFiStatus();
  return true;
}

bool ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  Serial.println("[wifi] reconectando");
  return connectWiFi();
}

bool parsePayload(const String& body, SignPayload& payload) {
  doc.clear();
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    Serial.print("[json] error=");
    Serial.println(err.c_str());
    return false;
  }

  JsonObject forecast = doc["forecast"];
  JsonObject air = doc["air"];
  JsonObject display = doc["display"];

  safeCopy(payload.headline, sizeof(payload.headline), display["headline"] | forecast["label"] | "Seguimiento");
  safeCopy(payload.line1, sizeof(payload.line1), display["line1"] | forecast["summary"] | "Sin resumen");
  safeCopy(payload.line2, sizeof(payload.line2), display["line2"] | air["label"] | "Sin datos");
  normalizeText(payload.headline);
  normalizeText(payload.line1);
  normalizeText(payload.line2);

  payload.pm10 = 0.0f;
  if (!air["pm10_ugm3"].isNull()) {
    payload.pm10 = air["pm10_ugm3"].as<float>();
  } else if (!doc["metrics"]["pm10_ugm3"].isNull()) {
    payload.pm10 = doc["metrics"]["pm10_ugm3"].as<float>();
  }
  payload.brightness = (uint8_t)(display["brightness"] | 24);
  if (payload.brightness > 40) payload.brightness = 40;
  uint16_t pmColor = colorForPm10(payload.pm10);
  payload.color[0] = (pmColor >> 11) & 0x1F;
  payload.color[0] = map(payload.color[0], 0, 31, 0, 255);
  payload.color[1] = (pmColor >> 5) & 0x3F;
  payload.color[1] = map(payload.color[1], 0, 63, 0, 255);
  payload.color[2] = pmColor & 0x1F;
  payload.color[2] = map(payload.color[2], 0, 31, 0, 255);
  payload.ok = true;

  return true;
}

bool fetchPayload(SignPayload& payload) {
  WiFiClient client;
  HTTPClient http;

  client.setTimeout(4000);
  http.setTimeout(4000);

  if (!http.begin(client, API_URL)) {
    return false;
  }

  int status = http.GET();
  if (status <= 0 || status != HTTP_CODE_OK) {
    http.end();
    return false;
  }

  String body = http.getString();
  http.end();
  return parsePayload(body, payload);
}

const char* pageText() {
  if (!currentPayload.ok) return "SIN DATOS";
  if (currentPage == 0) return currentPayload.headline;
  if (currentPage == 1) return currentPayload.line1;
  return currentPayload.line2;
}

void resetScroll() {
  scrollY = matrix.height();
}

void nextPage() {
  currentPage = (currentPage + 1) % 3;
  resetScroll();
  displayText = pageText();
  newDataReceived = true;
}

void updateDisplayText() {
  displayText = pageText();
  newDataReceived = true;
}

int16_t verticalTextHeight() {
  return displayText.length() * CHAR_HEIGHT;
}

const uint8_t* glyphForChar(char ch) {
  switch (ch) {
    case ' ': return FONT_SPACE;
    case '.': return FONT_DOT;
    case ',': return FONT_COMMA;
    case ':': return FONT_COLON;
    case '-': return FONT_DASH;
    case '/': return FONT_SLASH;
    case '0': return FONT_0;
    case '1': return FONT_1;
    case '2': return FONT_2;
    case '3': return FONT_3;
    case '4': return FONT_4;
    case '5': return FONT_5;
    case '6': return FONT_6;
    case '7': return FONT_7;
    case '8': return FONT_8;
    case '9': return FONT_9;
    case 'A': return FONT_A;
    case 'B': return FONT_B;
    case 'C': return FONT_C;
    case 'D': return FONT_D;
    case 'E': return FONT_E;
    case 'F': return FONT_F;
    case 'G': return FONT_G;
    case 'H': return FONT_H;
    case 'I': return FONT_I;
    case 'J': return FONT_J;
    case 'K': return FONT_K;
    case 'L': return FONT_L;
    case 'M': return FONT_M;
    case 'N': return FONT_N;
    case 'O': return FONT_O;
    case 'P': return FONT_P;
    case 'Q': return FONT_Q;
    case 'R': return FONT_R;
    case 'S': return FONT_S;
    case 'T': return FONT_T;
    case 'U': return FONT_U;
    case 'V': return FONT_V;
    case 'W': return FONT_W;
    case 'X': return FONT_X;
    case 'Y': return FONT_Y;
    case 'Z': return FONT_Z;
    default: return FONT_QUESTION;
  }
}

void drawVerticalChar(char ch, int16_t y) {
  const uint8_t* glyph = glyphForChar(ch);
  for (uint8_t row = 0; row < FONT_HEIGHT; row++) {
    uint8_t bits = pgm_read_byte(&glyph[row]);
    for (uint8_t col = 0; col < FONT_WIDTH; col++) {
      if (bits & (1 << (FONT_WIDTH - 1 - col))) {
        matrix.drawPixel(CHAR_LEFT_MARGIN + col, y + row, currentTextColor);
      }
    }
  }
}

void drawText() {
  matrix.fillScreen(0);
  for (uint16_t i = 0; i < displayText.length(); i++) {
    drawVerticalChar(displayText.charAt(i), scrollY + (i * CHAR_HEIGHT));
  }
  matrix.show();
}

void setup() {
  Serial.begin(115200);
  delay(200);

  matrix.begin();
  matrix.setRotation(1);
  matrix.setTextWrap(false);
  matrix.setBrightness(40);
  matrix.setTextColor(matrix.Color(255, 255, 255));
  matrix.fillScreen(0);
  matrix.show();

  connectWiFi();

  SignPayload nextPayload = currentPayload;
  if (WiFi.status() == WL_CONNECTED && fetchPayload(nextPayload)) {
    currentPayload = nextPayload;
    matrix.setBrightness(currentPayload.brightness);
    currentTextColor = colorForPm10(currentPayload.pm10);
  } else {
    currentTextColor = matrix.Color(255, 255, 255);
  }

  updateDisplayText();
  resetScroll();
}

void loop() {
  if (!ensureWiFi()) {
    displayText = "WIFI";
    currentTextColor = matrix.Color(255, 120, 0);
    newDataReceived = true;
    delay(250);
    return;
  }

  if (millis() - lastPollMs >= POLL_INTERVAL_MS || !currentPayload.ok) {
    lastPollMs = millis();
    SignPayload nextPayload = currentPayload;
    if (fetchPayload(nextPayload)) {
      currentPayload = nextPayload;
      matrix.setBrightness(currentPayload.brightness);
      currentTextColor = colorForPm10(currentPayload.pm10);
      updateDisplayText();
    }
  }

  if (millis() - lastScrollMs >= SCROLL_DELAY_MS || newDataReceived) {
    if (newDataReceived) {
      matrix.fillScreen(0);
      scrollY = matrix.height();
      newDataReceived = false;
    }

    drawText();
    lastScrollMs = millis();

    if (--scrollY < -verticalTextHeight()) {
      nextPage();
    }
  }
}
