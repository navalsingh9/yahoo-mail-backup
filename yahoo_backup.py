#!/usr/bin/env python3
"""
yahoo_backup.py — Back up your entire Yahoo Mail mailbox over IMAP.

Yahoo's own "Download my data" tool only exports account/profile metadata,
not your actual email content, and Yahoo has no bulk "Takeout"-style export
for Mail. This script connects over IMAP (the same protocol Thunderbird or
Outlook use) and saves every message in every folder to disk, either as one
.mbox file per folder or as individual .eml files.

It is resumable: progress (per folder, per UIDVALIDITY) is saved to a JSON
state file, so if your connection drops partway through 600k+ emails you can
just re-run the script and it will continue where it left off instead of
re-downloading everything.

Usage:
    python yahoo_backup.py --email you@yahoo.com

    # Yahoo requires an app password, not your normal account password:
    # Account Security -> "Generate app password" -> create one for "Other app"

Environment variables (alternative to flags, useful for not typing secrets):
    YAHOO_EMAIL
    YAHOO_APP_PASSWORD

See README.md for full setup instructions.
"""

import argparse
import getpass
import imaplib
import json
import mailbox
import os
import socket
import sys
import time
from email import message_from_bytes
from pathlib import Path

try:
    from tqdm import tqdm
    HAVE_TQDM = True
except ImportError:
    HAVE_TQDM = False

DEFAULT_SERVER = "imap.mail.yahoo.com"
DEFAULT_PORT = 993
STATE_FILENAME = "backup_state.json"
# How many messages to request from the server in a single FETCH command.
DEFAULT_BATCH_SIZE = 50
# Reconnect every N batches to avoid the server silently dropping a long-lived
# connection during a multi-hour run.
RECONNECT_EVERY_N_BATCHES = 200


def log(msg):
    print(msg, flush=True)


def safe_folder_name(imap_folder_name: str) -> str:
    """Turn an IMAP folder name (which may contain '/', spaces, etc.) into a
    filesystem-safe name."""
    name = imap_folder_name.strip('"')
    for ch in ('/', '\\', ':', '*', '?', '"', '<', '>', '|'):
        name = name.replace(ch, '_')
    return name or "INBOX"


class BackupState:
    """Tracks, per folder, the UIDVALIDITY and the highest UID already
    downloaded, so the script can resume instead of starting over."""

    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = {}

    def get(self, folder):
        return self.data.get(folder, {"uidvalidity": None, "last_uid": 0, "count": 0})

    def update(self, folder, uidvalidity, last_uid, count_delta):
        entry = self.data.get(folder, {"uidvalidity": uidvalidity, "last_uid": 0, "count": 0})
        if entry.get("uidvalidity") != uidvalidity:
            # Server renumbered this folder (rare). Start this folder over.
            entry = {"uidvalidity": uidvalidity, "last_uid": 0, "count": 0}
        entry["last_uid"] = max(entry["last_uid"], last_uid)
        entry["count"] = entry.get("count", 0) + count_delta
        self.data[folder] = entry
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        tmp.replace(self.path)


def connect(server, port, email_addr, app_password):
    imap = imaplib.IMAP4_SSL(server, port)
    imap.login(email_addr, app_password)
    return imap


def list_folders(imap):
    status, folders_raw = imap.list()
    if status != "OK":
        raise RuntimeError(f"Could not list folders: {folders_raw}")
    folders = []
    for raw in folders_raw:
        if raw is None:
            continue
        decoded = raw.decode(errors="replace")
        # Folder name is the last quoted (or bare) token.
        if '"' in decoded:
            name = decoded.rsplit('"', 2)[-2]
        else:
            name = decoded.split(" ")[-1]
        folders.append(name)
    return folders


def get_uidvalidity(imap):
    status, data = imap.response("UIDVALIDITY")
    if data and data[0]:
        try:
            return int(data[0])
        except (ValueError, TypeError):
            pass
    # Fallback: explicit STATUS command.
    status, data = imap.status("INBOX", "(UIDVALIDITY)")
    return None


def fetch_batch(imap, uids):
    """Fetch a batch of UIDs. Uses BODY.PEEK[] so messages are NOT marked
    as read on the server."""
    uid_set = ",".join(str(u) for u in uids)
    status, data = imap.uid("FETCH", uid_set, "(BODY.PEEK[])")
    if status != "OK":
        raise RuntimeError(f"FETCH failed: {data}")
    messages = {}
    current_uid = None
    for part in data:
        if isinstance(part, tuple):
            header = part[0]
            raw_bytes = part[1]
            # header looks like: b'123 (UID 456 BODY[] {1234}'
            header_str = header.decode(errors="replace")
            uid_val = None
            if "UID" in header_str:
                try:
                    tokens = header_str.split()
                    idx = tokens.index("UID")
                    uid_val = int(tokens[idx + 1])
                except (ValueError, IndexError):
                    uid_val = None
            if uid_val is not None:
                messages[uid_val] = raw_bytes
    return messages


def backup_folder(imap, folder, out_dir: Path, fmt: str, state: BackupState,
                   batch_size: int, delay: float, reconnect_args, server, port):
    try:
        status, _ = imap.select(f'"{folder}"', readonly=True)
    except imaplib.IMAP4.error as e:
        log(f"  Skipping folder {folder!r}: cannot select ({e})")
        return

    if status != "OK":
        log(f"  Skipping folder {folder!r}: SELECT failed")
        return

    uidvalidity = get_uidvalidity(imap)
    entry = state.get(folder)
    start_after = entry["last_uid"] if entry.get("uidvalidity") == uidvalidity else 0

    status, data = imap.uid("SEARCH", None, f"UID {start_after + 1}:*")
    if status != "OK":
        log(f"  Skipping folder {folder!r}: SEARCH failed")
        return

    uid_list = [int(u) for u in data[0].split()] if data and data[0] else []
    # When searching "N:*" on an empty remainder, IMAP servers often return
    # the single highest existing UID even if it's <= start_after. Filter it.
    uid_list = [u for u in uid_list if u > start_after]

    if not uid_list:
        log(f"  {folder}: up to date ({entry.get('count', 0)} messages already saved)")
        return

    log(f"  {folder}: {len(uid_list)} new message(s) to download")

    safe_name = safe_folder_name(folder)
    mbox = None
    eml_dir = None
    if fmt == "mbox":
        out_dir.mkdir(parents=True, exist_ok=True)
        mbox = mailbox.mbox(str(out_dir / f"{safe_name}.mbox"))
        mbox.lock()
    else:
        eml_dir = out_dir / safe_name
        eml_dir.mkdir(parents=True, exist_ok=True)

    iterator = range(0, len(uid_list), batch_size)
    progress = tqdm(iterator, desc=f"  {folder}", unit="batch") if HAVE_TQDM else iterator

    batches_done = 0
    highest_uid_seen = start_after
    saved_count = 0

    try:
        for i in progress:
            batch_uids = uid_list[i:i + batch_size]
            attempt = 0
            while True:
                try:
                    messages = fetch_batch(imap, batch_uids)
                    break
                except (imaplib.IMAP4.error, socket.error, OSError) as e:
                    attempt += 1
                    if attempt > 5:
                        raise
                    wait = min(2 ** attempt, 30)
                    log(f"    fetch error ({e}); retrying in {wait}s "
                        f"(attempt {attempt})")
                    time.sleep(wait)
                    try:
                        imap.noop()
                    except Exception:
                        imap = connect(server, port, *reconnect_args)
                        imap.select(f'"{folder}"', readonly=True)

            for uid, raw in messages.items():
                try:
                    if fmt == "mbox":
                        msg = message_from_bytes(raw)
                        mbox.add(msg)
                    else:
                        (eml_dir / f"{uid}.eml").write_bytes(raw)
                    saved_count += 1
                    highest_uid_seen = max(highest_uid_seen, uid)
                except Exception as e:
                    log(f"    warning: failed to save UID {uid}: {e}")

            state.update(folder, uidvalidity, highest_uid_seen, saved_count)
            saved_count = 0  # reset delta counter after each state.update

            if fmt == "mbox":
                mbox.flush()

            batches_done += 1
            if delay:
                time.sleep(delay)

            if batches_done % RECONNECT_EVERY_N_BATCHES == 0:
                try:
                    imap.logout()
                except Exception:
                    pass
                imap = connect(server, port, *reconnect_args)
                imap.select(f'"{folder}"', readonly=True)
    finally:
        if mbox is not None:
            mbox.unlock()
            mbox.close()

    log(f"  {folder}: done")


def main():
    parser = argparse.ArgumentParser(
        description="Back up your Yahoo Mail (or any IMAP mailbox) to local .mbox or .eml files.")
    parser.add_argument("--email", default=os.environ.get("YAHOO_EMAIL"),
                         help="Your email address (or set YAHOO_EMAIL env var)")
    parser.add_argument("--app-password", default=os.environ.get("YAHOO_APP_PASSWORD"),
                         help="Yahoo app password (or set YAHOO_APP_PASSWORD env var). "
                              "If omitted you'll be prompted securely.")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                         help=f"IMAP server (default: {DEFAULT_SERVER}). "
                              "Works with Gmail/Outlook/etc too if you change this.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--output", default="./yahoo_mail_backup",
                         help="Output directory (default: ./yahoo_mail_backup)")
    parser.add_argument("--format", choices=["mbox", "eml"], default="mbox",
                         help="mbox = one file per folder (default). "
                              "eml = one file per message.")
    parser.add_argument("--folder", action="append",
                         help="Only back up this folder. Repeat for multiple. "
                              "Default: all folders.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                         help=f"Messages per IMAP FETCH request (default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--delay", type=float, default=0.0,
                         help="Seconds to sleep between batches (be polite to the server)")
    parser.add_argument("--list-folders", action="store_true",
                         help="Just list available folders and exit")
    args = parser.parse_args()

    if not args.email:
        args.email = input("Email address: ").strip()
    if not args.app_password and not args.list_folders:
        args.app_password = getpass.getpass(
            "Yahoo app password (Account Security -> Generate app password): ")

    log(f"Connecting to {args.server}:{args.port} as {args.email} ...")
    try:
        imap = connect(args.server, args.port, args.email, args.app_password or "")
    except imaplib.IMAP4.error as e:
        log(f"Login failed: {e}")
        log("If you have 2-step verification on, you MUST use an app password, "
            "not your normal Yahoo password.")
        sys.exit(1)

    folders = list_folders(imap)
    if args.list_folders:
        log("Available folders:")
        for f in folders:
            log(f"  {f}")
        imap.logout()
        return

    if args.folder:
        folders = [f for f in folders if f in args.folder or f.strip('"') in args.folder]
        if not folders:
            log("None of the requested --folder names matched. Use --list-folders to see options.")
            sys.exit(1)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    state = BackupState(out_dir / STATE_FILENAME)

    log(f"Backing up {len(folders)} folder(s) to {out_dir.resolve()} as {args.format}")
    log("You can Ctrl+C at any time and re-run this script to resume.\n")

    reconnect_args = (args.email, args.app_password)

    for folder in folders:
        log(f"Folder: {folder}")
        try:
            backup_folder(imap, folder, out_dir, args.format, state,
                           args.batch_size, args.delay, reconnect_args,
                           args.server, args.port)
        except KeyboardInterrupt:
            log("\nInterrupted. Progress has been saved — re-run the script to resume.")
            sys.exit(0)
        except Exception as e:
            log(f"  Error backing up {folder!r}: {e}")
            log("  Continuing with next folder (re-run later to retry this one).")

    try:
        imap.logout()
    except Exception:
        pass

    log("\nAll done.")


if __name__ == "__main__":
    main()
