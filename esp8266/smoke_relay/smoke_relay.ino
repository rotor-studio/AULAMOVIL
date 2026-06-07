#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>

static const char* WIFI_SSID = "NUBEMOVIL";
static const char* WIFI_PASSWORD = "100*Nubemovil001";
static const char* API_TOKEN = "666999";
static const char* HOSTNAME = "nubemovil-humo";

static const uint8_t RELAY_PIN = D5;
static const bool RELAY_ACTIVE_HIGH = true;
static const bool DEFAULT_RELAY_ON = false;
static const unsigned long SERIAL_BAUD = 115200;
static const unsigned long WIFI_RETRY_INTERVAL_MS = 10000;
static const unsigned long WIFI_CONNECT_TIMEOUT_MS = 20000;

ESP8266WebServer server(80);
bool relayOn = DEFAULT_RELAY_ON;
unsigned long lastStatusPrintMs = 0;
unsigned long lastWiFiAttemptMs = 0;
bool wifiAttemptInProgress = false;

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

void applyRelayState(bool enabled) {
  relayOn = enabled;
  digitalWrite(RELAY_PIN, relayLevel(relayOn));
  Serial.printf("[smoke] relay=%s pin=D5\n", relayOn ? "ON" : "OFF");
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

void sendJsonState() {
  String body = "{";
  body += "\"ok\":true,";
  body += "\"device\":\"smoke_relay_1\",";
  body += "\"relay_on\":";
  body += relayOn ? "true" : "false";
  body += ",";
  body += "\"pin\":\"D5\",";
  body += "\"ip\":\"";
  body += WiFi.localIP().toString();
  body += "\"";
  body += "}";
  server.send(200, "application/json; charset=utf-8", body);
}

void handleState() {
  if (!authorized()) {
    server.send(401, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"unauthorized\"}");
    return;
  }
  sendJsonState();
}

void handleSet() {
  if (!authorized()) {
    server.send(401, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"unauthorized\"}");
    return;
  }

  bool nextState = false;
  if (!parseEnabledArg(&nextState)) {
    server.send(400, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"bad_enabled\"}");
    return;
  }

  applyRelayState(nextState);
  sendJsonState();
}

void handleRoot() {
  String html = "<!doctype html><html><head><meta charset=\"utf-8\"><title>NUBEMOVIL Humo Relay</title></head><body>";
  html += "<h1>NUBEMOVIL Humo Relay</h1>";
  html += "<p>Relay: ";
  html += relayOn ? "ON" : "OFF";
  html += " (D5)</p><p>IP: ";
  html += WiFi.localIP().toString();
  html += "</p></body></html>";
  server.send(200, "text/html; charset=utf-8", html);
}

void startWiFiAttempt() {
  WiFi.mode(WIFI_STA);
  WiFi.hostname(HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWiFiAttemptMs = millis();
  wifiAttemptInProgress = true;
  Serial.println("[wifi] CONNECTING");
}

void ensureWiFi() {
  const wl_status_t status = WiFi.status();
  const unsigned long now = millis();

  if (status == WL_CONNECTED) {
    if (wifiAttemptInProgress) {
      wifiAttemptInProgress = false;
      Serial.printf("[wifi] OK ip=%s\n", WiFi.localIP().toString().c_str());
    }
    return;
  }

  if (!wifiAttemptInProgress) {
    if (lastWiFiAttemptMs == 0 || now - lastWiFiAttemptMs >= WIFI_RETRY_INTERVAL_MS) {
      startWiFiAttempt();
    }
    return;
  }

  if (now - lastWiFiAttemptMs >= WIFI_CONNECT_TIMEOUT_MS) {
    wifiAttemptInProgress = false;
    Serial.println("[wifi] FAIL");
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(2000);
  Serial.println();
  Serial.println("[smoke] boot");

  pinMode(RELAY_PIN, OUTPUT);
  applyRelayState(DEFAULT_RELAY_ON);
  WiFi.persistent(false);
  ensureWiFi();

  server.on("/", HTTP_GET, handleRoot);
  server.on("/api/smoke/state", HTTP_GET, handleState);
  server.on("/api/smoke/set", HTTP_POST, handleSet);
  server.begin();
  Serial.println("[smoke] http=READY");
}

void loop() {
  server.handleClient();
  ensureWiFi();

  const unsigned long now = millis();
  if (now - lastStatusPrintMs >= 3000) {
    lastStatusPrintMs = now;
    Serial.print("[smoke] ip=");
    Serial.print(WiFi.localIP());
    Serial.print(" relay=");
    Serial.println(relayOn ? "ON" : "OFF");
  }
}
