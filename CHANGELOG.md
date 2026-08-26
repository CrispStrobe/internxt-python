# Changelog

## Unreleased

### Added
- **Upload target path aliases and recovery-friendly scans.** `upload` now
  accepts `--target` / `--path` / `-t` for the remote destination, still
  tolerates the old positional target shorthand, and normalizes Windows
  backslash paths. Recursive uploads also persist their remote subtree scan for
  24 hours so interrupted reruns can skip pre-existing files without fetching
  the full tree again.
- **Resumable multipart uploads** (internxt-python issue #11). No official
  Internxt client can recover an interrupted upload — the network API's
  `files/start` always mints a new uuid/UploadId and there is no endpoint to
  re-issue presigned URLs — but `files/finish` has no deadline and the
  AES-256-CTR ciphertext is deterministic given the file index. The CLI now
  exploits this: for multipart uploads (≥ 100 MiB) it checkpoints
  `{index, uuid, UploadId, presigned URLs, completed ETags}` to
  `upload_checkpoints/` as parts finish, and re-running the same upload
  re-encrypts locally (cheap, no network) while **skipping the PUT for parts
  already uploaded**, then finishes normally. Works until the presigned URLs
  expire (HTTP 403 → clean fallback to a fresh upload; ditto if the server
  rejects the stale state at finish). Disable with `--no-resume`.
- **In-session part repair**: a part that exhausts its per-PUT retries no
  longer aborts the whole upload. The producer still streams to the end
  (completing the cipher/hash state), then a repair pass re-encrypts just the
  failed parts by seeking the CTR keystream to their offset and re-PUTs them
  (2 rounds, 15 s apart), so brief outages survive without restarting. Only
  if repair also fails does the upload error out — with its progress
  checkpointed for resume.
- **OpenPGP login keys work on Python 3.13/3.14; PGPy no longer required**
  (internxt-python issue #10). Login needs a valid OpenPGP `ed25519Legacy`
  keypair, which was generated with PGPy — but PGPy (unmaintained since 2022)
  imports the `imghdr` stdlib module removed in Python 3.13, so it crashes on
  import there. Added a built-in, dependency-free backend that serialises the
  OpenPGP packets directly with `cryptography` (EdDSA/Ed25519 primary + ECDH/
  Curve25519 subkey), validated against openpgp.js and GnuPG. Key generation now
  tries **PGPy → native → GnuPG** and uses the first available, so login works on
  every supported Python. The native path is also ~3× faster than PGPy. PGPy and
  `python-gnupg` move to optional `extras_require` (`pip install .[pgpy]` /
  `.[gnupg]`); neither is needed for a working install.
- **Implicit auto-login for unattended pipelines** (issue #9): when no valid
  session is stored, commands now log in automatically from `INTERNXT_EMAIL` /
  `INTERNXT_PASSWORD` (+ `INTERNXT_TFA_SECRET` for 2FA), so `… | rcat …` needs no
  separate `login` step. Also kicks in transparently when a stored token has
  expired. An explicit `login && … && logout` flow still works.
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
- **Large recursive uploads are more tolerant of flaky disks and gateway
  errors.** Local file reads are retried in bounded subreads with exact offset
  reporting, transient gateway/API failures (`429`, `502`, `503`, `504`) are
  retried around upload metadata and listing calls, and an ambiguous
  post-create `502` now checks the destination before retrying to avoid obvious
  duplicate metadata entries.
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
- **Quieter unattended logs** (issue #9): the internal auth `TRACE`/`DEBUG`
  lines (which echoed the account email and bridge-auth material on every
  authenticated command) are now off by default and emitted to stderr only when
  `INTERNXT_DEBUG=1`.
- **`logout` now exits non-zero on failure**, so a scripted
  `login && … | rcat … && logout` chain can trap a failed logout.

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
