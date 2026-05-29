#include <Adafruit_GFX.h>
#include <Adafruit_NeoMatrix.h>
#include <Adafruit_NeoPixel.h>
#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>

#ifndef PSTR
#define PSTR
#endif

const char* WIFI_SSID = "ROTORLINK";
const char* WIFI_PASSWORD = "100*Rotor001";
const char* API_URL = "http://192.168.0.169:8000/api/sign/latest";

const unsigned long WIFI_TIMEOUT_MS = 15000;
const unsigned long POLL_INTERVAL_MS = 30000;
const unsigned long SCROLL_DELAY_MS = 160;
const uint8_t CHAR_HEIGHT = 8;
const int8_t CHAR_X = 1;

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

unsigned long lastPollMs = 0;
unsigned long lastScrollMs = 0;
uint8_t currentPage = 0;
int16_t scrollY = 0;
String displayText = "SIN DATOS";
bool newDataReceived = false;
uint16_t currentTextColor = matrix.Color(255, 255, 255);

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
  scrollY = matrix.height();
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

int16_t verticalTextHeight() {
  return displayText.length() * CHAR_HEIGHT;
}

void drawText() {
  matrix.fillScreen(0);
  matrix.setTextColor(currentTextColor);
  for (uint16_t i = 0; i < displayText.length(); i++) {
    char c[2] = { displayText.charAt(i), '\0' };
    matrix.setCursor(CHAR_X, scrollY + (i * CHAR_HEIGHT));
    matrix.print(c);
  }
  matrix.show();
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("=== CARTEL NUBEMOVIL 64x8 VERTICAL ===");
  Serial.print("[boot] reset=");
  Serial.println(ESP.getResetReason());
  Serial.print("[boot] api=");
  Serial.println(API_URL);

  matrix.begin();
  matrix.setRotation(1);
  matrix.setTextWrap(false);
  matrix.setBrightness(40);
  matrix.setTextColor(matrix.Color(255, 255, 255));
  matrix.fillScreen(0);
  matrix.show();
  Serial.println("[matrix] ready");

  connectWiFi();

  SignPayload nextPayload = currentPayload;
  if (WiFi.status() == WL_CONNECTED && fetchPayload(nextPayload)) {
    currentPayload = nextPayload;
    matrix.setBrightness(currentPayload.brightness);
    currentTextColor = colorForPm10(currentPayload.pm10);
    Serial.println("[boot] payload inicial OK");
  } else {
    Serial.println("[boot] payload inicial KO");
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
      Serial.println("[ok] payload actualizado");
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
