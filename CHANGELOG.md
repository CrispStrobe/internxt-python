# Changelog

## Unreleased — Audit, Bug Fixes, Test Infrastructure

A multi-tool security/correctness audit (ruff, mypy, bandit, pylint,
vulture, pyflakes) was run across the codebase. Every medium+ finding was
either fixed or annotated with rationale, and a 557-test pytest suite was
built up alongside CI to lock in those fixes.

End state: **0 ruff errors, 0 mypy errors, 0 Bandit medium+ findings,
557/557 pytest passing in ~3 seconds, 90% line coverage**.

---

### Bug fixes

#### Critical

1. **`utils/api.py` — silent data loss in `delete_permanently()`**
   The function built `data={'items': [{'uuid': ..., 'type': ...}]}` but
   the inner `delete()` method had no `data` parameter, so the items list
   was silently dropped on the wire. Backup-file purges after the
   `safety_pattern` upload (called whenever an existing file is replaced)
   were no-ops, leaking trash storage indefinitely.
   **Fixed:** extended `delete()` to accept and forward `data=`, and
   updated `delete_permanently()` to pass it through.

2. **`__init__.py` — package wouldn't import**
   File started with text but had no opening `"""` for its docstring, so
   `import internxt_cli` raised `SyntaxError`.
   **Fixed:** added the missing triple-quote.

3. **`services/webdav_provider.py:set_property` — every PROPPATCH crashed**
   This is the handler macOS Finder and Windows Explorer use to set
   creation/modification times on remote folders. Two broken pieces:
   - It imported `rfc_1123_to_timestamp` and `rfc_3339_to_timestamp` from
     `wsgidav.util`. Neither has ever existed in any released wsgidav
     version. Every PROPPATCH crashed with `ImportError`.
   - It then called `drive_service.set_folder_timestamps(...)`, which was
     never defined on `DriveService`. Even if the imports had worked, the
     call would `AttributeError`.

   Both were caught by the outer `try/except` and surfaced as silent
   HTTP 403 responses to the client.
   **Fixed:** replaced the imports with `parse_time_string` (handles
   RFC 1123) plus a stdlib `datetime.fromisoformat` fallback for
   ISO 8601 / RFC 3339; added the missing `set_folder_timestamps` method,
   which calls `api.update_folder_metadata` and invalidates the parent
   cache.

#### High

4. **`services/webdav_server.py:start()` — `UnboundLocalError` on bad input**
   `active_server` was only assigned inside `if/elif/elif` branches for
   `auto`/`waitress`/`cheroot`. Any other value made the subsequent
   `if active_server is None:` check raise `UnboundLocalError`.
   **Fixed:** initialise `active_server: Optional[str] = None` upfront and
   raise an explicit `ValueError` for unknown choices.

5. **`services/webdav_server.py:start()` — bound to `0.0.0.0` ignoring config**
   The `host` was hardcoded to `"0.0.0.0"`, exposing the WebDAV server
   on every interface regardless of the `host` setting in the user's
   webdav config. Compounded by `read_webdav_config` silently dropping
   the `host` key during deserialization.
   **Fixed:** default to `127.0.0.1` (loopback only); pass user-set
   `host` from config through `read_webdav_config` so `0.0.0.0` is
   explicit opt-in.

6. **`services/webdav_provider.py:end_write` — `AttributeError` masked real errors**
   The `finally:` clause called `self._upload_buffer.cleanup()` without
   checking whether `_upload_buffer` had ever been assigned. Any
   exception before `begin_write` ran would crash with
   `AttributeError: 'InternxtDAVResource' object has no attribute
   '_upload_buffer'` and bury the real error.
   **Fixed:** initialise to `None` in `__init__`; guard the cleanup with
   `if self._upload_buffer is not None`.

7. **`services/crypto.py:generate_filename_encryption_iv` — `TypeError` at runtime**
   `hmac.new(bytes.fromhex(bucket_key), hashlib.sha512)` passes
   `hashlib.sha512` as the `msg` argument instead of `digestmod`, so the
   call raises `TypeError("Missing required argument 'digestmod'")` at
   runtime. (Currently dead code — `encrypt_filename` is not yet called
   anywhere — but a real latent bug that would fire as soon as someone
   wired it up.)
   **Fixed:** `hmac.new(..., digestmod=hashlib.sha512)`.

8. **`config/config.py` — duplicate `NETWORK_URL` key**
   `'NETWORK_URL'` was defined twice in the `self.config` dict literal.
   The second definition (`https://api.internxt.com`) silently overrode
   the first (`https://gateway.internxt.com/network`). The runtime
   behaviour was correct (the active value is the live network base URL)
   but the duplicate was an obvious code smell hiding a stale value.
   **Fixed:** removed the dead duplicate.

9. **`services/drive.py` and `utils/api.py` — duplicate function definitions**
   - `create_folder_recursive` was defined twice in `drive.py` (104
     lines of dead code in the first definition).
   - `move_file` and `move_folder` were each defined twice in `api.py`.
   In all cases the later definition silently overrode the earlier one.
   **Fixed:** removed the dead first definitions.

10. **`cli.py config` — command crashed on every invocation**
    Referenced `DRIVE_WEB_URL` which has been commented out of the config
    dict for an unknown amount of time. Every `python cli.py config`
    aborted with "Config key DRIVE_WEB_URL was not found in process.env"
    after only printing the section header.
    **Fixed:** tolerate missing keys with a `(not configured)` placeholder
    so the rest of the command runs.

11. **`install.py` — `shell=True` subprocess injection (Bandit B602)**
    `subprocess.run("pip install '" + dep + "'", shell=True)` would have
    accepted shell metacharacters from the dependency name.
    **Fixed:** switched to argv form using `sys.executable`.

#### Medium (security hardening, all Bandit medium+ findings cleared)

12. **`services/network_utils.py:test_webdav_connection` — unconditional `verify=False`**
    TLS verification was disabled for *all* requests, including remote
    hosts.
    **Fixed:** restricted `verify=False` to localhost loopback addresses
    only; remote URLs now use real TLS verification.

13. **`services/webdav_server.py` — multiple `0.0.0.0` binds (Bandit B104)**
    Three call sites hardcoded binding to all interfaces.
    **Fixed:** all replaced with the configurable `host` from webdav
    config, defaulting to loopback.

14. **`services/crypto.py:206` (SHA1 in PBKDF2) and `:257` (MD5 in EVP)**
    Both are required for protocol compatibility with the Internxt
    server (PBKDF2-HMAC-SHA1 password hash format; OpenSSL
    `EVP_BytesToKey` with MD5 for credential file encryption). Cannot be
    changed without backend cooperation.
    **Annotated:** `# nosec` with rationale comments; MD5 marked
    `usedforsecurity=False`.

15. **`services/drive.py:739` — MD5 used as cache-key hash**
    Non-cryptographic use (just deriving a stable filename for resumable
    upload checkpoints).
    **Fixed:** switched to SHA-256 (truncated to 32 hex chars).

#### Hygiene

- All bare `except:` clauses (8 of them) replaced with `except Exception:`
  or specific types.
- All unused imports removed (cli.py, services/*, utils/api.py, debug/*).
- All ~180 empty f-strings (`f"static text"`) replaced with plain strings.
- `raise … from e` chains added where pylint suggested (proper exception
  causality preservation).
- mypy implicit-Optional defaults annotated explicitly.
- `cli.py test` smoke command's two stale URL assertions updated to match
  the actual configured endpoints (was 5/7 passing, now 7/7).

---

### Test infrastructure (new)

#### Suite

**557 pytest tests** across 33 test files in `tests/`, covering:

- **Crypto** (`test_crypto.py`, `test_crypto_filename_meta.py`) — AES-256-CTR
  file round-trip across 0 B – 1 MB sizes, AES-256-GCM metadata cipher,
  PBKDF2 determinism, bucket-key derivation, filename encryption
  protocol round-trip + deterministic IV + cross-bucket isolation,
  decrypt-with-wrong-key returns None.
- **Auth** (`test_auth_login.py`, `test_auth_refresh.py`) — full hydrated
  login flow with mocked API, email lowercasing, missing-sKey error,
  token rotation with bridgeAuth recompute, persistence on success and
  not on failure.
- **Drive service** (`test_drive_*.py`) — path resolution (10 cases incl.
  legacy `name` field, file-vs-folder ambiguity), recursive folder
  creation with race-on-conflict fallback, content cache TTL, trash/
  delete dispatch, pagination with multi-page recursion, upload conflict
  state machine (skip/overwrite/safety-pattern with rollback), copy
  with timestamp preservation, memory-gated concurrency with **real
  threading test** for blocking + wakeup, validate_upload_sources,
  get_upload_statistics across recursive/non-recursive/empty.
- **WebDAV provider** (`test_webdav_*.py`) — resource metadata accessors,
  StreamingFileUpload memory↔disk hybrid, end_write upload-on-close
  cycle (small + large + update-existing + pending-uuid), get_content
  download path with real crypto round-trip, set_property PROPPATCH
  timestamps (RFC 1123 + RFC 3339 + Win32 namespace), thread-local
  isolated sessions, mark_deleted bookkeeping.
- **WebDAV server** (`test_webdav_server*.py`) — start() server-choice
  routing (auto/waitress/cheroot/invalid), HTTPS-with-waitress fallback,
  HTTPS-with-cheroot SSL adapter setup, missing-cert auto-generation,
  thread runner (waitress + cheroot branches, KeyboardInterrupt caught,
  silent shutdown), stop() (foreground + background via psutil), status
  (foreground + background + stale-pid cleanup), test_connection
  PROPFIND probe, mount instructions for all platforms,
  _check_port_available with real socket.
- **API client** (`test_api_*.py`) — every endpoint URL+method+payload
  pinned (folders pagination, create_folder validation, metadata GET/PUT,
  ancestors list-or-empty, trash bulk, network upload v2, search, login
  with email lowering); `_make_request` json-vs-data routing, Bearer
  strip when basic auth, HTTPError → ValueError, timeout → ConnectionError;
  `robust_request` 401-rotate-and-retry with refreshed token; raw
  upload_chunk/download_chunk via direct `requests.put`/`get`.
- **Config persistence** (`test_credentials_persistence.py`) — encrypted
  credentials round-trip on disk, clear-when-empty error semantics,
  webdav config + pid file lifecycle.
- **Network utils** (`test_network_utils.py`, `test_ssl_lifecycle.py`) —
  Range header parsing (7 cases), self-signed cert generate→save→get→
  validate cycle, `0o600` private-key permissions, regen-on-garbage-cert,
  loopback-only `verify=False` regression.
- **CLI commands** (`test_cli_*.py`, `test_mv_command.py`) — Click
  `CliRunner` against `whoami`, `logout`, `login`, `trash-path`,
  `delete-path` (force vs. confirmation prompts), `list-path`, `mkdir`,
  `search`, `find` (-name + -iname), `resolve`, `tree`, `upload` (no-source
  / target-creation / target-is-file rejection), `download-path` (folder-
  without-r / missing / filter-skipped), `webdav-stop/status/mount`,
  `config`, `mv` (single + glob expansion + conflict skip/overwrite +
  dry-run + already-in-target + parallel error propagation).

#### Headline test

`test_upload_download_e2e.py` — writes a 19 KB file locally, runs
`upload_file_to_folder` (real crypto, mocked network), captures the
encrypted bytes that would have hit the upload URL, then runs
`download_file` against those bytes and asserts byte-for-byte recovery.
This catches any drift in the Internxt encryption protocol that unit
mocks would miss.

#### CI gates

- **`pyproject.toml`** — pytest, coverage, ruff config.
- **`requirements-dev.txt`** — pinned floor for pytest, pytest-cov,
  ruff, mypy, bandit.
- **`.github/workflows/ci.yml`** — runs **ruff → mypy → bandit
  (medium+) → pytest+coverage** on Python 3.10 / 3.11 / 3.12. Coverage
  XML uploaded as an artifact on Python 3.11.

#### Coverage by module

| Module                          | Coverage |
|---------------------------------|---------:|
| `config/__init__.py`            |    100% |
| `config/config.py`              |     85% |
| `services/__init__.py`          |    100% |
| `services/auth.py`              |    100% |
| `services/crypto.py`            |    100% |
| `services/drive.py`             |     89% |
| `services/network_utils.py`     |     90% |
| `services/webdav_provider.py`   |     91% |
| `services/webdav_server.py`     |     83% |
| `utils/__init__.py`             |    100% |
| `utils/api.py`                  |     98% |
| **Total**                       |    **90%** |

The trust roots (auth + crypto) are at 100%. The remaining gaps in
`webdav_server.py` and `drive.py` are platform-specific fallback paths
(`_available_memory` for non-darwin/win32 systems) and `print()`-heavy
diagnostic branches that are best exercised by integration tests against
a live Internxt backend, not more unit mocks.

#### Not yet covered (intentional)

- A vcrpy-recorded contract test against the real Internxt API for one
  full upload→download cycle. Needs test credentials and a one-time
  recording session; would lock in the exact wire-format expectations of
  the production backend so future API changes fail the test in CI
  rather than in the field.
