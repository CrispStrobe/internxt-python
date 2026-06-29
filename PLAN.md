# Plan — Remaining Work

Forward-looking items. For completed work see [`HISTORY.md`](HISTORY.md).

---

## ~~Server-side multipart upload~~ — DONE (2026-06-29)

This was previously believed impossible (the note here claimed
`multiparts=N`, N>1 returns HTTP 400). That was **wrong**: reading
Internxt's own repos and testing live showed the network API fully
supports multipart for files ≥ 100 MiB. Implemented — see the HISTORY.md
entry "Large-file uploads: streaming + true multipart + streaming
download". Uploads now stream-encrypt (RAM bounded by part size), use
true S3 multipart with per-part retry, and store the protocol-correct
`ripemd160(sha256)` shard hash; downloads stream-decrypt to disk.

Possible follow-ups: concurrent part PUTs (currently sequential), and
resumable uploads across process restarts (re-using the same `UploadId`).

---

## WebDAV end-to-end testing

No tests fire real HTTP against a running WebDAV server. All unit tests
stub the wsgidav environ. Plan: add `tests/test_live_webdav_server.py`
with an in-process server fixture.

---

## Maintenance

### Pre-existing live test flakiness

Some live tests have eventual-consistency retries for downloads
immediately after uploads (the Internxt backend needs a moment for files
to become queryable). The retry pattern is applied to:
- `test_live_upload_extensionless_file`
- `test_live_file_move_between_folders`
- `test_live_large_file_upload_round_trip`
- `test_live_multipart_upload_round_trip` (also skippable via `IXT_SKIP_MULTIPART=1`)

### Minimum-Python audit

`setup.py` says `python_requires=">=3.8"` but some features may require
3.10+. Audit and either bump the floor or backport.

---

## Out of scope

- Sync engine, file versioning, GUI, cross-account migration
- Workspaces (`workspaces-list/use/unset`)
