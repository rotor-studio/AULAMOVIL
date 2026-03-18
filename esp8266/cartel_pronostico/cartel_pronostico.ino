#include <Adafruit_GFX.h>
#include <Adafruit_NeoMatrix.h>
#include <Adafruit_NeoPixel.h>
#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>

const char* WIFI_SSID = "ROTORLINK";
const char* WIFI_PASSWORD = "";
const char* API_URL = "http://192.168.0.169:8000/api/sign/latest";

const unsigned long POLL_INTERVAL_MS = 30000;
const unsigned long WIFI_RETRY_MS = 10000;
const unsigned long ANIM_STEP_MS = 75;
const unsigned long PAGE_DURATION_MS = 4000;
const unsigned long ICON_SWAP_MS = 350;

const uint8_t MATRIX_PIN = D5;
const uint8_t MATRIX_WIDTH = 38;
const uint8_t MATRIX_HEIGHT = 8;

// Ajusta esta línea si tu matriz no es TOP + LEFT + ZIGZAG.
Adafruit_NeoMatrix matrix = Adafruit_NeoMatrix(
  MATRIX_WIDTH,
  MATRIX_HEIGHT,
  MATRIX_PIN,
  NEO_MATRIX_TOP + NEO_MATRIX_LEFT + NEO_MATRIX_ROWS + NEO_MATRIX_ZIGZAG,
  NEO_GRB + NEO_KHZ800
);

struct RgbColor {
  uint8_t r;
  uint8_t g;
  uint8_t b;
};

struct SignPayload {
  bool ok = false;
  String headline;
  String line1;
  String line2;
  String forecastLabel;
  String forecastIcon;
  String airBand;
  String airLabel;
  String effect;
  uint8_t brightness = 40;
  RgbColor color = {46, 204, 113};
};

SignPayload currentPayload;
unsigned long lastPollMs = 0;
unsigned long lastWifiRetryMs = 0;
unsigned long lastAnimMs = 0;
unsigned long pageStartedMs = 0;
unsigned long lastIconSwapMs = 0;
uint8_t currentPage = 0;
int16_t scrollX = MATRIX_WIDTH;
bool iconPhase = false;

bool ensureWiFi();
bool fetchPayload(SignPayload& payload);
bool parsePayload(const String& json, SignPayload& payload);
String clampLine(const String& input, size_t maxLen);
void logPayload(const SignPayload& payload);

void displayInit();
void displayShowBoot();
void displayShowWifiWaiting();
void displayShowError(const String& message);
void displayShowPayload(const SignPayload& payload);
void displayTick();
void resetDisplayAnimation();
String currentPageText();
uint16_t matrixColor(const RgbColor& color);
void drawForecastIcon(const String& iconName, uint16_t color, bool phase);
void drawCloud(uint16_t color);
void drawSun(uint16_t color);
void drawRain(uint16_t color, bool phase);
void drawStorm(uint16_t color, bool phase);

void setup() {
  Serial.begin(115200);
  delay(100);

  displayInit();
  displayShowBoot();

  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  if (strlen(WIFI_SSID) > 0) {
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
}

void loop() {
  displayTick();

  if (!ensureWiFi()) {
    displayShowWifiWaiting();
    delay(20);
    return;
  }

  const unsigned long now = millis();
  if (now - lastPollMs >= POLL_INTERVAL_MS) {
    lastPollMs = now;

    SignPayload nextPayload;
    if (fetchPayload(nextPayload)) {
      currentPayload = nextPayload;
      displayShowPayload(currentPayload);
      logPayload(currentPayload);
    } else if (!currentPayload.ok) {
      displayShowError("Sin datos");
    }
  }

  delay(20);
}

bool ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  const unsigned long now = millis();
  if (now - lastWifiRetryMs < WIFI_RETRY_MS) {
    return false;
  }

  lastWifiRetryMs = now;
  WiFi.disconnect();
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.println("[wifi] reconectando...");
  return false;
}

bool fetchPayload(SignPayload& payload) {
  WiFiClient client;
  HTTPClient http;

  client.setTimeout(6000);
  http.setTimeout(6000);

  if (!http.begin(client, API_URL)) {
    Serial.println("[http] no se pudo abrir la URL");
    return false;
  }

  http.addHeader("Accept", "application/json");
  const int status = http.GET();
  if (status != HTTP_CODE_OK) {
    Serial.printf("[http] estado inesperado: %d\n", status);
    http.end();
    return false;
  }

  const String body = http.getString();
  http.end();
  return parsePayload(body, payload);
}

bool parsePayload(const String& json, SignPayload& payload) {
  DynamicJsonDocument doc(4096);
  const DeserializationError err = deserializeJson(doc, json);
  if (err) {
    Serial.printf("[json] error: %s\n", err.c_str());
    return false;
  }

  JsonObject forecast = doc["forecast"];
  JsonObject air = doc["air"];
  JsonObject display = doc["display"];
  JsonObject color = air["color"];

  payload.headline = clampLine(display["headline"] | forecast["label"] | "Seguimiento local", 64);
  payload.line1 = clampLine(display["line1"] | forecast["summary"] | "Sin resumen", 64);
  payload.line2 = clampLine(display["line2"] | air["label"] | "Sin datos de aire", 64);
  payload.forecastLabel = String((const char*)(forecast["label"] | ""));
  payload.forecastIcon = String((const char*)(forecast["icon"] | "cloud"));
  payload.airBand = String((const char*)(air["band"] | "sin_dato"));
  payload.airLabel = String((const char*)(air["label"] | "Sin datos"));
  payload.effect = String((const char*)(display["effect"] | "solid"));
  payload.brightness = (uint8_t)(display["brightness"] | 40);
  payload.color.r = (uint8_t)(color["rgb"][0] | 156);
  payload.color.g = (uint8_t)(color["rgb"][1] | 163);
  payload.color.b = (uint8_t)(color["rgb"][2] | 175);
  payload.ok = true;
  return true;
}

String clampLine(const String& input, size_t maxLen) {
  if (input.length() <= maxLen) {
    return input;
  }
  return input.substring(0, maxLen - 3) + "...";
}

void logPayload(const SignPayload& payload) {
  Serial.println("----- cartel -----");
  Serial.printf("headline: %s\n", payload.headline.c_str());
  Serial.printf("line1: %s\n", payload.line1.c_str());
  Serial.printf("line2: %s\n", payload.line2.c_str());
  Serial.printf("icon: %s\n", payload.forecastIcon.c_str());
  Serial.printf("air: %s\n", payload.airBand.c_str());
  Serial.printf("color: %u,%u,%u\n", payload.color.r, payload.color.g, payload.color.b);
  Serial.println("------------------");
}

void displayInit() {
  matrix.begin();
  matrix.setTextWrap(false);
  matrix.setBrightness(32);
  matrix.setTextColor(matrix.Color(255, 255, 255));
  matrix.fillScreen(0);
  matrix.show();
}

void displayShowBoot() {
  matrix.fillScreen(0);
  matrix.setCursor(1, 5);
  matrix.setTextColor(matrix.Color(60, 140, 255));
  matrix.print("BOOT");
  matrix.show();
}

void displayShowWifiWaiting() {
  static unsigned long lastBlink = 0;
  static bool on = false;
  if (millis() - lastBlink < 350) {
    return;
  }
  lastBlink = millis();
  on = !on;
  matrix.fillScreen(0);
  if (on) {
  matrix.fillRect(0, 0, MATRIX_WIDTH, MATRIX_HEIGHT, matrix.Color(10, 25, 40));
  matrix.setCursor(2, 1);
    matrix.setTextColor(matrix.Color(255, 170, 0));
    matrix.print("WIFI");
  }
  matrix.show();
}

void displayShowError(const String& message) {
  matrix.fillScreen(0);
  matrix.setCursor(0, 0);
  matrix.setTextColor(matrix.Color(255, 60, 60));
  matrix.print("ERR");
  matrix.setCursor(0, 8);
  matrix.print(clampLine(message, 4));
  matrix.show();
}

void displayShowPayload(const SignPayload& payload) {
  matrix.setBrightness(payload.brightness);
  resetDisplayAnimation();
}

void resetDisplayAnimation() {
  currentPage = 0;
  pageStartedMs = millis();
  lastAnimMs = 0;
  lastIconSwapMs = 0;
  iconPhase = false;
  scrollX = MATRIX_WIDTH;
}

String currentPageText() {
  if (!currentPayload.ok) {
    return "Sin datos";
  }
  switch (currentPage) {
    case 0: return currentPayload.headline;
    case 1: return currentPayload.line1;
    default: return currentPayload.line2;
  }
}

uint16_t matrixColor(const RgbColor& color) {
  return matrix.Color(color.r, color.g, color.b);
}

void drawCloud(uint16_t color) {
  matrix.fillCircle(4, 4, 2, color);
  matrix.fillCircle(7, 4, 3, color);
  matrix.fillCircle(11, 5, 2, color);
  matrix.fillRect(3, 5, 10, 3, color);
}

void drawSun(uint16_t color) {
  matrix.fillCircle(8, 5, 3, color);
  matrix.drawLine(8, 0, 8, 2, color);
  matrix.drawLine(8, 8, 8, 10, color);
  matrix.drawLine(3, 5, 5, 5, color);
  matrix.drawLine(11, 5, 13, 5, color);
  matrix.drawLine(4, 1, 5, 2, color);
  matrix.drawLine(11, 2, 12, 1, color);
  matrix.drawLine(4, 9, 5, 8, color);
  matrix.drawLine(11, 8, 12, 9, color);
}

void drawRain(uint16_t color, bool phase) {
  drawCloud(matrix.Color(90, 150, 220));
  if (phase) {
  matrix.drawPixel(5, 6, color);
  matrix.drawPixel(8, 7, color);
  matrix.drawPixel(11, 6, color);
  } else {
  matrix.drawPixel(5, 6, color);
  matrix.drawPixel(8, 6, color);
  matrix.drawPixel(11, 7, color);
  }
}

void drawStorm(uint16_t color, bool phase) {
  drawCloud(matrix.Color(100, 120, 180));
  matrix.drawLine(8, 5, 6, 7, color);
  matrix.drawLine(6, 7, 9, 7, color);
  matrix.drawLine(9, 7, 8, 7, color);
  if (phase) {
    matrix.drawPixel(12, 6, matrix.Color(255, 180, 0));
  }
}

void drawForecastIcon(const String& iconName, uint16_t color, bool phase) {
  if (iconName == "sun") {
    drawSun(color);
  } else if (iconName == "rain") {
    drawRain(color, phase);
  } else if (iconName == "storm") {
    drawStorm(color, phase);
  } else {
    drawCloud(color);
  }
}

void displayTick() {
  if (!currentPayload.ok) {
    return;
  }

  const unsigned long now = millis();
  if (now - lastAnimMs < ANIM_STEP_MS) {
    return;
  }
  lastAnimMs = now;

  if (now - pageStartedMs >= PAGE_DURATION_MS) {
    currentPage = (currentPage + 1) % 3;
    pageStartedMs = now;
    scrollX = MATRIX_WIDTH;
  }

  if (now - lastIconSwapMs >= ICON_SWAP_MS) {
    iconPhase = !iconPhase;
    lastIconSwapMs = now;
  }

  const String text = currentPageText();
  int16_t x1;
  int16_t y1;
  uint16_t w;
  uint16_t h;
  matrix.getTextBounds(text, 0, 0, &x1, &y1, &w, &h);

  matrix.fillScreen(0);
  drawForecastIcon(currentPayload.forecastIcon, matrixColor(currentPayload.color), iconPhase);

  matrix.setTextColor(matrixColor(currentPayload.color));
  matrix.setCursor(scrollX, 1);
  matrix.print(text);
  matrix.show();

  scrollX--;
  if (scrollX < -(int16_t)w - 2) {
    scrollX = MATRIX_WIDTH;
  }
}
