# Learnings

Insights from the audit + test build-out that took this codebase from
**zero tests, broken on import** to **585 tests at 90% line coverage,
17 real bugs found and fixed, all four CI gates passing**.

These are observations worth carrying forward, not a description of
what was done (see [`HISTORY.md`](HISTORY.md) for that) or a list of
what's left (see [`PLAN.md`](PLAN.md)).

---

## On the audit itself

### What each tool actually catches

The four-tool audit (ruff, mypy, bandit, pylint) felt redundant going
in, but every tool surfaced something the others missed:

| Tool | Highest-value finds |
|---|---|
| **ruff** | Empty f-strings (180+), bare `except:` (8), unused imports (40+), duplicate function definitions (3) |
| **mypy** | The `hmac.new(key, hashlib.sha512)` bug — `sha512` was being passed as `msg` not `digestmod`, would `TypeError` at runtime. mypy was the only tool that flagged this. |
| **bandit** | `subprocess(..., shell=True)` in install.py; `verify=False` everywhere; identified MD5/SHA1 sites that needed `# nosec` annotation with rationale |
| **pylint** | `from wsgidav.util import rfc_1123_to_timestamp` — flagged as `E0611: no-name-in-module`. We initially shrugged it off; it later turned out the import would crash every WebDAV PROPPATCH at runtime. |

Lesson: **don't dismiss pylint warnings as "noisy"**. The
`no-name-in-module` warning was the only signal that an entire user-
facing code path (Finder/Explorer setting folder timestamps) was
broken on import. Treat each as "is this real?" not "is this serious?"

### What unit tests can't catch — three concrete examples

The unit-mocked suite reached 90% coverage and missed three real bugs
that the live tests caught immediately:

1. **`create_folder_recursive` cache-coherency bug.** When creating
   nested paths like `/A/B/C`, intermediate folders went through
   `api.create_folder()` directly, which doesn't update the parent
   cache. Subsequent `resolve_path()` walks then read stale caches and
   returned `FileNotFoundError` even though the chain existed on the
   server. Unit tests stubbed `get_folder_content()` directly,
   bypassing the cache layer entirely — so the bug was invisible in
   unit tests.

2. **`/drive/users/me` returns 404.** Our `api.get_user_info()`
   method calls a URL that doesn't exist on the live backend. Unit
   tests mocked `_make_request` to return whatever we wanted; reality
   returns "Cannot GET /api/users/me". Pure integration-only finding.

3. **Internxt fuzzy search returns matches for random strings.** A
   32-char random hex query returns ~10 substring/Levenshtein matches
   with 1-2% similarity. Unit tests assumed "bogus query → empty
   results" — the live behavior is "bogus query → noisy ranked results
   with low scores". The contract is fundamentally different from what
   we'd guessed from the API docs.

The pattern: **unit tests verify your code does what you think the
backend does. Integration tests verify what the backend actually
does.** Both are needed. Unit tests for fast iteration on code logic;
live tests as a contract check.

---

## On the trust roots

`services/auth.py` and `services/crypto.py` are at **100% coverage
each**. This is non-accidental — these are the security-critical
modules where any bug compromises every user operation:

- `crypto.py` corruption = unrecoverable data loss
- `auth.py` corruption = wrong account exposed / credentials leaked

The full coverage cost was modest (around 30 tests across both),
because both modules are deliberately thin and pure-functional. The
mistake to avoid is letting these grow side-effects (file I/O,
network calls, mutable state) — that makes them un-coverable without
mocking, and once you're mocking the trust root, you've lost the
guarantee.

Specific tests that earned their keep:
- **AES-256-CTR round-trip across 0 B, 1 B, 16 B, 1 KB, 64 KB, 1 MB.**
  Catches off-by-one errors at the block boundary.
- **`encrypt_text` random-salt assertion.** Two encryptions of the
  same plaintext must produce different ciphertexts — catches anyone
  "optimizing" by hoisting salt generation out of the function.
- **`decrypt_meta` returns `None` (not raises) on bad input.** Pinned
  down because the calling code branches on `if decrypted is not
  None`; an exception would crash file listing.
- **`generate_filename_encryption_iv` hmac bug regression** — the
  bug we found via mypy. Test verifies `digestmod=hashlib.sha512`
  is correctly threaded.
- **End-to-end cycle test**: encrypt → "wire" → decrypt → assert
  byte-for-byte recovery. Catches any drift in the protocol.

---

## On testing the WebDAV provider

WebDAV is the largest user-visible surface and the hardest to unit
test, because every operation requires a populated wsgidav environ
dict (`{'wsgidav.provider': ..., 'PATH_INFO': ..., ...}`). We worked
around this by:

1. **Constructing resources via `__new__`** rather than `__init__`,
   then seeding attributes manually:

   ```python
   r = InternxtDAVResource.__new__(InternxtDAVResource)
   r.path = '/test'
   r.environ = {'wsgidav.provider': _FakeProvider()}
   r.file_metadata = {}
   r._upload_buffer = None
   ```

2. **Module-level injection of fake submodules.** For testing the
   server-startup branches we couldn't `patch('cheroot.wsgi', ...)`
   because cheroot is a real module without a `wsgi` attribute (it's
   a submodule). Solution: `sys.modules['cheroot.wsgi'] =
   fake_wsgi_module`.

3. **Patching `webdav_api._get_isolated_session`** rather than the
   underlying ApiClient — because the WebDAV provider intentionally
   builds a thread-local API client per request, and patching at the
   client level wouldn't intercept all of them.

These got us to 91% provider coverage, but everything is stubbed.
Real HTTP traffic against a running server is not exercised — see
`PLAN.md` section F for a proposed live-WebDAV test rig.

---

## On safety patterns for live tests

Several pitfalls came up while writing tests against a real (single,
shared, non-throwaway) Internxt account.

### Sentinel folder

Every operation must happen inside a known-prefix folder
(`/__pytest_internxt_cli_smoke__/<run-uuid>/`) so cleanup can
indiscriminately trash everything under it. Without this, a buggy
test that uploads to a path it computed wrong could pollute the
user's real data.

### Unique names per call

Every file/folder name in a live test gets a UUID suffix via
`_unique_name()`. This is for `pytest-rerunfailures`: when a flaky
test gets retried, the prior attempt may have already created an
entry with the test's "logical" name (say, `before.txt`). Without
uniqueness, the second attempt hits 409 Conflict and the rerun fails
deterministically — defeating the whole point of auto-retry.

This was learned the hard way. The first pass used hardcoded names
and we got cascading failures whenever any test was flaky.

### Auto-skip without creds

The whole live suite is gated on `IXT_ACCOUNT` and `IXT_PWD` env
vars (loaded from `.env` if present). Without them, every test in
the file is skipped. This makes the suite **safe to commit** —
contributors who don't have creds, or CI with no secrets, run the
552 unit tests and skip the 28 live tests cleanly.

### Cleanup in `try/finally`

Module-scope teardown trashes the entire sentinel folder, regardless
of whether tests pass or fail. Because Internxt's trash retains items
for 30 days, even if cleanup itself fails the user can recover via
the web UI. **Defense in depth on a shared resource.**

### The `.env` gitignore audit

Before committing anything, verify with `git check-ignore .env` AND
`git diff --cached --name-only | grep .env`. Both must come back
clean. Done at every commit involving live-test changes. The
gitignore file alone isn't enough — `git add .` could in principle
pick it up if someone reorders the rules.

---

## On rate limiting and eventual consistency

The Internxt backend exhibits both, and the live tests catch them
inconsistently:

- **Rate limiting**: under sustained operations (~30+ requests in a
  minute), some endpoints start returning generic 5xx errors with
  bodies that don't parse as JSON. Our wrapper converts these to
  `ValueError("API Error: Unknown Error")` — informative enough that
  a human can debug, but not specific enough to retry on.

- **Eventual consistency**: server-side fuzzy search needs ~2-10s
  after an upload before the new file appears in search results. The
  `test_live_search_finds_uniquely_named_file` test explicitly retries
  up to 5 times with a 2s delay, then `pytest.skip`s rather than
  failing — because the latency is outside our control and isn't a
  CLI bug.

The mitigation is **`pytest-rerunfailures` with `reruns=2,
reruns_delay=2`**. Combined with per-call unique names, the live
suite stabilized at 100% pass rate over consecutive runs (28/28 ×3).

---

## On crypto compatibility annotations

Several Bandit findings (SHA1 in PBKDF2, MD5 in OpenSSL EVP key
derivation) are unfixable: they're required for wire-protocol
compatibility with the Internxt server. Our approach:

- `# nosec B303` / `# nosec B324` to silence Bandit
- `usedforsecurity=False` keyword on the `hashlib.md5(...)` call
  (Python 3.9+ feature that signals "this hash is for non-security
  purposes" — protects against future hardened Python builds that
  might disable MD5 entirely)
- A comment explaining *why* this exists (not "MD5 is bad" — that's
  obvious — but "MD5 is required to match OpenSSL's `EVP_BytesToKey`,
  which the Internxt CLI uses for credential file encryption. Do not
  change.")

The point of the comment: when someone in 18 months sees
`hashlib.md5(...)` and tries to "fix" it, they hit a wall of
explanation telling them not to.

The same approach should be used for any other security-flagged code
that's intentional. Always have a `# nosec` *with reason*.

---

## On code-smell-to-bug correlation

Five separate findings across this audit started as "minor cleanup
tasks" and turned out to be hiding real bugs:

1. **Duplicate function definitions** (3 in `drive.py`, 2 in `api.py`).
   Removed as dead code. One of them (`create_folder_recursive`)
   turned out to have *different* logic than the surviving copy —
   the dead version had a more aggressive retry loop that we lost.
   Re-evaluating: should one of them have been kept? Conclusion: no,
   the surviving version is correct, but the duplicate was a sign of
   a half-finished refactor.

2. **Bare `except:`** — looks lazy, but `webdav_provider.py:101`
   was masking `KeyboardInterrupt` and `SystemExit` from the user.
   Replacing with `except OSError` (the actual exception that can
   happen during temp-file cleanup) restored normal Ctrl+C handling.

3. **Empty f-strings** (`f"static text"`) — pure cosmetic, but they
   were ALSO appearing in error-message formatting code that we had
   to look at to understand other bugs. Cleaning them up reduced
   eyeball-load when reading the error paths.

4. **`# pylint: disable=unused-import` on dependency probes** —
   ruff didn't honor the pylint directive, so we had to add explicit
   `# noqa: F401` comments. Each one is a place where a future
   contributor might think "this is unused, delete it" and break the
   ImportError-for-helpful-message pattern.

5. **`DRIVE_WEB_URL` config key referenced by `cli.py config`** —
   commented out of the config dict an unknown amount of time ago.
   Crashed the `config` command on every invocation. This wasn't a
   warning anywhere; just dead code with consequences.

The pattern: **investigate every "why is this here" before deleting.**
Half the time it's dead code; the other half it's load-bearing.

---

## On the relationship between mypy and runtime

Several mypy findings turned out to be 100% real bugs at runtime:

- `hmac.new(key, hashlib.sha512)` — would `TypeError` at runtime
- `_upload_buffer.cleanup()` in a `finally` clause where the
  attribute might never have been set — would `AttributeError`,
  masking the original exception
- `decrypt_meta` declared `-> str` but could return `None` — would
  hit `'NoneType' has no attribute ...` in callers

Several others were noise:
- "Incompatible default for argument" (PEP 484 implicit Optional) —
  cosmetic
- "Incompatible types in assignment" inside Dict[str, Any] containers
  — type narrowing limitation, not a bug

Lesson: **mypy errors with `[union-attr]` and `[call-overload]` are
almost always real bugs. Errors with `[assignment]` and
`[arg-type]` inside dynamic dicts are usually noise.**

---

## On the value of a comprehensive in-CLI smoke test

`python cli.py test` (a 7-assertion in-process check that doesn't
need pytest installed) was already in the codebase before the audit
— we just had to fix two stale URL assertions to make it pass.
Having this is valuable because:

- It runs in <1s
- It needs no test framework
- It runs the actual import chain, not a stubbed version
- A user reporting a bug can run it and prove their install is OK

This pattern is worth keeping. New audit-hardening features should
add an assertion to `cli.py test` if they're checking a startup
invariant (config keys, import side effects, dependency presence).

---

## What I'd do differently next time

1. **Set up `.env` and gitignore it FIRST**, before any test that
   uses creds. We did this safely but in a hurry; structuring it
   as the first action would have removed the risk window.

2. **Add `pytest-rerunfailures` and per-call unique names from day 1**
   for any live tests. We added them iteratively after watching the
   first few full-suite runs fail flakily. Would have saved time.

3. **Test the cache layer separately from the operations layer.**
   The `create_folder_recursive` bug was hidden because we mocked
   `get_folder_content` (the cached interface) directly in unit
   tests. Testing the cache invalidation independently — i.e.,
   "after operation X, is the cache for parent Y invalidated?" —
   would have caught it without needing live tests.

4. **Run the suite with `-p no:cacheprovider`** when sanity-checking
   coverage, because pytest's own cache made me think tests were
   passing when they were actually being skipped due to changed
   collection state.

5. **Investigate every "this looks weird" comment in the original
   code first**, before writing tests around it. Half the inline
   `TODO` / `# FIXME` / `# weird, but...` comments were warnings
   about real bugs.

---

## Final numbers

| Metric | Value |
|---|---|
| Total tests | 585 (557 unit + 28 live) |
| Total coverage | 90% |
| Trust-root coverage | 100% (`auth.py`, `crypto.py`) |
| Bugs found and fixed | 17 (15 audit + 2 from live tests) |
| Bandit medium+ findings | 0 |
| ruff errors | 0 |
| mypy errors | 0 |
| Unit suite runtime | ~3 seconds |
| Live suite runtime | ~60-90 seconds |
| Lines of test code | ~3500 |
| Lines of production code | ~2700 |
| Test-to-production ratio | 1.3:1 |
