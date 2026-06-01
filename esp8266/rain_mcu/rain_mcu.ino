#include <ESP8266WiFi.h>
#include <PubSubClient.h>

// Replace these placeholders before flashing a device.
  const char* WIFI_SSID = "CHANGE_WIFI_SSID";
  const char* WIFI_PASSWORD = "CHANGE_WIFI_PASSWORD";
  const char* MQTT_HOST = "192.168.1.109";
  const int MQTT_PORT = 1883;

// Keep the published topics stable so the collector continues to ingest data.
static const char* TOPIC_TIPS_TOTAL = "meteo/rain/tips_total";
static const char* TOPIC_MM_TOTAL = "meteo/rain/mm_total";
static const char* TOPIC_MM_INTERVAL = "meteo/rain/mm_interval";
static const char* TOPIC_RATE_MMH = "meteo/rain/rate_mmh";
static const char* TOPIC_LAST_TIP_MS = "meteo/rain/last_tip_ms";
static const char* TOPIC_SINCE_LAST_TIP = "meteo/rain/since_last_tip_ms";
static const char* TOPIC_JSON = "meteo/rain/json";

static const uint8_t HALL_PIN = D5;
static const uint32_t DEBOUNCE_MS = 150;
static const float MM_PER_TIP = 0.64f;

static const unsigned long SERIAL_BAUD = 115200;
static const unsigned long WIFI_STARTUP_DELAY_MS = 45000;
static const unsigned long WIFI_TIMEOUT_MS = 15000;
static const unsigned long WIFI_RETRY_MS = 10000;
static const unsigned long MQTT_RETRY_MS = 5000;
static const unsigned long PUBLISH_MS = 2000;

volatile uint32_t tipCount = 0;
volatile uint32_t lastTipMs = 0;

uint32_t lastPublishedTips = 0;
unsigned long lastPublishMs = 0;
unsigned long lastWifiAttemptMs = 0;
unsigned long lastMqttAttemptMs = 0;
unsigned long wifiStartupDeadlineMs = WIFI_STARTUP_DELAY_MS;

WiFiClient espClient;
PubSubClient client(espClient);

ICACHE_RAM_ATTR void onTip() {
  const uint32_t now = millis();
  if (now - lastTipMs >= DEBOUNCE_MS) {
    tipCount++;
    lastTipMs = now;
  }
}

bool connectWiFi() {
  Serial.print("[wifi] connecting to ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  const unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < WIFI_TIMEOUT_MS) {
    delay(250);
    Serial.print(".");
    yield();
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.print("[wifi] failed status=");
    Serial.println(WiFi.status());
    return false;
  }

  Serial.print("[wifi] connected ip=");
  Serial.println(WiFi.localIP());
  return true;
}

bool ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  const unsigned long now = millis();
  if (wifiStartupDeadlineMs != 0 && now < wifiStartupDeadlineMs) {
    return false;
  }

  if (now - lastWifiAttemptMs < WIFI_RETRY_MS) {
    return false;
  }
  lastWifiAttemptMs = now;
  return connectWiFi();
}

bool ensureMQTT() {
  if (client.connected()) {
    return true;
  }

  if (!ensureWiFi()) {
    return false;
  }

  const unsigned long now = millis();
  if (now - lastMqttAttemptMs < MQTT_RETRY_MS) {
    return false;
  }
  lastMqttAttemptMs = now;

  const String clientId = "rain-esp8266-" + String(ESP.getChipId(), HEX);
  Serial.print("[mqtt] connecting as ");
  Serial.println(clientId);

  if (client.connect(clientId.c_str())) {
    Serial.println("[mqtt] connected");
    return true;
  }

  Serial.print("[mqtt] failed rc=");
  Serial.println(client.state());
  return false;
}

void publishRetained(const char* topic, const String& value) {
  client.publish(topic, value.c_str(), true);
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(200);
  Serial.println();
  Serial.println("[rain] boot");

  pinMode(HALL_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(HALL_PIN), onTip, FALLING);

  client.setServer(MQTT_HOST, MQTT_PORT);

  Serial.println("[rain] tipping bucket ready");
  Serial.print("[rain] mm_per_tip=");
  Serial.println(MM_PER_TIP, 2);
}

void loop() {
  ensureWiFi();
  if (ensureMQTT()) {
    client.loop();
  }

  const unsigned long now = millis();
  if (now - lastPublishMs < PUBLISH_MS) {
    delay(10);
    return;
  }

  const unsigned long elapsedMs = lastPublishMs == 0 ? PUBLISH_MS : (now - lastPublishMs);
  lastPublishMs = now;

  uint32_t tipsCopy;
  uint32_t lastTipCopy;
  noInterrupts();
  tipsCopy = tipCount;
  lastTipCopy = lastTipMs;
  interrupts();

  const float mmTotal = tipsCopy * MM_PER_TIP;
  const uint32_t intervalTips = tipsCopy - lastPublishedTips;
  const float mmInterval = intervalTips * MM_PER_TIP;
  lastPublishedTips = tipsCopy;

  float rateMmh = 0.0f;
  if (elapsedMs > 0) {
    rateMmh = mmInterval * (3600000.0f / elapsedMs);
  }

  uint32_t sinceLastTipMs = 0;
  if (lastTipCopy > 0) {
    sinceLastTipMs = now - lastTipCopy;
  }

  String json = "{";
  json += "\"tips_total\":" + String(tipsCopy);
  json += ",\"mm_total\":" + String(mmTotal, 2);
  json += ",\"mm_interval\":" + String(mmInterval, 2);
  json += ",\"rate_mmh\":" + String(rateMmh, 2);
  json += ",\"last_tip_ms\":" + String(lastTipCopy);
  json += ",\"since_last_tip_ms\":" + String(sinceLastTipMs);
  json += "}";

  if (client.connected()) {
    publishRetained(TOPIC_TIPS_TOTAL, String(tipsCopy));
    publishRetained(TOPIC_MM_TOTAL, String(mmTotal, 2));
    publishRetained(TOPIC_MM_INTERVAL, String(mmInterval, 2));
    publishRetained(TOPIC_RATE_MMH, String(rateMmh, 2));
    publishRetained(TOPIC_LAST_TIP_MS, String(lastTipCopy));
    publishRetained(TOPIC_SINCE_LAST_TIP, String(sinceLastTipMs));
    publishRetained(TOPIC_JSON, json);
  }

  Serial.print("[rain] tips=");
  Serial.print(tipsCopy);
  Serial.print(" total_mm=");
  Serial.print(mmTotal, 2);
  Serial.print(" interval_mm=");
  Serial.print(mmInterval, 2);
  Serial.print(" rate_mmh=");
  Serial.print(rateMmh, 2);
  Serial.print(" since_last_tip_ms=");
  Serial.println(sinceLastTipMs);
}
