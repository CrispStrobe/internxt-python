# Changelog

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
