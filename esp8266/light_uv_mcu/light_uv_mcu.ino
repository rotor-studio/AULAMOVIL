#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_TSL2591.h>

// Replace these placeholders before flashing a device.
static const char* WIFI_SSID = "CHANGE_WIFI_SSID";
static const char* WIFI_PASSWORD = "CHANGE_WIFI_PASSWORD";

static const char* MQTT_HOST = "CHANGE_MQTT_HOST";
static const int MQTT_PORT = 1883;

static const uint8_t I2C_SDA_PIN = D2;
static const uint8_t I2C_SCL_PIN = D1;
static const uint8_t UV_PIN = A0;

static const unsigned long WIFI_TIMEOUT_MS = 15000;
static const unsigned long WIFI_RETRY_MS = 10000;
static const unsigned long MQTT_RETRY_MS = 5000;
static const unsigned long PUBLISH_MS = 1000;
static const unsigned long TSL_REINIT_MS = 60000;

WiFiClient espClient;
PubSubClient client(espClient);
Adafruit_TSL2591 tsl = Adafruit_TSL2591(2591);

bool tslOK = false;
unsigned long lastPublishMs = 0;
unsigned long lastWifiAttemptMs = 0;
unsigned long lastMqttAttemptMs = 0;
unsigned long lastTslInitMs = 0;
uint8_t consecutiveTslFailures = 0;

bool initTSL() {
  lastTslInitMs = millis();
  if (!tsl.begin()) {
    tslOK = false;
    Serial.println("[tsl] not detected");
    return false;
  }

  tsl.setGain(TSL2591_GAIN_MED);
  tsl.setTiming(TSL2591_INTEGRATIONTIME_100MS);
  tslOK = true;
  consecutiveTslFailures = 0;
  Serial.println("[tsl] ready");
  return true;
}

void connectWiFi() {
  const unsigned long now = millis();
  if (WiFi.status() == WL_CONNECTED || now - lastWifiAttemptMs < WIFI_RETRY_MS) {
    return;
  }

  lastWifiAttemptMs = now;
  Serial.print("[wifi] connecting to ");
  Serial.println(WIFI_SSID);

  WiFi.disconnect();
  delay(100);
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

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[wifi] connected ip=");
    Serial.println(WiFi.localIP());
  } else {
    Serial.print("[wifi] failed status=");
    Serial.println(WiFi.status());
  }
}

void connectMQTT() {
  const unsigned long now = millis();
  if (client.connected() || WiFi.status() != WL_CONNECTED || now - lastMqttAttemptMs < MQTT_RETRY_MS) {
    return;
  }

  lastMqttAttemptMs = now;
  const String clientId = "light-uv-esp8266-" + String(ESP.getChipId(), HEX);
  Serial.print("[mqtt] connecting as ");
  Serial.println(clientId);

  if (client.connect(clientId.c_str())) {
    Serial.println("[mqtt] connected");
  } else {
    Serial.print("[mqtt] failed rc=");
    Serial.println(client.state());
  }
}

float rawToVolts(int raw) {
  return raw * (3.3f / 1023.0f);
}

int readUvRawAverage(int samples = 20) {
  long sum = 0;
  for (int i = 0; i < samples; i++) {
    sum += analogRead(UV_PIN);
    delay(5);
    yield();
  }
  return (int)(sum / samples);
}

float readUvVoltageAverage(int samples = 20) {
  long sum = 0;
  for (int i = 0; i < samples; i++) {
    sum += analogRead(UV_PIN);
    delay(5);
    yield();
  }
  return rawToVolts((int)(sum / samples));
}

bool readLux(float* luxOut) {
  if (!tslOK) {
    return false;
  }

  const uint32_t lum = tsl.getFullLuminosity();
  const uint16_t ir = lum >> 16;
  const uint16_t full = lum & 0xFFFF;
  const float lux = tsl.calculateLux(full, ir);

  if ((full == 0xFFFF && ir == 0xFFFF) || isnan(lux) || isinf(lux)) {
    consecutiveTslFailures++;
    Serial.print("[tsl] invalid read full=");
    Serial.print(full);
    Serial.print(" ir=");
    Serial.print(ir);
    Serial.print(" lux=");
    Serial.println(lux);

    if (consecutiveTslFailures >= 3) {
      Serial.println("[tsl] reinit after repeated invalid reads");
      initTSL();
    }
    return false;
  }

  consecutiveTslFailures = 0;
  *luxOut = lux < 0 ? 0.0f : lux;
  return true;
}

void publishMetric(const char* topic, const String& payload) {
  if (!client.connected()) {
    return;
  }
  client.publish(topic, payload.c_str(), true);
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("=== LIGHT UV MCU ===");

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(100000);

  initTSL();

  client.setServer(MQTT_HOST, MQTT_PORT);
  connectWiFi();
  connectMQTT();
}

void loop() {
  connectWiFi();
  connectMQTT();

  if (client.connected()) {
    client.loop();
  }

  const unsigned long now = millis();
  if (tslOK && now - lastTslInitMs >= TSL_REINIT_MS) {
    Serial.println("[tsl] periodic refresh");
    initTSL();
  }

  if (now - lastPublishMs < PUBLISH_MS) {
    delay(20);
    return;
  }
  lastPublishMs = now;

  float lux = -1.0f;
  const bool luxValid = readLux(&lux);
  const int rawUv = readUvRawAverage(20);
  const float uvVoltage = readUvVoltageAverage(20);

  if (luxValid) {
    publishMetric("meteo/light_uv/lux", String(lux, 2));
  }
  publishMetric("meteo/light_uv/uv_raw", String(rawUv));
  publishMetric("meteo/light_uv/uv_voltage", String(uvVoltage, 3));

  String json = "{";
  if (luxValid) {
    json += "\"lux\":";
    json += String(lux, 2);
    json += ",";
  }
  json += "\"uv_raw\":";
  json += String(rawUv);
  json += ",\"uv_v\":";
  json += String(uvVoltage, 3);
  json += "}";
  publishMetric("meteo/light_uv/json", json);

  Serial.print("[publish] ");
  Serial.println(json);
}
