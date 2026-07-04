# Internxt Python CLI

A Python implementation of the Internxt CLI for encrypted cloud storage with **path-based operations**, **timestamp preservation**, and a **built-in WebDAV server**.

## ⚠️ Disclaimer

This is an unofficial, open-source project and is **not** affiliated with, endorsed by, or supported by Internxt, Inc. It is a personal project built for learning and to provide an alternative interface. Use it at your own risk.

## ✨ Features

### 🌐 **WebDAV Server**

  - ✅ **Mount as a local drive**: Access your Internxt Drive directly from Finder, File Explorer, or any WebDAV client.
  - ✅ **Cross-platform support**: Works on Windows, macOS, and Linux.
  - ✅ **Stable and Compatible**: Uses `waitress` (recommended) or `cheroot` for the best client compatibility.
  - ✅ **Server Choice**: Force `waitress` or `cheroot` with the `--server` flag for debugging.

### 🛣️ **Path-Based Operations**

  - ✅ **Human-readable paths**: Use `/Documents/report.pdf` instead of UUIDs.
  - ✅ **Fuzzy Search**: Instantly find files and folders with a fast, server-side `search` command.
  - ✅ **Wildcard Find**: Use `find` with patterns like `*.pdf`, `report*`, etc.
  - ✅ **Tree visualization**: See your folder structure at a glance.
  - ✅ **Path navigation**: Browse folders like your local filesystem.

### 🔐 **Core Functionality**

  - ✅ **Timestamp Preservation**: Preserves original file modification/creation dates on `upload` and `download-path`.
  - ✅ **Secure authentication**: Login/logout with 2FA support.
  - ✅ **File operations**: Upload, download with progress indicators.
  - ✅ **Stream from stdin (`rcat`)**: Pipe data straight to a Drive file (rclone-`rcat` style) for unattended backups — `mariadb-dump | xz | cli.py rcat /backups/db.xz`.
  - ✅ **Large files**: Streaming encrypt/upload and decrypt/download keep memory bounded (a few MB) regardless of file size; files ≥ 100 MiB **automatically** use true multipart upload (30 MB parts, uploaded in parallel and each retried independently) for resilient transfers on slow/flaky connections. Parallel **ranged downloads** are available opt-in via `--ranged`.
  - ✅ **Folder management**: Create and organize folders.
  - ✅ **Zero-knowledge encryption**: AES-256-CTR client-side encryption.

## 🚀 Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# (Recommended for WebDAV)
pip install waitress

# Login to your account
python cli.py login

# Mount your drive locally! (EASIEST WAY TO USE)
python cli.py webdav-start

# Or, use path-based commands
python cli.py list-path
python cli.py search "report"
python cli.py find /Documents "*.pdf"
python cli.py upload -r -p ./my-docs /Backups
```

## 📖 Usage Guide

### 🔐 Authentication

```bash
# Login with interactive prompts
python cli.py login

# Login non-interactively
python cli.py login --email user@example.com --password mypass --2fa 123456

# Check current user
python cli.py whoami

# Logout and clear credentials
python cli.py logout
```

#### Supplying credentials securely

Ways to provide the password, most to least secure:

```bash
# 1. Pipe via stdin — never appears in shell history, argv, or the process list
printf '%s' "$MY_PASSWORD" | python cli.py login -e you@example.com --password-stdin

# 2. Environment variables (good for CI; avoid `export`-ing into your shell rc)
INTERNXT_EMAIL=you@example.com INTERNXT_PASSWORD="$MY_PASSWORD" python cli.py login

# 3. The -e/-p flags  ⚠️  leak into shell history and `ps` — avoid on shared hosts
```

For 2FA in automation, pass the TOTP secret instead of a one-off code:
`--tfa-secret` (or `INTERNXT_TFA_SECRET`) auto-generates the 6-digit code.

**Implicit auto-login.** You don't strictly need to run `login` first: when no
valid session is stored, every command (e.g. `rcat`) auto-logs-in from
`INTERNXT_EMAIL` / `INTERNXT_PASSWORD` (+ `INTERNXT_TFA_SECRET` if 2FA is on).
This also kicks in transparently if a stored token has expired. Set
`INTERNXT_DEBUG=1` to surface the internal auth trace (off by default; it goes
to stderr so it never pollutes piped output).

#### How the session is stored

After login the session (which includes your **mnemonic** — the key to all your
files) is encrypted at rest in `~/.internxt-cli/.inxtcli` (file `0600`, dir `0700`).
The encryption key is sourced in this order:

1. **OS keychain** (macOS Keychain / Linux Secret Service / Windows Credential
   Manager) via `keyring` — a random per-install key; **recommended**. Install
   with `pip install keyring` if not already present.
2. **`INTERNXT_CREDENTIALS_KEY`** env var — a key you supply (handy for CI where
   there's no desktop keychain).
3. **Legacy static key** — obfuscation only; used as a last resort if neither of
   the above is available. The `0600` permission is your real protection here.

Set `INTERNXT_NO_KEYRING=1` to skip the OS keychain (forces option 2/3). Legacy
credential files from older versions are read and upgraded automatically.

### 🌐 WebDAV Server

Mount your Internxt Drive as a local disk.

```bash
# Start the WebDAV server (it will print the URL and credentials)
python cli.py webdav-start

# Start in the background
python cli.py webdav-start --background

# Force a specific server (e.g., cheroot for SSL)
python cli.py webdav-start --server cheroot

# Check if the server is running
python cli.py webdav-status

# Stop the server
python cli.py webdav-stop

# Show mount instructions for your OS
python cli.py webdav-mount

# Test if the server is responding correctly
python cli.py webdav-test

# Show full WebDAV configuration and paths
python cli.py webdav-config

# Show advanced debugging info
python cli.py webdav-debug

# Regenerate SSL certs
python cli.py webdav-regenerate-ssl
```

After starting, open your file manager (Finder/File Explorer) or Client (like CyberDuck) and connect to the server (e.g., `http://localhost:8080`) with username `internxt` and password `internxt-webdav`.

### 🛣️ Path-Based Operations

#### List & Navigate

```bash
# List root folder with readable paths
python cli.py list-path

# List specific folders
python cli.py list-path "/Documents"
python cli.py list-path "/Photos/2023/Summer"

# Show detailed information (size, date)
python cli.py list-path "/Documents" --detailed

# Show folder structure as tree
python cli.py tree
python cli.py tree "/Projects" --depth 2
```

#### Search & Find

```bash
# Fast, global, server-side fuzzy search
python cli.py search "report"

# Show full details (size, date, full path) (slow!)
python cli.py search "report" --detailed

# Slow, client-side wildcard find (POSIX-like syntax, slow!)
python cli.py find / "*.pdf"              # All PDF files in entire drive
python cli.py find /Photos "*.jpg"        # All JPGs in /Photos
python cli.py find . "report*"            # Files starting with "report" in current path
```

#### ⬆️⬇️ Upload & Download

**Upload**

```bash
# Upload a single file, preserving its timestamp
python cli.py upload -p ./local-report.pdf /Documents/

# Upload a whole folder recursively, preserving all timestamps
python cli.py upload -r -p ./my-project /Backups/

# Upload with filters and overwrite conflicts
python cli.py upload -r ./photos /Photos --include "*.jpg" --on-conflict overwrite
```

**Stream from stdin (`rcat`)**

```bash
# Pipe a stream straight to a Drive file (rclone-rcat style) — great for
# unattended backups without a named local file. REMOTE_PATH includes the
# filename; the parent folder is created if missing.
mariadb-dump mydb | xz -6 | python cli.py rcat /backups/mydb.xz
tar czf - /etc           | python cli.py rcat /backups/etc.tar.gz

# Auto-login: if you're not logged in, rcat (and the other commands) log in
# from INTERNXT_EMAIL / INTERNXT_PASSWORD (+ INTERNXT_TFA_SECRET for 2FA), so a
# pipeline needs no separate `login` step:
INTERNXT_EMAIL=you@example.com INTERNXT_PASSWORD="$PW" \
  pg_dump -Fc mydb | python cli.py rcat /backups/mydb.dmp
# ...or keep them separate and trap each phase:
python cli.py login -e you@example.com --password-stdin <<<"$PW" \
  && PGPASSWORD="$DBPW" pg_dump -Fc mydb | python cli.py rcat /backups/mydb.dmp \
  && python cli.py logout

# Upstream passwords: while piping into rcat the dump tool has no terminal, so
# it can't prompt for ITS OWN db password — supply it non-interactively, e.g.
# PGPASSWORD / ~/.pgpass (Postgres) or MYSQL_PWD / ~/.my.cnf (MySQL/MariaDB).

# Note: Internxt needs the exact size up front, so rcat first spools stdin to a
# temp file to measure it, then encrypts+uploads in one pass. Ensure free temp
# space for the whole stream (use --temp-dir to relocate the spool).
```

**Download**

```bash
# Download a file by path, preserving its timestamp
python cli.py download-path -p "/Documents/report.pdf"

# Download a folder recursively to a local directory
python cli.py download-path -r "/Photos/2023" --destination ./My-Photos

# Download a folder with filters
python cli.py download-path -r "/Music" --include "*.mp3" --exclude "demo_*"

# Opt in to parallel ranged downloads for large files (≥ 100 MiB). Off by
# default; falls back to a single stream if the server ignores HTTP Range.
python cli.py download-path -p "/Backups/bigdump.xz" --ranged --chunk-workers 4
```

### 🗑️ Delete & Trash Operations

#### Move to Trash (Recoverable)

```bash
# Move to trash by path
python cli.py trash-path "/OldDocuments/outdated.pdf"
python cli.py trash-path "/TempFolder"
```

#### Permanent Delete (⚠️ Cannot Be Undone)

```bash
# Permanently delete by path (with warnings)
python cli.py delete-path "/TempFile.txt"
```

### 📁 Traditional Operations (UUID-based)

```bash
# List folders (old way with UUIDs)
python cli.py list
python cli.py list --folder-id <folder-uuid>

# Create folders
python cli.py mkdir "My New Folder"

# Upload/Download by UUID (see path-based commands for more features)
python cli.py upload ./document.pdf
python cli.py download <file-uuid>
```

### 🔧 Utility Commands

```bash
# Show current configuration
python cli.py config

# Test CLI components
python cli.py test

# Extended help with examples
python cli.py help-extended

# Debug path resolution
python cli.py resolve "/Documents/report.pdf"
```

## 📦 Installation

```bash
# Clone the repository
git clone https://github.com/CrispStrobe/internxt-python
cd internxt-python

# Install dependencies
pip install -r requirements.txt

# For the best WebDAV experience, install 'waitress'
pip install waitress  # (Highly recommended for WebDAV)

# Start using immediately
python cli.py login
python cli.py webdav-start
```

### Requirements

  - **Python 3.8 – 3.14**
  - **Dependencies**: `cryptography`, `mnemonic`, `tqdm`, `requests`, `click`, `WsgiDAV`
  - **WebDAV Server**: `waitress` (recommended) or `cheroot`
  - **OpenPGP login keys** are generated by a built-in backend (uses
    `cryptography`; no extra dependency, all Python versions). Optional
    alternatives are auto-detected if installed: `PGPy` (Python < 3.13 only) or
    `python-gnupg` (needs a system `gpg` binary) — `pip install '.[pgpy]'` /
    `'.[gnupg]'`.

## 🔒 Security & Privacy

This CLI implements **the same security model** as official Internxt clients:

  - **Client-side encryption**: All files encrypted on your device before upload (AES-256-CTR).
  - **Zero-knowledge**: Internxt servers never see your unencrypted data or keys.
  - **Secure Credentials**: Encrypted and stored locally in `~/.internxt-cli/`.

## 🏗️ Development

### Project Structure

```
internxt-python/
├── cli.py                    # Main CLI interface with all commands
├── config/
│   └── config.py             # Configuration management
├── services/
│   ├── auth.py               # Authentication & login
│   ├── crypto.py             # Encryption/decryption (AES-256-CTR)
│   ├── drive.py              # Drive operations & path resolution
│   ├── network_utils.py      # SSL cert lifecycle, range parsing
│   ├── webdav_provider.py    # WsgiDAV provider for Internxt
│   └── webdav_server.py      # WebDAV server management
├── utils/
│   └── api.py                # HTTP API client
├── tests/                    # Pytest suite (622 tests, 90% coverage)
├── pyproject.toml            # Pytest, coverage, ruff config
├── requirements-dev.txt      # Dev/test dependencies
└── .github/workflows/ci.yml  # Lint + type-check + test on Py 3.10/3.11/3.12
```

### Development Setup

```bash
# Clone and setup development environment
git clone https://github.com/CrispStrobe/internxt-python.git
cd internxt-python

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/macOS

# Install in development mode
pip install -e .
pip install -r requirements-dev.txt
```

### Running Tests

The project ships with a 591-test unit suite at **90% line coverage**
(plus 31 optional live tests — 622 total).
It covers crypto round-trips, path resolution, upload/download conflict
handling, the WebDAV provider, all major CLI commands, and a real
encrypt → upload → "wire" → download → decrypt round-trip cycle.

```bash
# Run the full test suite (~3 seconds)
pytest

# With coverage report
pytest --cov=services --cov=utils --cov=config

# Run a specific test file
pytest tests/test_crypto.py -v

# In-CLI smoke check (no test framework needed)
python cli.py test
```

Per-module coverage:

| Module                          | Coverage |
|---------------------------------|---------:|
| `services/auth.py`              |    100% |
| `services/crypto.py`            |    100% |
| `utils/api.py`                  |     98% |
| `services/webdav_provider.py`   |     91% |
| `services/network_utils.py`     |     90% |
| `services/drive.py`             |     89% |
| `config/config.py`              |     85% |
| `services/webdav_server.py`     |     83% |
| **Total**                       |    **90%** |

### Project documentation

| File | Purpose |
|---|---|
| [`HISTORY.md`](HISTORY.md) | What's been done — full audit summary + every bug found and fixed during the test build-out. |
| [`PLAN.md`](PLAN.md) | What's left — roadmap of unimplemented work: confirmed gaps vs. upstream `internxt/cli`, potential differentiators (folder copy, quota), WebDAV-providers reliability test, known maintenance debt. |
| [`LEARNINGS.md`](LEARNINGS.md) | Lessons carried forward — what each audit tool actually catches, why unit tests miss certain bugs, safety patterns for live tests against real accounts. |

#### Live integration smoke (optional)

`tests/test_live_smoke.py` is a 31-test suite that runs end-to-end
against the real Internxt backend, covering: login, list, upload (small
+ unicode + extensionless + 2 MB), download with byte-for-byte
verification, recursive folder creation, file rename/move/copy/update,
folder rename/move, trash, server-side fuzzy search, and client-side
wildcard find. Auto-skipped unless credentials are present.

```bash
# Put creds in a .env file (gitignored — never committed)
echo 'IXT_ACCOUNT=you@example.com' > .env
echo 'IXT_PWD=your-password' >> .env

# Runs in ~60-90 seconds, always cleans up
pytest tests/test_live_smoke.py -v
```

All operations happen inside a unique sentinel folder
(`/__pytest_internxt_cli_smoke__/<run-uuid>/`) which is always trashed
at teardown — your real files are never touched. Every file/folder name
within a test includes a UUID suffix so transient retries never
collide. Transient API failures (rate-limit, eventual-consistency) are
auto-retried via `pytest-rerunfailures`.

### Quality Gates

The CI runs four gates on every push/PR:

```bash
ruff check .                                                           # lint
mypy --no-incremental cli.py services                                  # types
bandit -r . -x ./.mypy_cache,./__pycache__,./.git,./tests -ll          # security (medium+)
pytest --cov=services --cov=utils --cov=config                         # tests
```

All four must pass.

### Getting Help

```bash
python cli.py --help
python cli.py help-extended
python cli.py <command> --help
```

## 📄 License

**AGPL-3.0 license**

-----

*Made with ❤️ for the Internxt community*