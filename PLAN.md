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

---

# Performance: within-file chunk concurrency (added 2026-06-29)

File-level (batch) concurrency ALREADY exists: `cli.py` uses a
`ThreadPoolExecutor` + `--workers`, gated by `DriveService._mem_acquire`/
`_mem_release` (`services/drive.py:121,147`). What is still SEQUENTIAL is the
transfer of a SINGLE large file. Two opportunities, ported from the
filen-python / filen-dart "bounded chunk concurrency" work (read those repos'
PLAN.md "Performance" section + filen-dart `LEARNINGS.md` first).

Internxt differs from Filen: **AES-CTR, one continuous keystream** (encryption
is inherently sequential — like a running hash), and **S3** storage (multipart
parts on upload; a single presigned object on download).

## Step A — parallel multipart part uploads ✅ DONE
**Implemented** (`services/drive.py` `_perform_network_upload`). The producer
reads each 30 MB part and runs `encryptor.update` + `sha.update` strictly in
order, then dispatches the part PUT to a bounded
`ThreadPoolExecutor(max_workers=self.chunk_workers)` (default 4; CLI
`upload --chunk-workers N` sets `DriveService.chunk_workers`). A
`BoundedSemaphore(n_workers)` caps parts in flight and `_mem_acquire`/
`_mem_release` add the RAM ceiling on bytes in flight; each worker writes its
ETag to `parts_manifest[part_index]` BY INDEX (pre-sized list). A failed part is
collected under a lock, stops the producer, and is re-raised after
`executor.shutdown(wait=True)`. Files < 100 MiB stay on the single-PUT
sequential path. Unit tests: `tests/test_chunk_concurrency.py` (peak in-flight ≤
N, manifest ordered under out-of-order completion, hash identical under
concurrency, in-flight bytes bounded by the gate, failing part surfaced).

NOTE — gate nesting: `upload_file_to_folder` used to hold an outer
`_mem_acquire(UPLOAD_PART_SIZE*2)` across the whole call. Since the per-part
gating now lives inside `_perform_network_upload`, that outer hold is skipped
for multipart files (`file_size >= MULTIPART_MIN_SIZE`) — the gate is global and
non-reentrant, so holding it outside while the per-part acquires run inside
would deadlock. Small (single-PUT) files still reserve at the outer level.
Verified live: 110 MB multipart upload round-trips byte-exact.

Original plan (kept for reference):
`services/drive.py` → `_perform_network_upload` (~line 185): the sequential
`for part_index in range(parts):` loop (~238) that encrypts a 30 MB part
(`encryptor.update`), updates `sha`, and PUTs it via
`_put_with_retry(lambda: api.upload_part(urls[i], ciphertext))` (`utils/api.py:423`)
before the next. Only files ≥ `MULTIPART_MIN_SIZE` (100 MiB) use multipart;
smaller files are a single PUT — keep them sequential.

Approach (mirror filen Step 1):
- A SEQUENTIAL producer reads each part, runs `encryptor.update(plaintext)` IN
  ORDER (CTR keystream is stateful — CANNOT parallelize) and updates `sha`, then
  hands `(part_index, urls[part_index], ciphertext)` to a bounded
  `ThreadPoolExecutor(max_workers=N)` (N=4 default; add a `--chunk-workers` flag /
  `DriveService` attr).
- Each worker PUTs and writes its ETag to `parts_manifest[part_index]` — pre-size
  the list and assign BY INDEX (parts finish out of order).
- Bound bytes-in-flight with the EXISTING gate: `self._mem_acquire(len(ciphertext))`
  before dispatch, `self._mem_release(...)` in the worker `finally`. N×30 MB live.
- After join: assert `bytes_done == file_size`, manifest is ordered by PartNumber,
  then `api.finish_upload(...)` unchanged.

CONSTRAINTS — do NOT just flip the loop:
1. CTR `encryptor.update` + `sha.update` stay strictly sequential in the producer
   (keystream + content hash are order-dependent). Only the PUT is parallel.
2. `parts_manifest` ordered by PartNumber regardless of completion order.
3. Bound by BYTES in flight via `_mem_acquire` (30 MB parts — count is not enough).
4. On part failure, surface after join; let the existing checkpoint/resume path
   (`create_upload_checkpoint`, `services/drive.py:898`) handle restart.

Win is the single-large-file case (batch is already file-parallel).

## Step B — parallel ranged downloads ⬜ TODO (riskier; gate behind a flag)
`services/drive.py` → `download_file` (~line 1616): today one
`api.download_stream(url)` (`utils/api.py:461`) over a single presigned S3 URL
(`api.get_download_links(...)['shards'][0]['url']`), streamed + CTR-decrypted to
disk. S3 presigned GETs honor HTTP `Range`.

Approach:
- Split into N aligned ranges (e.g. 30 MB). Add `api.download_range(url, start, end)`
  (sends `Range: bytes=a-b`; expect 206). Fetch ranges with a bounded pool.
- Write each range at its offset (`f.seek(off); f.write(...)` under a lock, or
  `os.pwrite`); pre-size with `f.truncate(file_size)`.
- HARD PART — seekable CTR decrypt: AES-CTR's counter for byte offset O is
  `initial_counter + O//16`. Add `crypto.new_download_decryptor_at(mnemonic,
  bucket_id, file_index_hex, offset)` that inits the counter for a 16-B-aligned
  offset. Then each range decrypts independently. Verify the existing content-hash.

CONSTRAINTS:
1. Range starts MUST be AES-block-aligned (16 B); 30 MB is aligned.
2. Bound bytes-in-flight (`_mem_acquire`).
3. Fall back to the sequential single-stream path on 200-not-206 or small files.

## Tests (unit hermetic + live)
Unit (mock `utils/api.py` PUT/GET): assert peak in-flight ≤ N; manifest ordered;
seekable-CTR decrypt reproduces plaintext; small files stay sequential.
- [x] upload: peak parts in flight ≤ N (`test_chunk_concurrency.py`)
- [x] upload: `parts_manifest` ordered by PartNumber under out-of-order completion
- [x] small files (<100 MiB) stay single-PUT
- [x] memory: in-flight bytes bounded by the gate's budget
- [ ] (Step B) seekable-CTR decrypt of an arbitrary aligned offset == plaintext
Live (mirror `tests/test_live_smoke.py` skip gate; confine to a temp folder, clean up):
- [ ] ≥100 MB upload: byte-exact round-trip + faster than the sequential baseline
- [ ] interrupted multipart upload resumes (checkpoint) and completes
- [ ] ranged download of a large file is byte-exact + faster than single-stream
