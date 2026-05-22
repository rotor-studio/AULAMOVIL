# MOVIL CLOUD

Sitio PHP mínimo para recibir datos e imágenes desde la Raspberry Pi y mostrar una onepage visual con la última imagen de cámara como fondo y los últimos datos en bloques simples.

## Estructura

- `public/index.php`
  - onepage principal
- `public/api/ingest.php`
  - endpoint receptor para la Pi
- `public/api/state.php`
  - devuelve el último estado guardado
- `public/api/frame.php`
  - sirve la última imagen recibida
- `storage/latest_state.json`
  - último estado persistido
- `storage/latest_frame.jpg`
  - último frame persistido

## Ejecutar en local

Para pruebas solo en este ordenador:

```bash
php -S 127.0.0.1:8080 -t public
```

Luego abrir:

```text
http://127.0.0.1:8080/
```

Para recibir datos desde la Pi por red local:

```powershell
./serve-local.ps1
```

Eso levanta PHP en `0.0.0.0:8080`.

En este PC no tengo permisos de administrador para abrir el firewall, así que para desarrollo local la forma más robusta es usar un túnel inverso SSH hacia la Pi:

```powershell
./open-pi-tunnel.ps1
```

Con ese túnel activo, la Pi puede enviar a:

```text
http://127.0.0.1:18080/api/ingest.php
```

## Token de ingesta

Editar:

- `config/config.php`

Cambiar:

- `ingest_token`

## Formato de ingesta desde la Pi

El endpoint receptor es:

```text
POST /api/ingest.php
```

Acepta tres modos:

1. `multipart/form-data`

- `token`
- `payload`
- `frame`

2. `application/json`

- `token`
- `title`
- `subtitle`
- `location`
- `metrics`
- `frame_base64` opcional

3. `application/x-www-form-urlencoded`

- `token`
- `title`
- `subtitle`
- `location`
- `metrics` como JSON string

`payload` o el cuerpo JSON pueden tener esta forma:

```json
{
  "title": "MOVIL CLOUD",
  "subtitle": "Aula Móvil en directo",
  "location": "Madrid",
  "metrics": [
    { "label": "Temperatura", "value": "21.5", "unit": "C" },
    { "label": "Humedad", "value": "57", "unit": "%" },
    { "label": "Presión", "value": "1017", "unit": "hPa" }
  ]
}
```

## Ejemplo de prueba manual

Con una imagen `frame.jpg` local y `multipart/form-data`:

```bash
curl -X POST "http://127.0.0.1:8080/api/ingest.php" ^
  -F "token=change-this-token" ^
  -F "payload={\"title\":\"MOVIL CLOUD\",\"subtitle\":\"Prueba local\",\"location\":\"Aula Movil\",\"metrics\":[{\"label\":\"Temperatura\",\"value\":\"21.5\",\"unit\":\"C\"}]}" ^
  -F "frame=@frame.jpg"
```

Con JSON puro:

```bash
curl -X POST "http://127.0.0.1:8080/api/ingest.php" ^
  -H "Content-Type: application/json" ^
  -d "{\"token\":\"change-this-token\",\"title\":\"MOVIL CLOUD\",\"subtitle\":\"Prueba local\",\"location\":\"Aula Movil\",\"metrics\":[{\"label\":\"Temperatura\",\"value\":\"21.5\",\"unit\":\"C\"}]}"
```

## Preparado para hosting PHP / WordPress

- No depende de extensiones raras
- No necesita base de datos
- Guarda estado en archivos
- Puede subirse como subcarpeta o adaptarse como plantilla PHP
