# Manual backup with progress (plan)

Spec: `docs/superpowers/specs/2026-11-24-manual-backup-progress-design.md`. PR title: "Back up now button with live progress".

Executed inline (small change), TDD per task. Tests in `tests/test_backup_progress.py`.

## Tasks

### Task 1. Streamed rclone upload with progress
- [x] Failing tests: upload reports bytes, total, speed, and rate-limit retries from rclone's JSON log; the timeout message says how far it got; JSON-mode error text is parsed.
- [x] `remote.upload(path, on_progress=None)` runs rclone with `--use-json-log --stats=1s --stats-log-level=NOTICE --log-level=DEBUG`, reads stderr on a thread, enforces the 15-minute limit, kills rclone on timeout.
- [x] `tests/fake_rclone.py` ignores flags and prints JSON stats lines; `FAKE_RCLONE_SLOW` and `FAKE_RCLONE_RETRIES` simulate a slow, throttled upload.
- Commit: "Backups: stream rclone upload progress"

### Task 2. Steps, progress, and timings on BackupRun
- [x] Failing tests: a run records each step in order with its time, ends at 100 percent; a failed run keeps the step it stopped in.
- [x] Migration `0002`; a progress reporter in `backup/service.py` saves at most twice a second, plus on every step change.
- Commit: "Backups: record steps, progress, and step timings"

### Task 3. Queue, scheduler pickup, stuck runs
- [x] Failing tests: `request_backup` queues one run and refuses a second; `run_queued` runs it; the scheduler wakes at least every 5 seconds; stale runs are marked failed with the spec's messages.
- Commit: "Backups: queued manual backups run by the scheduler"

### Task 4. Button, progress bar, status JSON
- [ ] Failing tests: admins see the button, others don't; non-admin POST is refused; status JSON reports step, percent, detail.
- [ ] `backup/urls.py`, `backup/views.py`, `backup/static/backup/progress.js`, home card markup, CSS.
- [ ] Screenshots: idle, waiting, uploading with retries, finished, failed, non-admin view; light and dark.
- Commit: "Home: Back up now button with live progress"

### Task 5. Docs
- [ ] README Backups section and Status line; CLAUDE.md source of truth.
- Commit: "Docs: manual backup and slow upload help"
