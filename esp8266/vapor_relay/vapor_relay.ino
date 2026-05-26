#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>

static const char* WIFI_SSID = "CHANGE_WIFI_SSID";
static const char* WIFI_PASSWORD = "CHANGE_WIFI_PASSWORD";
static const char* API_TOKEN = "CHANGE_API_TOKEN";
static const char* HOSTNAME = "nubemovil-vapor";

static const uint8_t RELAY_PIN = D6;
static const bool RELAY_ACTIVE_HIGH = true;
static const bool DEFAULT_RELAY_ON = false;
static const unsigned long SERIAL_BAUD = 115200;

ESP8266WebServer server(80);
bool relayOn = DEFAULT_RELAY_ON;
unsigned long lastStatusPrintMs = 0;

bool authorized() {
  String token = server.arg("token");
  if (token.length() == 0 && server.hasHeader("X-Api-Token")) {
    token = server.header("X-Api-Token");
  }
  return token == API_TOKEN;
}

void applyRelayState(bool enabled) {
  relayOn = enabled;
  const uint8_t outputLevel = (relayOn == RELAY_ACTIVE_HIGH) ? HIGH : LOW;
  digitalWrite(RELAY_PIN, outputLevel);
  Serial.printf("[vapor] relay=%s pin=D6\n", relayOn ? "ON" : "OFF");
}

void sendJsonState() {
  String body = "{";
  body += "\"ok\":true,";
  body += "\"device\":\"vapor_relay_1\",";
  body += "\"relay_on\":";
  body += relayOn ? "true" : "false";
  body += ",";
  body += "\"pin\":\"D6\",";
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

  String enabled = server.arg("enabled");
  enabled.toLowerCase();
  if (enabled != "true" && enabled != "false" && enabled != "1" && enabled != "0" && enabled != "on" && enabled != "off") {
    server.send(400, "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"bad_enabled\"}");
    return;
  }

  const bool nextState = (enabled == "true" || enabled == "1" || enabled == "on");
  applyRelayState(nextState);
  sendJsonState();
}

void handleRoot() {
  String html = "<!doctype html><html><head><meta charset=\"utf-8\"><title>NUBEMOVIL Vapor Relay</title></head><body>";
  html += "<h1>NUBEMOVIL Vapor Relay</h1>";
  html += "<p>Relay: ";
  html += relayOn ? "ON" : "OFF";
  html += "</p><p>IP: ";
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
    Serial.printf("[vapor] wifi=OK ip=%s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[vapor] wifi=FAIL");
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(2000);
  Serial.println();
  Serial.println("[vapor] boot");

  pinMode(RELAY_PIN, OUTPUT);
  applyRelayState(DEFAULT_RELAY_ON);
  ensureWiFi();

  server.on("/", HTTP_GET, handleRoot);
  server.on("/api/vapor/state", HTTP_GET, handleState);
  server.on("/api/vapor/set", HTTP_POST, handleSet);
  server.begin();
  Serial.println("[vapor] http=READY");
}

void loop() {
  ensureWiFi();
  server.handleClient();

  const unsigned long now = millis();
  if (now - lastStatusPrintMs >= 3000) {
    lastStatusPrintMs = now;
    Serial.print("[vapor] ip=");
    Serial.println(WiFi.localIP());
  }
}
