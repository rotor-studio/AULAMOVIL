# NUBEMOVIL Player

PHP + SQLite route player for public map playback and private route uploads/export tools.

## Local run

```bash
cd /Users/xd/AULAMOVIL
PLAYER_PASSWORD='change-this-local-password' php -S 127.0.0.1:8787 -t player/public
```

Open:

```text
http://127.0.0.1:8787/
```

## Storage

Runtime data is stored in `player/var/player.sqlite`. This directory is ignored by git.

## Security Notes

- The private layer requires a password.
- Set `PLAYER_PASSWORD_HASH` in production with a `password_hash()` value.
- `PLAYER_PASSWORD` is accepted for local development.
- SQLite writes use prepared statements.
- CSV uploads are limited and validated server-side.
- Mutating private API requests require a CSRF token.
