# NUBEMOVIL Player handoff

## Branch and repository

- Local branch: `Player`
- Remote repository: `git@github.com:rotor-studio/AULAMOVIL.git`
- Remote branch: `Player`
- Pull request URL: `https://github.com/rotor-studio/AULAMOVIL/pull/new/Player`

## Online deployment

- Public URL: `https://www.rotor-studio.net/nubemovil/player/`
- FTP path: `/www.rotor-studio.net/nubemovil/player`
- Old uppercase path `/NUBEMOVIL/PLAYER/` was removed and now returns `404`.

## Local runtime

From repo root:

```bash
PLAYER_PASSWORD=player-local php -S 127.0.0.1:8787 -t player/public
```

Local URL:

```text
http://127.0.0.1:8787/
```

## Private access

- Local default password fallback: `player-local`
- Online password is not stored in Git.
- Online password is stored locally in ignored file:

```text
player/var/ONLINE_PASSWORD.txt
```

The online Apache/PHP deployment uses `PLAYER_PASSWORD_HASH` from:

```text
player/public/.htaccess
```

## Storage

- SQLite database path locally:

```text
player/var/player.sqlite
```

- Online database path:

```text
/www.rotor-studio.net/nubemovil/player/var/player.sqlite
```

The `var` directory is protected by:

```text
player/var/.htaccess
```

Direct HTTP access to `var/player.sqlite` should return `403`.

## Current features

- Public route viewer with Leaflet.
- Sensor selector and timeline play controls.
- Playback speed selector: `x1`, `x3`, `x5`, `x10`.
- Temperature emoji toggle for the moving current marker.
- Data card with full current values and derived GPS speed.
- Private login.
- CSV upload to SQLite.
- Route rename and delete.
- PNG export of current frame.
- PNG sequence recording exported as ZIP.
- Export format selector: horizontal, vertical, square.
- Export map provider selector: CARTO claro or OpenStreetMap.
- Export zoom selector: lejos, normal, cerca, detalle.
- Export panel uses dark background with white high-contrast data.

## Notes

- Export tries to render live web tiles into canvas. If browser/CORS blocks them, it falls back to a drawn cartographic background.
- No map tiles or generated PNG frames are committed.
- The root `.gitignore` and other non-player files may be locally modified; they are unrelated to this player branch work unless explicitly reviewed.
