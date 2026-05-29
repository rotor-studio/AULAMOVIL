#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>

static const char* WIFI_SSID = "ROTORLINK";
static const char* WIFI_PASSWORD = "100*Rotor001";
static const char* API_TOKEN = "666999";
static const char* HOSTNAME = "nubemovil-vapor";

static const uint8_t VAPOR_RELAY_PIN = D6;
static const uint8_t FAN_RELAY_PIN = D2;
static const bool RELAY_ACTIVE_HIGH = true;
static const bool DEFAULT_VAPOR_ON = false;
static const bool DEFAULT_FAN_ON = false;
static const unsigned long SERIAL_BAUD = 115200;

ESP8266WebServer server(80);
bool vaporRelayOn = DEFAULT_VAPOR_ON;
bool fanRelayOn = DEFAULT_FAN_ON;
unsigned long lastStatusPrintMs = 0;

bool authorized() {
  String token = server.arg("token");
  if (token.length() == 0 && server.hasHeader("X-Api-Token")) {
    token = server.header("X-Api-Token");
  }
  return token == API_TOKEN;
}

uint8_t relayLevel(bool enabled) {
  return (enabled == RELAY_ACTIVE_HIGH) ? HIGH : LOW;
}

void applyVaporState(bool enabled) {
  vaporRelayOn = enabled;
  digitalWrite(VAPOR_RELAY_PIN, relayLevel(vaporRelayOn));
  Serial.printf("[vapor] relay=%s pin=D6\n", vaporRelayOn ? "ON" : "OFF");
}

void applyFanState(bool enabled) {
  fanRelayOn = enabled;
  digitalWrite(FAN_RELAY_PIN, relayLevel(fanRelayOn));
  Serial.printf("[fan] relay=%s pin=D2\n", fanRelayOn ? "ON" : "OFF");
}

bool parseEnabledArg(bool* nextState) {
  String enabled = server.arg("enabled");
  enabled.toLowerCase();
  if (enabled != "true" && enabled != "false" && enabled != "1" && enabled != "0" && enabled != "on" && enabled != "off") {
    return false;
  }
  *nextState = (enabled == "true" || enabled == "1" || enabled == "on");
  return true;
}

void sendVaporState() {
  String body = "{";
  body += "\"ok\":true,";
  body += "\"device\":\"vapor_relay_1\",";
  body += "\"relay_on\":";
  body += vaporRelayOn ? "true" : "false";
  body += ",";
  body += "\"pin\":\"D6\",";
  body += "\"ip\":\"";
  body += WiFi.localIP().toString();
  body += "\"";
  body += "}";
  server.send(200, "application/json; charset=utf-8", body);
}

void sendFanState() {
  String body = "{";
  body += "\"ok\":true,";
  body += "\"device\":\"fan_relay_1\",";
  body += "\"relay_on\":";
  body += fanRelayOn ? "true" : "false";
  body += ",";
  body += "\"pin\":\"D2\",";
  body += "\"ip\":\"";
  body += WiFi.localIP().toString();
  body += "\"";
  body += "}";
  server.send(200, "application/json; charset=utf-8", body);
}

void handleVaporState() {
  if (!authorized()) {
    server.send(401, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"unauthorized\"}");
    return;
  }
  sendVaporState();
}

void handleFanState() {
  if (!authorized()) {
    server.send(401, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"unauthorized\"}");
    return;
  }
  sendFanState();
}

void handleVaporSet() {
  if (!authorized()) {
    server.send(401, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"unauthorized\"}");
    return;
  }

  bool nextState = false;
  if (!parseEnabledArg(&nextState)) {
    server.send(400, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"bad_enabled\"}");
    return;
  }

  applyVaporState(nextState);
  sendVaporState();
}

void handleFanSet() {
  if (!authorized()) {
    server.send(401, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"unauthorized\"}");
    return;
  }

  bool nextState = false;
  if (!parseEnabledArg(&nextState)) {
    server.send(400, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"bad_enabled\"}");
    return;
  }

  applyFanState(nextState);
  sendFanState();
}

void handleRoot() {
  String html = "<!doctype html><html><head><meta charset=\"utf-8\"><title>NUBEMOVIL Relay Control</title></head><body>";
  html += "<h1>NUBEMOVIL Relay Control</h1>";
  html += "<p>Vapor: ";
  html += vaporRelayOn ? "ON" : "OFF";
  html += " (D6)</p><p>Ventilador: ";
  html += fanRelayOn ? "ON" : "OFF";
  html += " (D2)</p><p>IP: ";
  html += WiFi.localIP().toString();
  html += "</p></body></html>";
  server.send(200, "text/html; charset=utf-8", html);
}

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  WiFi.mode(WIFI_STA);
  WiFi.hostname(HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 20000) {
    delay(250);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] OK ip=%s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[wifi] FAIL");
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(2000);
  Serial.println();
  Serial.println("[relay] boot");

  pinMode(VAPOR_RELAY_PIN, OUTPUT);
  pinMode(FAN_RELAY_PIN, OUTPUT);
  applyVaporState(DEFAULT_VAPOR_ON);
  applyFanState(DEFAULT_FAN_ON);
  ensureWiFi();

  server.on("/", HTTP_GET, handleRoot);
  server.on("/api/vapor/state", HTTP_GET, handleVaporState);
  server.on("/api/vapor/set", HTTP_POST, handleVaporSet);
  server.on("/api/fan/state", HTTP_GET, handleFanState);
  server.on("/api/fan/set", HTTP_POST, handleFanSet);
  server.begin();
  Serial.println("[relay] http=READY");
}

void loop() {
  ensureWiFi();
  server.handleClient();

  const unsigned long now = millis();
  if (now - lastStatusPrintMs >= 3000) {
    lastStatusPrintMs = now;
    Serial.print("[relay] ip=");
    Serial.print(WiFi.localIP());
    Serial.print(" vapor=");
    Serial.print(vaporRelayOn ? "ON" : "OFF");
    Serial.print(" fan=");
    Serial.println(fanRelayOn ? "ON" : "OFF");
  }
}
