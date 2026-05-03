# Plan / Roadmap

What's known to be unimplemented, broken, or worth porting from upstream
(`internxt/cli`). Priority is from a CLI-completeness perspective; the
existing 585-test suite at 90% coverage already covers the audit fixes
and existing functionality.

---

## Confirmed gaps vs. upstream `internxt/cli`

After a side-by-side comparison with the official upstream CLI:

### Functional regressions (must-fix)

These commands exist upstream but not here. The trash one is the most
visible — without restore + list + clear, our `trash` is effectively a
one-way trip from a CLI-user's perspective.

#### 1. Trash lifecycle (`trash-list`, `trash-restore`, `trash-clear`)

The backend plumbing is **already wired** in `utils/api.py` and unit-tested:

- `api.get_trash_content(offset, limit, item_type)` → GET `/storage/trash/paginated`
- `api.restore_item(uuid, type, dest_folder_uuid)` → POST `/trash/restore`
- `api.clear_trash()` → DELETE `/storage/trash/all`

What's missing is the user-facing Click commands. Estimated work:

- `trash-list` (Click command, ~20 LOC + 2-3 unit tests + 1 live test)
- `trash-restore-path <path>` and `trash-restore <uuid>` (~30 LOC + tests)
- `trash-clear` with confirmation prompt (~15 LOC + tests)
- 1 cross-cutting live test: trash → list → restore → verify file is back

#### 2. Login `--twofactor` / `--twofactortoken` flag parity

Verify our `login --2fa` flag matches what upstream exposes (`--twofactor`,
`--twofactortoken`). They might also support env-var overrides
(`INTERNXT_2FA_TOKEN`) which we don't.

### Niceties (low priority)

#### 3. `add-cert` — install WebDAV self-signed cert into OS trust store

Quality-of-life. Currently the user has to accept the cert manually in
their browser/file manager every time. Platform-specific implementation
(`security` on macOS, `certutil` on Windows, system trust on Linux).

#### 4. `logs` command — print log directory path

Trivial; we already write logs there, just don't expose the path nicely.

### Explicitly skipped

- **Workspaces** (`workspaces-list/use/unset`) — out of scope. Personal
  accounts only. Would require threading a `workspace_uuid` through
  every drive op and auth call — substantial lift for no individual-
  user benefit.

---

## Things upstream doesn't have either (potential differentiators)

If we add these, we're ahead. Each is a self-contained feature.

### A. Folder copy

**Neither CLI has this.** Adding it would be a real differentiator for
users migrating between accounts or making backups. Implementation path:

- Extend `drive_service.copy_item` with a folder branch
- Walk source folder recursively
- For each subfolder: create matching folder under destination
- For each file: download → re-upload (current `copy_item` strategy)
- Streaming optimization: hold one chunk at a time to avoid OOM on
  large folders
- Handle the "destination folder already has a child with this name"
  case (skip / overwrite / rename)

Estimated 50-80 LOC + 2-3 live tests covering: empty folder, single
file, nested 2 levels, conflict at destination.

### B. Storage quota display

Backend exposes `/users/usage` (we have `api.get_storage_usage()` wired
up) but no CLI command for it. Add `python cli.py quota` showing used /
limit / percentage in human-readable form. Trivial.

### C. Sharing / link generation

Internxt has shareable file links in their web UI. The API for it isn't
in our `api.py` — would need to add the share-link endpoints first
(POST `/sharings`, GET `/sharings/...`). Medium-sized feature.

### D. Backup configuration

Upstream doesn't expose backup config; the desktop client manages it.
Would need to call backup-config endpoints which aren't in our `api.py`.
Probably out of scope for a CLI.

---

### F. WebDAV provider end-to-end testing

Both WSGI servers we ship (`waitress` and `cheroot`) have known field
reports of intermittent issues — `waitress` is the default and most
stable, `cheroot` was the original and is documented as having
"NoneType has no len()" errors with macOS Finder. Comments in
`webdav_server.py` even warn about it.

What's tested today (unit + live):
- ✅ Server starts on either backend (mocked SSL/cert path branches)
- ✅ start() routes correctly per `--server` choice
- ✅ stop() / status() / mount-instructions / port-availability
- ✅ Provider class (`InternxtDAVProvider`): `get_resource_inst`,
  `exists`, init with credentials
- ✅ Resource (`InternxtDAVResource`) accessors: content length, mime
  type, etag, last-modified
- ✅ Collection (`InternxtDAVCollection`) listing + member resolution
- ✅ `begin_write` / `end_write` upload cycle (small + large file
  buffer, with-errors abort, update vs create)
- ✅ `set_property` PROPPATCH timestamp handler
- ✅ `get_content` download path (with real crypto round-trip via
  mocked network)

What's **not** tested (the actual reliability concern):
- ❌ Real HTTP traffic against the running server. Every WebDAV unit
  test stubs the wsgidav environ; nothing fires a real `requests.put()`
  / `PROPFIND` / `MKCOL` against a server bound to a real socket.
- ❌ macOS Finder–specific quirks (the `Depth: infinity`, `If: <token>`
  conditional headers, locking semantics, the `MS-Author-Via` header
  Office requires)
- ❌ Cheroot vs waitress behavioural differences end-to-end
- ❌ Large file PUT through the server (does it stream correctly when
  Content-Length triggers the disk-buffer path?)
- ❌ Concurrent client connections (two Finder windows open at once)

**Plan: add `tests/test_live_webdav_server.py` with an in-process
server fixture.**

Outline:

```
@pytest.fixture(scope='module')
def running_webdav(authed_session):
    # Start the WebDAV server in a background thread on a free port,
    # bound to 127.0.0.1 only (no LAN exposure).
    # yield server URL + auth tuple
    # On teardown: server.stop() + clear state
```

Tests to write (priority order):

1. **OPTIONS** — verify `Allow:` header includes PROPFIND, PROPPATCH,
   MKCOL, COPY, MOVE, LOCK, UNLOCK; `DAV:` header includes "1, 2";
   `MS-Author-Via:` is set.
2. **PROPFIND root depth=0** — returns 207 multistatus XML containing
   the root resource. Verify well-formed XML + presence of
   `D:resourcetype`, `D:getlastmodified`.
3. **PROPFIND root depth=1** — returns the root + immediate children
   (whatever's already in the user's drive). At minimum, doesn't
   crash on the user's actual file tree.
4. **MKCOL** — create a folder via WebDAV. Verify the folder appears
   when re-listed via PROPFIND AND via the underlying drive_service.
5. **PUT** small file — upload via WebDAV. Verify it lands on the
   real backend with the correct bytes.
6. **GET** the file we just PUT — verify response body is the original
   bytes (full encrypt → decrypt round-trip via the running server).
7. **DELETE** — delete the file via WebDAV. Verify it's gone from
   PROPFIND + from drive_service.
8. **MOVE** — rename via WebDAV's MOVE method. Verify path changes.
9. **PROPPATCH** for timestamps — set `getlastmodified` on a folder,
   verify it sticks (this is the regression we already fixed; needs
   end-to-end coverage).
10. **Both servers**: run the suite parameterized over
    `server_choice='waitress'` and `server_choice='cheroot'` — catches
    behavioural divergence.

Safety:
- Bind only to `127.0.0.1` on a kernel-assigned free port.
- Run all WebDAV ops inside the same sentinel folder
  (`/__pytest_internxt_cli_smoke__/<run-uuid>/`) the existing live
  tests use.
- Module-scope teardown stops the server cleanly.

Effort: ~2 hours. Catches: cheroot vs waitress drift, PROPFIND XML
shape regressions, MOVE/COPY semantics, the `If: <token>` header
gotcha that breaks macOS Finder, the disk-buffer streaming path under
real HTTP framing.

Alternative if writing real WebDAV tests is too much: at minimum,
write a scripted manual smoke (`tests/manual_webdav_smoke.sh`) the
user runs by hand: starts server, runs `curl -X PROPFIND/PUT/GET/DELETE`
against it, asserts non-error responses. Less rigorous but catches
the most blatant breakage.

---

## Maintenance / quality work (not features)

### M1. Reduce `services/drive.py` size

`services/drive.py` is **992 statements** in one file. It mixes:

- Path resolution + caching
- Trash dispatch
- Move / rename / copy
- Memory-gated upload concurrency
- Network upload (multipart, retry, progress)
- Network download (decrypt, write, timestamp preserve)
- Folder operations (create, recursive create, list)
- Validation + statistics helpers
- Filename sanitization
- Resumable upload checkpoints

A natural split:
- `services/drive_paths.py` — resolve_path, list_folder_with_paths,
  get_full_path_for_item, find_files
- `services/drive_upload.py` — upload_file_to_folder,
  upload_with_safety_pattern, upload_single_item_with_conflict_handling,
  _upload_chunk_with_progress, _mem_acquire/_release/_available_memory
- `services/drive_download.py` — download_file, download_file_by_path
- `services/drive_mutations.py` — move_*, rename_*, copy_*,
  trash_*, delete_*, set_folder_timestamps
- `services/drive.py` — facade re-exporting the unified API for backward
  compat

Risk: 992 lines is annoying but works; splitting touches ~every test.
Probably not worth it unless someone is actively contributing to the
upload code paths.

### M2. CI cassette path for live tests

The current live test suite uses real credentials (read from `.env`)
and skips automatically without them. CI never runs them. Two ways to
get them into CI:

(a) **Skip in CI** — current state. CI runs unit tests only, the user
    runs live tests locally before releases. Simplest and safest.

(b) **GitHub Secrets** — add `IXT_ACCOUNT` / `IXT_PWD` to the repo as
    encrypted secrets and run live tests in CI. Has the obvious risk
    that anyone with PR write access can exfiltrate them; not
    recommended for a personal-use project.

(c) **vcrpy cassettes** — record real responses, redact tokens, replay
    in CI. We discussed and discarded this in favor of the live-smoke
    pattern because of the "this is a real account, not a throwaway"
    constraint.

Recommendation: **stay on (a)**. Live suite is for local pre-release
verification, not CI.

### M3. Stale-cache audit

The `create_folder_recursive` cache-coherency bug we found suggests
there may be more places where mutations don't update the parent cache.
Worth a one-pass review of every mutating method (`move_*`, `rename_*`,
`copy_item`, `update_file`, `trash_*`, `delete_permanently_*`) to
confirm each one invalidates / updates the right cache entries.

Done partially (we know move_*, trash_*, rename_*, delete_* invalidate
parent cache via `_clear_parent_cache_for_item`). Still need to check:

- `update_file` after replace_file
- `copy_item` (does it add the new file to dest cache? — currently I
  don't think it does)
- Folder operations in `set_folder_timestamps`

### M4. Pre-commit hooks

Project has CI but no pre-commit hooks. A `.pre-commit-config.yaml`
running `ruff check --fix` + `ruff format` (or just `ruff check` if
we don't want to enforce formatting) would catch lint-clean violations
before they hit CI.

### M5. Minimum-Python audit

`setup.py` says `python_requires=">=3.8"` but several pieces use
3.10+ syntax (`Optional[...]` without `from __future__ import
annotations` is fine, but pattern matching, walrus, etc. would not
be). Audit and either bump the floor or backport.

---

## Out of scope (explicit non-goals)

These are intentionally not on the roadmap:

- Sync engine (continuous file sync) — that's what the Internxt
  desktop client is for; CLI isn't the right tool.
- File versioning / point-in-time restore — backend doesn't expose
  this.
- Anti-virus / file scanning — Internxt servers do this; CLI hooks
  would just duplicate.
- GUI — out of scope for a CLI tool.
- Cross-account migration helpers — interesting but a separate
  project.

---

## Suggested next session

If you want to ship one more bundle of work:

1. **WebDAV providers reliability test** (F above, ~2 hours) — biggest
   real-world blind spot; addresses the field reports of intermittent
   issues.
2. **Trash lifecycle** (~3 hours) — closes the most-visible upstream
   functional gap.
3. **Folder copy** (~3 hours) — real differentiator; neither CLI has it.
4. **Quota command** (~30 min) — trivial easy win.

Total: roughly a day's work, would close every meaningful gap with
upstream and add a feature upstream doesn't have. Live test count
would grow from 28 to ~50; unit tests by maybe 15-20.

If you can only do one: **the WebDAV reliability test**. It's the
piece most likely to surface a real bug, and the effort is smaller
than the others.
