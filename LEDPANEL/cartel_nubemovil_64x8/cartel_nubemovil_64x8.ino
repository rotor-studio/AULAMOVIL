#include <Adafruit_GFX.h>
#include <Adafruit_NeoMatrix.h>
#include <Adafruit_NeoPixel.h>
#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>

#ifndef PSTR
#define PSTR
#endif

const char* WIFI_SSID = "NUBEMOVIL";
const char* WIFI_PASSWORD = "100*Nubemovil001";
const char* API_URL = "http://192.168.1.109:8000/api/sign/latest";

const unsigned long WIFI_TIMEOUT_MS = 15000;
const unsigned long POLL_INTERVAL_MS = 30000;
const unsigned long SCROLL_DELAY_MS = 160;
const unsigned long WIFI_STARTUP_DELAY_MS = 60000;
const uint8_t FONT_WIDTH = 5;
const uint8_t FONT_HEIGHT = 6;
const uint8_t CHAR_TOP_MARGIN = 1;
const int16_t CHAR_ADVANCE_PX = 6;

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
  bool hasRemoteColor;
};

StaticJsonDocument<2048> doc;
SignPayload currentPayload = {
  false,
  "SIN DATOS",
  "",
  "",
  0.0f,
  24,
  {46, 204, 113},
  false
};

unsigned long lastPollMs = 0;
unsigned long lastScrollMs = 0;
unsigned long wifiStartupDeadlineMs = 0;
uint8_t currentPage = 0;
int16_t scrollX = matrix.width();
String displayText = "SIN DATOS";
bool newDataReceived = false;
uint16_t currentTextColor = matrix.Color(255, 255, 255);

static const uint8_t FONT_SPACE[FONT_HEIGHT] PROGMEM = {0, 0, 0, 0, 0, 0};
static const uint8_t FONT_QUESTION[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x01, 0x06, 0x00, 0x04};
static const uint8_t FONT_DOT[FONT_HEIGHT] PROGMEM = {0, 0, 0, 0, 0, 0x04};
static const uint8_t FONT_COMMA[FONT_HEIGHT] PROGMEM = {0, 0, 0, 0, 0x04, 0x08};
static const uint8_t FONT_COLON[FONT_HEIGHT] PROGMEM = {0, 0x04, 0, 0, 0x04, 0};
static const uint8_t FONT_DASH[FONT_HEIGHT] PROGMEM = {0, 0, 0x1F, 0, 0, 0};
static const uint8_t FONT_SLASH[FONT_HEIGHT] PROGMEM = {0x01, 0x02, 0x04, 0x08, 0x10, 0};
static const uint8_t FONT_0[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x13, 0x15, 0x19, 0x0E};
static const uint8_t FONT_1[FONT_HEIGHT] PROGMEM = {0x04, 0x0C, 0x04, 0x04, 0x04, 0x0E};
static const uint8_t FONT_2[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x01, 0x06, 0x08, 0x1F};
static const uint8_t FONT_3[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x02, 0x01, 0x11, 0x0E};
static const uint8_t FONT_4[FONT_HEIGHT] PROGMEM = {0x02, 0x06, 0x0A, 0x1F, 0x02, 0x02};
static const uint8_t FONT_5[FONT_HEIGHT] PROGMEM = {0x1F, 0x10, 0x1E, 0x01, 0x11, 0x0E};
static const uint8_t FONT_6[FONT_HEIGHT] PROGMEM = {0x06, 0x08, 0x1E, 0x11, 0x11, 0x0E};
static const uint8_t FONT_7[FONT_HEIGHT] PROGMEM = {0x1F, 0x01, 0x02, 0x04, 0x04, 0x04};
static const uint8_t FONT_8[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x0E, 0x11, 0x11, 0x0E};
static const uint8_t FONT_9[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x11, 0x0F, 0x01, 0x06};
static const uint8_t FONT_A[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11};
static const uint8_t FONT_B[FONT_HEIGHT] PROGMEM = {0x1E, 0x11, 0x1E, 0x11, 0x11, 0x1E};
static const uint8_t FONT_C[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x10, 0x10, 0x11, 0x0E};
static const uint8_t FONT_D[FONT_HEIGHT] PROGMEM = {0x1C, 0x12, 0x11, 0x11, 0x12, 0x1C};
static const uint8_t FONT_E[FONT_HEIGHT] PROGMEM = {0x1F, 0x10, 0x1E, 0x10, 0x10, 0x1F};
static const uint8_t FONT_F[FONT_HEIGHT] PROGMEM = {0x1F, 0x10, 0x1E, 0x10, 0x10, 0x10};
static const uint8_t FONT_G[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x10, 0x13, 0x11, 0x0F};
static const uint8_t FONT_H[FONT_HEIGHT] PROGMEM = {0x11, 0x11, 0x1F, 0x11, 0x11, 0x11};
static const uint8_t FONT_I[FONT_HEIGHT] PROGMEM = {0x0E, 0x04, 0x04, 0x04, 0x04, 0x0E};
static const uint8_t FONT_J[FONT_HEIGHT] PROGMEM = {0x03, 0x01, 0x01, 0x01, 0x11, 0x0E};
static const uint8_t FONT_K[FONT_HEIGHT] PROGMEM = {0x11, 0x12, 0x1C, 0x12, 0x11, 0x11};
static const uint8_t FONT_L[FONT_HEIGHT] PROGMEM = {0x10, 0x10, 0x10, 0x10, 0x10, 0x1F};
static const uint8_t FONT_M[FONT_HEIGHT] PROGMEM = {0x11, 0x1B, 0x15, 0x11, 0x11, 0x11};
static const uint8_t FONT_N[FONT_HEIGHT] PROGMEM = {0x11, 0x19, 0x15, 0x13, 0x11, 0x11};
static const uint8_t FONT_O[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x11, 0x11, 0x11, 0x0E};
static const uint8_t FONT_P[FONT_HEIGHT] PROGMEM = {0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10};
static const uint8_t FONT_Q[FONT_HEIGHT] PROGMEM = {0x0E, 0x11, 0x11, 0x15, 0x12, 0x0D};
static const uint8_t FONT_R[FONT_HEIGHT] PROGMEM = {0x1E, 0x11, 0x11, 0x1E, 0x12, 0x11};
static const uint8_t FONT_S[FONT_HEIGHT] PROGMEM = {0x0F, 0x10, 0x0E, 0x01, 0x11, 0x0E};
static const uint8_t FONT_T[FONT_HEIGHT] PROGMEM = {0x1F, 0x04, 0x04, 0x04, 0x04, 0x04};
static const uint8_t FONT_U[FONT_HEIGHT] PROGMEM = {0x11, 0x11, 0x11, 0x11, 0x11, 0x0E};
static const uint8_t FONT_V[FONT_HEIGHT] PROGMEM = {0x11, 0x11, 0x11, 0x11, 0x0A, 0x04};
static const uint8_t FONT_W[FONT_HEIGHT] PROGMEM = {0x11, 0x11, 0x11, 0x15, 0x1B, 0x11};
static const uint8_t FONT_X[FONT_HEIGHT] PROGMEM = {0x11, 0x0A, 0x04, 0x04, 0x0A, 0x11};
static const uint8_t FONT_Y[FONT_HEIGHT] PROGMEM = {0x11, 0x0A, 0x04, 0x04, 0x04, 0x04};
static const uint8_t FONT_Z[FONT_HEIGHT] PROGMEM = {0x1F, 0x02, 0x04, 0x08, 0x10, 0x1F};

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

uint16_t payloadColor(const SignPayload& payload) {
  if (payload.hasRemoteColor) {
    return matrix.Color(payload.color[0], payload.color[1], payload.color[2]);
  }
  return colorForPm10(payload.pm10);
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
  if (wifiStartupDeadlineMs && millis() < wifiStartupDeadlineMs) {
    return false;
  }

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

  payload.hasRemoteColor = false;
  if (!display["color"].isNull()) {
    JsonVariant color = display["color"];
    if (color.is<JsonArray>() && color.size() >= 3) {
      payload.color[0] = (uint8_t)(color[0] | 255);
      payload.color[1] = (uint8_t)(color[1] | 255);
      payload.color[2] = (uint8_t)(color[2] | 255);
      payload.hasRemoteColor = true;
    } else if (color.is<JsonObject>()) {
      payload.color[0] = (uint8_t)(color["r"] | color["red"] | 255);
      payload.color[1] = (uint8_t)(color["g"] | color["green"] | 255);
      payload.color[2] = (uint8_t)(color["b"] | color["blue"] | 255);
      payload.hasRemoteColor = true;
    }
  }

  currentTextColor = payloadColor(payload);
  payload.ok = true;

  Serial.print("[json] headline=");
  Serial.println(payload.headline);
  Serial.print("[json] line1=");
  Serial.println(payload.line1);
  Serial.print("[json] line2=");
  Serial.println(payload.line2);
  Serial.print("[json] brightness=");
  Serial.println(payload.brightness);
  Serial.print("[json] pm10=");
  Serial.println(payload.pm10, 2);
  Serial.print("[json] remoteColor=");
  Serial.println(payload.hasRemoteColor ? "yes" : "no");
  return true;
}

bool fetchPayload(SignPayload& payload) {
  WiFiClient client;
  HTTPClient http;

  client.setTimeout(4000);
  http.setTimeout(4000);

  Serial.print("[http] GET ");
  Serial.println(API_URL);

  if (!http.begin(client, API_URL)) {
    Serial.println("[http] begin failed");
    return false;
  }

  int status = http.GET();
  Serial.print("[http] status=");
  Serial.println(status);

  if (status <= 0) {
    Serial.print("[http] error=");
    Serial.println(http.errorToString(status));
    http.end();
    return false;
  }

  if (status != HTTP_CODE_OK) {
    Serial.print("[http] body status no OK=");
    Serial.println(status);
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
  scrollX = matrix.width();
}

void nextPage() {
  currentPage = (currentPage + 1) % 3;
  resetScroll();
  displayText = pageText();
  newDataReceived = true;
  Serial.print("[page] ");
  Serial.println(currentPage);
}

void updateDisplayText() {
  displayText = pageText();
  newDataReceived = true;
}

bool samePayloadText(const SignPayload& a, const SignPayload& b) {
  return strcmp(a.headline, b.headline) == 0
    && strcmp(a.line1, b.line1) == 0
    && strcmp(a.line2, b.line2) == 0;
}

int16_t textWidthPx(const String& text) {
  return text.length() * CHAR_ADVANCE_PX;
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

void drawHorizontalChar(char ch, int16_t x) {
  const uint8_t* glyph = glyphForChar(ch);
  for (uint8_t row = 0; row < FONT_HEIGHT; row++) {
    uint8_t bits = pgm_read_byte(&glyph[row]);
    for (uint8_t col = 0; col < FONT_WIDTH; col++) {
      if (bits & (1 << (FONT_WIDTH - 1 - col))) {
        matrix.drawPixel(x + col, CHAR_TOP_MARGIN + row, currentTextColor);
      }
    }
  }
}

void drawText() {
  matrix.fillScreen(0);
  for (size_t i = 0; i < displayText.length(); i++) {
    drawHorizontalChar(displayText[i], scrollX + (i * CHAR_ADVANCE_PX));
  }

  matrix.show();
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("=== CARTEL NUBEMOVIL 64x8 ===");
  Serial.print("[boot] reset=");
  Serial.println(ESP.getResetReason());
  Serial.print("[boot] api=");
  Serial.println(API_URL);

  matrix.begin();
  matrix.setTextWrap(false);
  matrix.setBrightness(40);
  matrix.setTextColor(matrix.Color(255, 255, 255));
  matrix.fillScreen(0);
  matrix.show();
  Serial.println("[matrix] ready");
  wifiStartupDeadlineMs = millis() + WIFI_STARTUP_DELAY_MS;
  Serial.print("[wifi] esperando router ");
  Serial.print(WIFI_STARTUP_DELAY_MS);
  Serial.println(" ms");

  displayText = "BOOT";
  currentTextColor = matrix.Color(60, 140, 255);
  newDataReceived = true;

  SignPayload nextPayload = currentPayload;
  if (WiFi.status() == WL_CONNECTED && fetchPayload(nextPayload)) {
    currentPayload = nextPayload;
    matrix.setBrightness(currentPayload.brightness);
    Serial.println("[boot] payload inicial OK");
  } else {
    Serial.println("[boot] payload inicial KO");
  }

  updateDisplayText();
  resetScroll();
}

void loop() {
  if (!ensureWiFi()) {
    if (wifiStartupDeadlineMs && millis() < wifiStartupDeadlineMs) {
      displayText = "BOOT";
      currentTextColor = matrix.Color(60, 140, 255);
    } else {
      displayText = "WIFI";
      currentTextColor = matrix.Color(255, 120, 0);
    }
    newDataReceived = true;
    delay(250);
    return;
  }

  if (millis() - lastPollMs >= POLL_INTERVAL_MS || !currentPayload.ok) {
    lastPollMs = millis();
    SignPayload nextPayload = currentPayload;
    if (fetchPayload(nextPayload)) {
      bool textChanged = !samePayloadText(currentPayload, nextPayload);
      currentPayload = nextPayload;
      matrix.setBrightness(currentPayload.brightness);
      if (textChanged || !displayText.length()) {
        updateDisplayText();
      }
      Serial.println("[ok] payload actualizado");
      Serial.print("[ok] textChanged=");
      Serial.println(textChanged ? "yes" : "no");
    }
  }

  if (millis() - lastScrollMs >= SCROLL_DELAY_MS || newDataReceived) {
    if (newDataReceived) {
      matrix.fillScreen(0);
      scrollX = matrix.width();
      newDataReceived = false;
    }

    drawText();
    lastScrollMs = millis();

    if (--scrollX < -textWidthPx(displayText)) {
      nextPage();
    }
  }
}
