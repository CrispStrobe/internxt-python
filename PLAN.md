# Plan — Remaining Work

Forward-looking items. For completed work see [`HISTORY.md`](HISTORY.md).

---

## Server-side multipart upload

The Internxt network API (`/v2/buckets/{id}/files/start`) only supports
`multiparts=1` — a single pre-signed URL per upload. Attempts to request
multiple URLs (`multiparts=N`, N>1) return HTTP 400. True multipart
upload (splitting large files across multiple URLs/shards) would require
backend support that doesn't exist today.

The current upload path handles large files via streaming progress
(`_upload_chunk_with_progress` sends in 1 MB sub-pieces for tqdm) and
dynamic timeouts based on file size. This works for files up to several
GB on reasonably fast connections.

**If the backend adds multipart support later:** The `MULTIPART_THRESHOLD`
and `CHUNK_SIZE` constants in `drive.py` and the `parts`/`chunk_size`
parameters in `api.start_upload` are ready to wire up.

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

### Minimum-Python audit

`setup.py` says `python_requires=">=3.8"` but some features may require
3.10+. Audit and either bump the floor or backport.

---

## Out of scope

- Sync engine, file versioning, GUI, cross-account migration
- Workspaces (`workspaces-list/use/unset`)
