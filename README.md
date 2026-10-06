# Yahoo Mail Backup

A small, dependency-light Python script to back up your **entire Yahoo Mail
mailbox** to your own computer — every folder, every message, every
attachment.

## Why this exists

Yahoo's own ["Download my data"](https://legal.yahoo.com/us/en/yahoo/privacy/dashboard/index.html)
tool only exports account/profile metadata as a small JSON file. It does
**not** include your actual email content, and Yahoo has no equivalent of
Google Takeout for Mail. This script fills that gap using plain IMAP — the
same protocol Thunderbird, Apple Mail, and Outlook use — so you get a real,
complete local copy of your mail.

## Features

- Downloads every folder, or just the ones you name
- Saves as one `.mbox` file per folder (default, works with Thunderbird,
  Apple Mail, `mutt`, etc.) or as individual `.eml` files
- **Resumable**: tracks progress per folder in `backup_state.json`. If your
  connection drops at message 400,000 of 639,000, just re-run the script —
  it picks up where it left off instead of starting over
- Uses `BODY.PEEK[]` so messages are never marked as read on the server
- Automatic retry with backoff on network errors, and periodic reconnects
  for very long-running backups
- No paid software, no third-party servers — your mail goes straight from
  Yahoo to your disk

## Setup

1. **Generate a Yahoo app password** (required if you have 2-step
   verification enabled, and recommended either way):
   Yahoo Account → Account Security → **Generate app password** → choose
   "Other App" and name it something like "Mail Backup".

2. **Install Python 3.8+** if you don't have it.

3. Clone this repo and install the one optional dependency:

   ```bash
   git clone <this-repo-url>
   cd yahoo-mail-backup
   pip install -r requirements.txt
   ```

   (`tqdm` is only used for a progress bar — the script works fine without
   it if you'd rather skip this step.)

## Usage

```bash
python yahoo_backup.py --email you@yahoo.com
```

You'll be prompted for your app password (or set `YAHOO_APP_PASSWORD` as an
environment variable to skip the prompt). By default everything is saved
under `./yahoo_mail_backup/` as one `.mbox` file per folder.

### Useful options

```bash
# See what folders you have before committing to a full backup
python yahoo_backup.py --email you@yahoo.com --list-folders

# Back up just Inbox and Sent
python yahoo_backup.py --email you@yahoo.com --folder INBOX --folder "Sent"

# Save as individual .eml files instead of .mbox
python yahoo_backup.py --email you@yahoo.com --format eml

# Custom output location
python yahoo_backup.py --email you@yahoo.com --output /path/to/backup

# Add a small delay between batches if you're worried about rate limits
python yahoo_backup.py --email you@yahoo.com --delay 0.5
```

Full flag list: `python yahoo_backup.py --help`

### Resuming an interrupted backup

Just run the exact same command again. Progress is stored in
`backup_state.json` inside your output directory, keyed by folder and the
server's `UIDVALIDITY`, so it will only fetch messages it hasn't saved yet.

## A note on mailbox size

If you have hundreds of thousands of emails, this will take a while and use
real disk space — plan for roughly the size Yahoo reports your mailbox as
using. Consider deleting or archiving old newsletters/promotions in Yahoo
Mail first if you want a smaller backup.

## Other providers

Despite the name, this works with any IMAP provider — just pass a different
`--server` (e.g. `--server imap.gmail.com` for Gmail, though Gmail already
has Takeout).

## Contributing

Issues and PRs welcome — this was written to fix a real gap in Yahoo's data
portability tools, so improvements that make it more robust for large
mailboxes are especially appreciated.

## License

MIT — see [LICENSE](LICENSE).
