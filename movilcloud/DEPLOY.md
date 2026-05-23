# NUBEMOVIL Deploy Notes

## Scope
- This branch contains the PHP receiver and onepage for the public `NUBEMOVIL` site.
- Keep all passwords, FTP credentials and ingest tokens out of git.

## Files To Publish
- publish the contents of `movilcloud/public/`
- keep these directories available to PHP as writable runtime storage:
  - `movilcloud/storage/`
  - `movilcloud/storage/frames/`
- keep runtime config editable:
  - `movilcloud/config/config.php`

## Required Runtime Config
- `site_name`
  - current public branding: `NUBEMOVIL`
- `ingest_token`
  - placeholder in git
  - replace only on the server
- ensure `storage/` is writable by PHP

## Public Endpoints
- ingest:
  - `/nubemovil/cloud_ingest.php`
- state:
  - `/nubemovil/cloud_state.php`
- frame:
  - `/nubemovil/cloud_frame.php`
- current branded entry:
  - `/nubemovil/nubemovil.php`

## Pi Bridge Target
- `https://www.rotor-studio.net/nubemovil/cloud_ingest.php`

## Deploy Checklist
1. Upload the app to the public `nubemovil` directory.
2. Set the real `ingest_token` only on the server copy.
3. Ensure `storage/` is writable.
4. Open `cloud_state.php` and confirm fresh updates.
5. Open `nubemovil.php` and confirm the public page renders current metrics and frame.

