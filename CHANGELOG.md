# Changelog

## Unreleased

### Added
- **Secure credential input + storage.** `login` now accepts the password via
  `--password-stdin` (never hits argv/shell-history/process-list) and reads
  `INTERNXT_EMAIL` / `INTERNXT_PASSWORD` env vars. The stored session is
  encrypted at rest with a key from the **OS keychain** (`keyring`) when
  available, else a user-supplied `INTERNXT_CREDENTIALS_KEY`, else the legacy
  static key — and the credentials file/dir are now `0600`/`0700` (previously the
  file was world-readable and "encrypted" only with a public constant). Legacy
  credential files are auto-migrated on read. `INTERNXT_NO_KEYRING=1` forces the
  file fallback.
- **`rcat` — stream stdin to a Drive file** (issue #9): pipe a stream straight to
  Drive without a named local file, rclone-`rcat` style
  (`mariadb-dump | xz | cli.py rcat /backups/db.xz`). Internxt requires the exact
  size up front (the gateway pre-issues the presigned part URLs at upload start),
  so true unknown-size streaming isn't possible — `rcat` spools stdin to a temp
  file to measure size, then runs the normal streaming-encrypt upload (single-PUT
  or multipart). Empty stdin aborts non-zero; a TTY (no pipe) is rejected;
  `--temp-dir` relocates the spool.

### Fixed
- **Clearer API errors**: a failed request now reports the HTTP status, reads the
  server's message under either `message` or `error` (the network gateway uses
  `error`), and adds an actionable hint for well-known conditions. A full account
  now reads `API Error (HTTP 420): Max space used — storage quota exceeded…`
  instead of the previous useless `API Error: Unknown Error`.
- **`trash-restore`** no longer swallows the real failure (auth/network/API
  errors) behind a blanket "not found in trash" — the underlying error is
  surfaced.
- Repaired pre-existing `mypy` gaps in `utils/api.py` (implicit-`Optional`
  defaults, an unreachable `isinstance`); the CI type-check gate now covers
  `utils` and `config` too.

### Notes
- **Multipart upload is automatic** for files ≥ 100 MiB (the server's multipart
  floor) and its parts upload in parallel by default (`--chunk-workers`, default
  4; set `1` for serial). **Ranged download stays opt-in** (`--ranged`, default
  off) until it is fully live-validated.

## 1.1.0 — within-file transfer concurrency

A single large file now transfers with **bounded concurrency** (multi-file batch
concurrency via `--workers` already existed).

### Added
- **Parallel multipart part uploads (Step A):** files ≥ 100 MiB upload their 30 MB
  S3 multipart parts in parallel (previously sequential), bounded by the existing
  memory gate (`_mem_acquire`/`_mem_release`). AES-CTR encryption + the content
  hash stay strictly sequential; only the part PUTs overlap, and the parts
  manifest is ordered by part number.
- **Parallel ranged downloads (Step B, opt-in `--ranged`):** large downloads fetch
  multiple HTTP byte-ranges concurrently, decrypt each via a seekable AES-CTR
  decryptor, and reassemble by offset. Falls back to the single-stream path when
  the server doesn't honor `Range` or the file is small.

### Preserved
- Batch file-level concurrency (`--workers`) unchanged; files < 100 MiB keep the
  single-PUT path and non-ranged downloads keep the single-stream path.

## 1.0.0

- Prior release.
