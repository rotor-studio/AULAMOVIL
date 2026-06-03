#include <Adafruit_GFX.h>
#include <Adafruit_NeoMatrix.h>
#include <Adafruit_NeoPixel.h>

#define MATRIX_PIN D4
#define MATRIX_WIDTH 32
#define MATRIX_HEIGHT 8

const unsigned long COLOR_INTERVAL_MS = 5000;
const uint8_t BRIGHTNESS = 50;

Adafruit_NeoMatrix matrix = Adafruit_NeoMatrix(
  MATRIX_WIDTH,
  MATRIX_HEIGHT,
  MATRIX_PIN,
  NEO_MATRIX_TOP + NEO_MATRIX_LEFT + NEO_MATRIX_COLUMNS + NEO_MATRIX_ZIGZAG,
  NEO_GRB + NEO_KHZ800
);

struct RgbColor {
  uint8_t r;
  uint8_t g;
  uint8_t b;
};

const RgbColor palette[] = {
  {0, 180, 255},   // azul vapor
  {0, 255, 160},   // verde agua
  {170, 80, 255},  // violeta
  {255, 110, 0},   // naranja
  {255, 40, 120},  // magenta
  {255, 255, 255}, // blanco
};

const size_t paletteCount = sizeof(palette) / sizeof(palette[0]);

unsigned long lastColorChangeMs = 0;
size_t colorIndex = 0;

void applyColor(const RgbColor& color) {
  matrix.fillScreen(matrix.Color(color.r, color.g, color.b));
  matrix.show();
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("vapor_color_32x8 boot");
  Serial.println("Matrix 32x8 en D4");

  matrix.begin();
  matrix.setBrightness(BRIGHTNESS);
  matrix.fillScreen(0);
  matrix.show();
  delay(50);

  applyColor(palette[colorIndex]);
  lastColorChangeMs = millis();
  Serial.println("Primer color aplicado");
}

void loop() {
  const unsigned long now = millis();
  if (now - lastColorChangeMs < COLOR_INTERVAL_MS) {
    delay(5);
    return;
  }

  colorIndex = (colorIndex + 1) % paletteCount;
  applyColor(palette[colorIndex]);
  lastColorChangeMs = now;
  Serial.print("Cambio de color: ");
  Serial.println(colorIndex);
  delay(5);
}
