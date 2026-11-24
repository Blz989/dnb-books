# Manual backup with progress (design)

Status: in review. Plan: `docs/superpowers/plans/2026-11-24-manual-backup-progress.md`.

## Problem

Nightly backups often fail with "Backup saved locally but not uploaded: rclone took longer than 15 minutes." A backup should take a minute or two. The owner can't start a backup from the app or see where the time goes.

The 15-minute limit applies only to the rclone upload (`backup/remote.py`). The database copy, packing, and encryption finish before it. rclone's Google Drive docs say the built-in client ID is shared by all rclone users and is rate-limited, and that a private client ID avoids this (https://rclone.org/drive/#making-your-own-client-id). rclone logs those rate-limit retries only at debug level (`fs/pacer.go`: "low level retry", "Rate limited, increasing sleep").

## What changes

1. **Back up now button.** On the home screen Backups card, admins only. It queues a backup; the scheduler service picks it up within 5 seconds and runs it with the same code as the nightly run. A second request while one is waiting or running is refused: "A backup is already running."
2. **Progress bar.** Shown on the Backups card to every signed-in user while a backup is waiting or running, updated every second. Steps, in order: Checking settings, Copying the database, Packing uploaded files, Encrypting, Uploading the off-site copy, Removing old backups. During the upload the line under the bar shows MB sent of total, speed, time left, and how many times Google asked rclone to slow down.
3. **Step timings on every run**, nightly and manual. The card shows the last run's total time and its slowest step. When a run fails, it shows the step it stopped in.
4. **Better upload failure text.** The timeout message keeps its first sentence and adds how far the upload got and the rate-limit count, for example: "rclone took longer than 15 minutes. It sent 12.4 MB of 40.1 MB. Google asked it to slow down 37 times."
5. **Stuck runs.** A run with no progress update for 20 minutes is marked failed: "The backup stopped without finishing. The server may have restarted during it." A request the scheduler hasn't started after 10 minutes is marked failed: "The scheduler didn't start this backup. Check that the scheduler service is running." After 60 seconds of waiting the card already says: "Waiting for the scheduler to start it."
6. **README.** The button, what the progress shows, and how to set up a private Google client ID when the upload is slow.

## Not changing

- The 15-minute upload limit, the backup format, retention, and the restore drill.
- No new dependencies. rclone's own `--use-json-log --stats=1s` output is the progress source.

## Data

`BackupRun` gains: status `QUEUED` ("Waiting"), `step`, `progress` (0 to 100), `detail` (the line under the bar), `step_times` (list of `[step, seconds]`), `requested_by` (user, null for nightly), `updated_at`.
