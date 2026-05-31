#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>

  const char* WIFI_SSID = "CHANGE_WIFI_SSID";
  const char* WIFI_PASSWORD = "CHANGE_WIFI_PASSWORD";
  const char* MQTT_HOST = "192.168.1.109";
  const int MQTT_PORT = 1883;

WiFiClient espClient;
PubSubClient client(espClient);

// ===== BME280 =====
Adafruit_BME280 bme;
bool bmeOK = false;

// ===== MUX 4067 =====
const int S0 = D5;
const int S1 = D6;
const int S2 = D7;
const int S3 = D8;

const uint8_t CH_SPEED = 0;   // C0
const uint8_t CH_DIR   = 1;   // C1

// ===== Shunt + rangos =====
const float SHUNT_OHMS = 100.0;

// Velocidad: 4–20mA => 0–30 m/s
const float I_MIN_MA = 4.0;
const float I_MAX_MA = 20.0;
const float WIND_MAX_MS = 30.0;

// Dirección: 4–20mA => 0–360°
const float DIR_MAX_DEG = 360.0;

const unsigned long PUBLISH_MS = 1000;
unsigned long lastPub = 0;

// ---------- Helpers ----------
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) delay(300);
}

void connectMQTT() {
  while (!client.connected()) {
    String clientId = "wind-esp8266-" + String(ESP.getChipId(), HEX);
    if (!client.connect(clientId.c_str())) delay(1000);
  }
}

void muxSelect(uint8_t ch) {
  digitalWrite(S0, (ch >> 0) & 1);
  digitalWrite(S1, (ch >> 1) & 1);
  digitalWrite(S2, (ch >> 2) & 1);
  digitalWrite(S3, (ch >> 3) & 1);
  delayMicroseconds(300);
}

int readMuxRaw(uint8_t ch) {
  muxSelect(ch);
  analogRead(A0);
  delayMicroseconds(300);
  return analogRead(A0);
}

float rawToVolts(int raw) {
  return raw * (3.3f / 1023.0f);
}

float voltsToMilliAmps(float v) {
  return (v / SHUNT_OHMS) * 1000.0f;
}

float normalizeDeg(float deg) {
  while (deg < 0) deg += 360.0f;
  while (deg >= 360.0f) deg -= 360.0f;
  return deg;
}

const char* degToCardinal(float deg) {
  if (deg < 22.5)  return "N";
  if (deg < 67.5)  return "NE";
  if (deg < 112.5) return "E";
  if (deg < 157.5) return "SE";
  if (deg < 202.5) return "S";
  if (deg < 247.5) return "SW";
  if (deg < 292.5) return "W";
  if (deg < 337.5) return "NW";
  return "N";
}

// ---------- Setup ----------
void setup() {
  Serial.begin(115200);

  pinMode(S0, OUTPUT);
  pinMode(S1, OUTPUT);
  pinMode(S2, OUTPUT);
  pinMode(S3, OUTPUT);

  // ===== I2C =====
  Wire.begin(D2, D1);

  // --- BME280 ---
  if (bme.begin(0x76)) {
    bmeOK = true;
    Serial.println("BME280 OK (0x76)");
  } else if (bme.begin(0x77)) {
    bmeOK = true;
    Serial.println("BME280 OK (0x77)");
  } else {
    Serial.println("BME280 NO detectado");
  }

  connectWiFi();
  client.setServer(MQTT_HOST, MQTT_PORT);
  connectMQTT();
}

// ---------- Loop ----------
void loop() {
  if (!client.connected()) connectMQTT();
  client.loop();

  if (millis() - lastPub < PUBLISH_MS) return;
  lastPub = millis();

  // ===== Velocidad =====
  int raw_speed = readMuxRaw(CH_SPEED);
  float v_speed = rawToVolts(raw_speed);
  float i_speed_mA = voltsToMilliAmps(v_speed);

  float wind_ms = (i_speed_mA - I_MIN_MA) * (WIND_MAX_MS / (I_MAX_MA - I_MIN_MA));
  if (wind_ms < 0) wind_ms = 0;

  // ===== Dirección =====
  int raw_dir = readMuxRaw(CH_DIR);
  float v_dir = rawToVolts(raw_dir);
  float i_dir_mA = voltsToMilliAmps(v_dir);

  float dir_deg = (i_dir_mA - I_MIN_MA) * (DIR_MAX_DEG / (I_MAX_MA - I_MIN_MA));
  dir_deg = normalizeDeg(dir_deg);

  const char* dir_card = degToCardinal(dir_deg);

  // ===== BME =====
  float temp = NAN, hum = NAN, pres = NAN;
  if (bmeOK) {
    temp = bme.readTemperature();
    hum  = bme.readHumidity();
    pres = bme.readPressure() / 100.0;
  }

  // ===== MQTT =====
  client.publish("meteo/wind/speed_ms", String(wind_ms, 2).c_str(), true);
  client.publish("meteo/wind/direction_deg", String(dir_deg, 1).c_str(), true);
  client.publish("meteo/wind/direction_cardinal", dir_card, true);

  if (bmeOK) {
    client.publish("meteo/env/temperature_c", String(temp, 2).c_str(), true);
    client.publish("meteo/env/humidity", String(hum, 2).c_str(), true);
    client.publish("meteo/env/pressure_hpa", String(pres, 2).c_str(), true);
  }

  // ===== JSON =====
  String json = "{";
  json += "\"speed_ms\":" + String(wind_ms, 2);
  json += ",\"dir_deg\":" + String(dir_deg, 1);
  json += ",\"dir_card\":\"" + String(dir_card) + "\"";

  if (bmeOK) {
    json += ",\"temp\":" + String(temp, 2);
    json += ",\"hum\":" + String(hum, 2);
    json += ",\"pres\":" + String(pres, 2);
  }

  json += "}";
  client.publish("meteo/wind/json", json.c_str(), true);

  Serial.println(json);
}
