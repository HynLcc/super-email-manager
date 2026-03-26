#!/usr/bin/env python3
"""
Email Sync: Himalaya → Local Thread Folders

Pulls emails via Himalaya CLI, groups by thread (JWZ-style),
and organizes into per-thread folders with thread.md + raw EML + attachments.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import html
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Config (loaded from config.toml)
# ---------------------------------------------------------------------------

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # Python < 3.11

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.toml"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(
            f"[ERROR] Config file not found: {CONFIG_PATH}\n"
            f"Copy the example and fill in your details:\n"
            f"  cp {SKILL_DIR / 'config.example.toml'} {CONFIG_PATH}",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"[ERROR] Failed to parse {CONFIG_PATH}: {e}", file=sys.stderr)
        sys.exit(1)


_CONFIG = _load_config()
OUR_ADDRESSES = set(addr.lower() for addr in _CONFIG.get("email", {}).get("our_addresses", []))
DEFAULT_FOLDERS = _CONFIG.get("imap", {}).get("folders", ["INBOX", "Sent"])
HIMALAYA_ACCOUNT = _CONFIG.get("himalaya", {}).get("account", "work")
PAGE_SIZE = 100

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(text: str, max_len: int = 50) -> str:
    """Lowercase, strip Re:/Fwd:, replace non-alnum with hyphens, truncate."""
    text = strip_subject_prefixes(text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")[:max_len].rstrip("-")
    return text or "untitled"


def strip_subject_prefixes(subject: str) -> str:
    """Remove Re:/Fwd:/Fw: prefixes for thread matching."""
    return re.sub(r"(?i)^(re|fwd?|fw)\s*:\s*", "", subject).strip()


def extract_message_ids(header_value: str | None) -> list[str]:
    """Extract all <...> message IDs from a References or In-Reply-To header."""
    if not header_value:
        return []
    return re.findall(r"<([^>]+)>", header_value)


def parse_date(date_str: str | None) -> datetime:
    """Parse email date string to datetime. Falls back to epoch on failure."""
    if not date_str:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def addr_display(addr_str: str) -> str:
    """Extract display name or email local part from an address string."""
    name, address = email.utils.parseaddr(addr_str)
    if name:
        return name
    if address:
        return address.split("@")[0]
    return "unknown"


def addr_email(addr_str: str) -> str:
    """Extract email address from a header value."""
    _, address = email.utils.parseaddr(addr_str)
    return address.lower()


def is_our_email(from_addr: str) -> bool:
    """Check if the sender is one of our addresses."""
    return addr_email(from_addr) in OUR_ADDRESSES



def get_text_body(msg: email.message.EmailMessage) -> str:
    """Extract the best text body from an email message."""
    # Prefer text/plain
    body = msg.get_body(preferencelist=("plain",))
    if body:
        content = body.get_content()
        if isinstance(content, str):
            return content.strip()

    # Fallback to text/html → strip tags
    body = msg.get_body(preferencelist=("html",))
    if body:
        content = body.get_content()
        if isinstance(content, str):
            return html_to_text(content).strip()

    return ""


def html_to_text(html_content: str) -> str:
    """Very basic HTML to text conversion."""
    # Remove style/script tags
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
    # Convert <br> and <p> to newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?p[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Remove remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    text = html.unescape(text)
    # Collapse blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def run_himalaya(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a himalaya CLI command.

    --account is injected right after the subcommand (before positional query args)
    to avoid being parsed as part of the query string.
    """
    arg_list = list(args)
    # Insert --account after the subcommand name(s), before any positional args.
    # Subcommands are the leading non-flag tokens (e.g. "envelope list", "message export").
    insert_pos = 0
    for i, a in enumerate(arg_list):
        if a.startswith("-"):
            insert_pos = i
            break
        insert_pos = i + 1
    arg_list[insert_pos:insert_pos] = ["--account", HIMALAYA_ACCOUNT]
    cmd = ["himalaya"] + arg_list
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if check and result.returncode != 0:
        print(f"  [ERROR] himalaya {' '.join(args)}", file=sys.stderr)
        print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Union-Find for threading
# ---------------------------------------------------------------------------


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ---------------------------------------------------------------------------
# Core: fetch envelopes
# ---------------------------------------------------------------------------


def fetch_envelopes(folder: str, since: str | None = None) -> list[dict]:
    """Fetch all envelopes from a folder via Himalaya."""
    envelopes = []
    page = 1
    while True:
        args = [
            "envelope", "list",
            "--folder", folder,
            "--page-size", str(PAGE_SIZE),
            "--page", str(page),
            "--output", "json",
        ]
        if since:
            args.extend(["after", since])

        result = run_himalaya(*args, check=False)
        if result.returncode != 0:
            if page == 1:
                print(f"  [WARN] Failed to list envelopes in {folder}: {result.stderr.strip()}", file=sys.stderr)
            break

        try:
            batch = json.loads(result.stdout)
        except json.JSONDecodeError:
            break

        if not batch:
            break

        envelopes.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1

    return envelopes


# ---------------------------------------------------------------------------
# Core: download raw EML
# ---------------------------------------------------------------------------


def download_eml(envelope_id: str, folder: str, dest_path: Path) -> bool:
    """Download raw EML for a single message."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_himalaya(
        "message", "export", str(envelope_id),
        "--folder", folder,
        "--full",
        "--destination", str(dest_path),
        check=False,
    )
    return result.returncode == 0


def download_attachments(envelope_id: str, folder: str, dest_dir: Path) -> list[str]:
    """Download attachments for a message. Returns list of filenames."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    before = set(dest_dir.iterdir())
    result = run_himalaya(
        "attachment", "download", str(envelope_id),
        "--folder", folder,
        "--downloads-dir", str(dest_dir),
        check=False,
    )
    if result.returncode != 0:
        return []
    after = set(dest_dir.iterdir())
    new_files = after - before
    return [f.name for f in new_files]


# ---------------------------------------------------------------------------
# Core: parse EML
# ---------------------------------------------------------------------------


class ParsedEmail:
    def __init__(self, eml_path: Path):
        with open(eml_path, "rb") as f:
            self.msg = email.message_from_binary_file(f, policy=email.policy.default)
        self.path = eml_path
        self.message_id = self.msg.get("Message-ID", "").strip("<>")
        self.in_reply_to = extract_message_ids(self.msg.get("In-Reply-To"))
        self.references = extract_message_ids(self.msg.get("References"))
        self.subject = self.msg.get("Subject", "(no subject)")
        self.from_addr = self.msg.get("From", "")
        self.to_addr = self.msg.get("To", "")
        self.date = parse_date(self.msg.get("Date"))
        self.body = get_text_body(self.msg)
        self.is_ours = is_our_email(self.from_addr)

        # Collect all ancestor message IDs (References + In-Reply-To, deduplicated)
        seen = set()
        self.ancestors = []
        for mid in self.references + self.in_reply_to:
            if mid not in seen:
                seen.add(mid)
                self.ancestors.append(mid)

    def participants(self) -> set[str]:
        """All email addresses involved."""
        addrs = set()
        for header in ("From", "To", "Cc"):
            val = self.msg.get(header, "")
            for _, addr in email.utils.getaddresses([val]):
                if addr:
                    addrs.add(addr.lower())
        return addrs

    def attachment_names(self) -> list[str]:
        """List attachment filenames from the MIME structure."""
        names = []
        for part in self.msg.walk():
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd.lower():
                fn = part.get_filename()
                if fn:
                    names.append(fn)
        return names


# ---------------------------------------------------------------------------
# Core: threading
# ---------------------------------------------------------------------------


def group_into_threads(emails: list[ParsedEmail]) -> list[list[ParsedEmail]]:
    """Group emails into threads using Union-Find on Message-ID references."""
    uf = UnionFind()

    # Phase 1: link by References/In-Reply-To
    for em in emails:
        if not em.message_id:
            continue
        uf.find(em.message_id)  # ensure it exists
        for ancestor in em.ancestors:
            uf.union(ancestor, em.message_id)

    # Phase 2: fallback by normalized subject for orphans
    subject_to_root = {}  # normalized subject → representative message_id
    for em in emails:
        root = uf.find(em.message_id) if em.message_id else None
        norm_subj = strip_subject_prefixes(em.subject).lower().strip()
        if not norm_subj:
            continue

        if root and root == em.message_id and not em.ancestors:
            # This is an orphan (no references). Try to match by subject.
            if norm_subj in subject_to_root:
                uf.union(subject_to_root[norm_subj], em.message_id)
            else:
                subject_to_root[norm_subj] = em.message_id
        elif root and norm_subj not in subject_to_root:
            subject_to_root[norm_subj] = root

    # Collect threads
    thread_map = defaultdict(list)
    for em in emails:
        root = uf.find(em.message_id) if em.message_id else em.message_id or id(em)
        thread_map[root].append(em)

    # Sort each thread by date
    threads = []
    for msgs in thread_map.values():
        msgs.sort(key=lambda m: m.date)
        threads.append(msgs)

    # Sort threads by earliest message date
    threads.sort(key=lambda t: t[0].date)
    return threads


# ---------------------------------------------------------------------------
# Core: folder naming
# ---------------------------------------------------------------------------


def thread_folder_name(thread: list[ParsedEmail]) -> str:
    """Generate folder name: {sender_slug}_{subject_slug}."""
    # Use the first non-us sender, or first sender
    first_external = None
    for em in thread:
        if not em.is_ours:
            first_external = em
            break
    anchor = first_external or thread[0]

    sender_slug = slugify(addr_display(anchor.from_addr), max_len=30)
    subject_slug = slugify(strip_subject_prefixes(anchor.subject), max_len=50)
    return f"{sender_slug}_{subject_slug}"


def unique_folder_name(base_name: str, existing: set[str]) -> str:
    """Add -2, -3, ... suffix if name conflicts."""
    if base_name not in existing:
        return base_name
    i = 2
    while f"{base_name}-{i}" in existing:
        i += 1
    return f"{base_name}-{i}"


def eml_filename(index: int, em: ParsedEmail) -> str:
    """Generate EML filename: {seq}_{date}_{from|to}_{sender_slug}.eml"""
    date_str = em.date.strftime("%Y-%m-%d")
    direction = "to" if em.is_ours else "from"
    sender = slugify(addr_display(em.from_addr), max_len=30)
    return f"{index:03d}_{date_str}_{direction}_{sender}.eml"


# ---------------------------------------------------------------------------
# Core: generate thread.md
# ---------------------------------------------------------------------------


def generate_thread_md(thread: list[ParsedEmail], attachments_map: dict[str, list[str]]) -> str:
    """Generate the thread.md content for a thread."""
    first = thread[0]
    last = thread[-1]

    # Collect all participants
    all_participants = set()
    for em in thread:
        all_participants |= em.participants()

    # Frontmatter
    participants_yaml = "\n".join(f"  - {p}" for p in sorted(all_participants))
    status = "ongoing"

    lines = [
        "---",
        f"subject: {json.dumps(first.subject)}",
        f"from: {json.dumps(first.from_addr)}",
        f"participants:",
        participants_yaml,
        f'started: "{first.date.strftime("%Y-%m-%d")}"',
        f'last_date: "{last.date.strftime("%Y-%m-%d")}"',
        f"message_count: {len(thread)}",
        f"status: {status}",
        "---",
        "",
        "## Summary",
        "",
        "",
        "",
    ]

    email_num = 0
    reply_num = 0

    for em in thread:
        sender_name = addr_display(em.from_addr)
        date_str = em.date.strftime("%Y-%m-%d %H:%M")
        body = em.body

        if em.is_ours:
            reply_num += 1
            lines.append("---")
            lines.append("")
            lines.append(f"## Reply {reply_num} — {sender_name} ({date_str})")
            lines.append("")
            # Our replies in normal text format
            lines.append(body)
        else:
            email_num += 1
            lines.append("---")
            lines.append("")
            lines.append(f"## Email {email_num} — {sender_name} ({date_str})")
            lines.append("")
            # Incoming emails in quote block format
            quoted = "\n".join(f"> {line}" for line in body.splitlines())
            lines.append(quoted)

        # Attachments
        att_names = attachments_map.get(em.message_id, [])
        if att_names:
            lines.append("")
            for name in att_names:
                lines.append(f"📎 [{name}](attachments/{quote(name)})")

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core: sync state
# ---------------------------------------------------------------------------


class SyncState:
    def __init__(self, state_path: Path):
        self.path = state_path
        self.data = {"synced_message_ids": [], "synced_envelope_ids": [], "thread_map": {}}
        if self.path.exists():
            with open(self.path) as f:
                self.data = json.load(f)

    @property
    def synced_ids(self) -> set[str]:
        return set(self.data.get("synced_message_ids", []))

    @property
    def synced_envelope_ids(self) -> set[str]:
        return set(self.data.get("synced_envelope_ids", []))

    @property
    def thread_map(self) -> dict[str, str]:
        """message_id → folder_name mapping."""
        return self.data.get("thread_map", {})

    def mark_synced(self, message_id: str, folder_name: str, envelope_ids: list[str] | None = None):
        if message_id not in self.synced_ids:
            self.data.setdefault("synced_message_ids", []).append(message_id)
        self.data.setdefault("thread_map", {})[message_id] = folder_name
        if envelope_ids:
            existing = set(self.data.get("synced_envelope_ids", []))
            for eid in envelope_ids:
                if eid not in existing:
                    self.data.setdefault("synced_envelope_ids", []).append(eid)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Core: sync pipeline
# ---------------------------------------------------------------------------


def sync(
    output_dir: Path,
    folders: list[str],
    since: str | None = None,
    dry_run: bool = False,
    rebuild: str | None = None,
):
    """Main sync pipeline."""
    state = SyncState(output_dir / ".sync_state.json")

    # Step 1: Fetch envelopes from all folders
    print("Step 1: Fetching envelopes...")
    all_envelopes = []  # (envelope, folder)
    for folder in folders:
        print(f"  Folder: {folder}")
        envs = fetch_envelopes(folder, since=since)
        print(f"    Found {len(envs)} envelopes")
        for env in envs:
            all_envelopes.append((env, folder))

    if not all_envelopes:
        print("No envelopes found.")
        return

    # Step 2: Filter already synced (unless rebuilding)
    print("Step 2: Filtering...")
    if rebuild:
        # When rebuilding, we re-process messages in the specified thread
        rebuild_ids = set()
        for mid, folder_name in state.thread_map.items():
            if folder_name == rebuild:
                rebuild_ids.add(mid)
        # Remove from synced so they get re-processed
        # But we still need to know which envelope IDs to fetch
        # For rebuild, we process ALL envelopes and filter later
        new_envelopes = all_envelopes
        print(f"  Rebuild mode: reprocessing thread '{rebuild}'")
    else:
        synced_envs = state.synced_envelope_ids
        new_envelopes = []
        for env, folder in all_envelopes:
            env_id = str(env.get("id", ""))
            if env_id and env_id not in synced_envs:
                new_envelopes.append((env, folder))
        print(f"  {len(new_envelopes)} new envelopes to process (skipped {len(all_envelopes) - len(new_envelopes)} already synced)")

    if not new_envelopes:
        print("Nothing new to sync.")
        return

    # Step 3: Download and parse new emails
    print("Step 3: Downloading raw emails...")
    parsed_emails: list[ParsedEmail] = []
    email_folder_map: dict[str, str] = {}  # message_id → imap folder
    email_envid_map: dict[str, tuple[str, str]] = {}  # message_id → (envelope_id, folder)
    msgid_to_envids: dict[str, list[str]] = {}  # message_id → [envelope_ids]
    synced_ids = state.synced_ids

    tmp_dir = output_dir / ".tmp_eml"
    tmp_dir.mkdir(exist_ok=True)

    for i, (env, folder) in enumerate(new_envelopes):
        env_id = str(env.get("id", ""))
        if not env_id:
            continue

        # Download to temp location first
        tmp_eml = tmp_dir / f"{folder}_{env_id}.eml"
        if not tmp_eml.exists():
            ok = download_eml(env_id, folder, tmp_eml)
            if not ok:
                continue

        # Parse
        try:
            parsed = ParsedEmail(tmp_eml)
        except Exception as e:
            print(f"  [WARN] Failed to parse {tmp_eml.name}: {e}", file=sys.stderr)
            continue

        # Deduplicate by message-id (same message in INBOX and Sent)
        if parsed.message_id and parsed.message_id in email_folder_map:
            msgid_to_envids.setdefault(parsed.message_id, []).append(env_id)
            continue
        if not rebuild and parsed.message_id and parsed.message_id in synced_ids:
            continue

        parsed_emails.append(parsed)
        if parsed.message_id:
            email_folder_map[parsed.message_id] = folder
            email_envid_map[parsed.message_id] = (env_id, folder)
            msgid_to_envids.setdefault(parsed.message_id, []).append(env_id)

        if (i + 1) % 20 == 0:
            print(f"  Downloaded {i + 1}/{len(new_envelopes)}...")

    print(f"  {len(parsed_emails)} new unique emails parsed")

    if not parsed_emails:
        print("No new emails to process.")
        # Clean up tmp
        _cleanup_tmp(tmp_dir)
        return

    # Also load already-synced emails for proper threading context
    if not rebuild:
        existing_parsed = _load_existing_emls(output_dir)
        all_for_threading = existing_parsed + parsed_emails
    else:
        all_for_threading = parsed_emails

    # Step 4: Thread grouping
    print("Step 4: Grouping into threads...")
    threads = group_into_threads(all_for_threading)
    print(f"  {len(threads)} threads found")

    # Step 5: Generate/update folders
    print("Step 5: Generating thread folders...")
    new_message_ids = {em.message_id for em in parsed_emails}
    existing_folders = {
        p.name for p in output_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    }

    for thread in threads:
        # Skip threads with no new messages (unless rebuild)
        has_new = any(em.message_id in new_message_ids for em in thread)
        if not has_new and not rebuild:
            continue

        # Determine folder name
        existing_name = None
        for em in thread:
            if em.message_id in state.thread_map:
                existing_name = state.thread_map[em.message_id]
                break

        if existing_name:
            folder_name = existing_name
        else:
            base_name = thread_folder_name(thread)
            folder_name = unique_folder_name(base_name, existing_folders)
            existing_folders.add(folder_name)

        thread_dir = output_dir / folder_name
        raw_dir = thread_dir / "raw"
        att_dir = thread_dir / "attachments"

        if dry_run:
            print(f"  [DRY RUN] Would create/update: {folder_name}/")
            for idx, em in enumerate(thread, 1):
                direction = "→" if em.is_ours else "←"
                print(f"    {direction} {eml_filename(idx, em)}")
            continue

        # Create directories
        raw_dir.mkdir(parents=True, exist_ok=True)

        # Move EML files and download attachments
        attachments_map: dict[str, list[str]] = {}
        for idx, em in enumerate(thread, 1):
            fname = eml_filename(idx, em)
            dest_eml = raw_dir / fname

            if em.message_id in new_message_ids:
                # Move from tmp to final location
                if em.path.exists() and not dest_eml.exists():
                    # Rename existing files with old names if re-indexing
                    em.path.rename(dest_eml)
                elif em.path.exists() and dest_eml.exists():
                    em.path.unlink()  # duplicate, remove tmp copy
            # else: already in place from previous sync

            # Download attachments for new messages
            att_names = em.attachment_names()
            if att_names and em.message_id in email_envid_map and em.message_id in new_message_ids:
                env_id, imap_folder = email_envid_map[em.message_id]
                downloaded = download_attachments(env_id, imap_folder, att_dir)
                attachments_map[em.message_id] = downloaded or att_names
            elif att_names:
                attachments_map[em.message_id] = att_names

        # Generate thread.md
        thread_md = generate_thread_md(thread, attachments_map)
        (thread_dir / "thread.md").write_text(thread_md, encoding="utf-8")

        # Remove empty attachments dir
        if att_dir.exists() and not any(att_dir.iterdir()):
            att_dir.rmdir()

        # Update sync state
        for em in thread:
            if em.message_id:
                env_ids = msgid_to_envids.get(em.message_id, [])
                state.mark_synced(em.message_id, folder_name, envelope_ids=env_ids)

        print(f"  {'Updated' if existing_name else 'Created'}: {folder_name}/ ({len(thread)} messages)")

    # Step 6: Save state
    if not dry_run:
        state.save()
        print("Step 6: Sync state saved.")

    # Clean up
    _cleanup_tmp(tmp_dir)
    print("Done!")


def _load_existing_emls(output_dir: Path) -> list[ParsedEmail]:
    """Load already-synced EML files for threading context."""
    parsed = []
    for thread_dir in output_dir.iterdir():
        if not thread_dir.is_dir() or thread_dir.name.startswith("."):
            continue
        raw_dir = thread_dir / "raw"
        if not raw_dir.exists():
            continue
        for eml_file in sorted(raw_dir.glob("*.eml")):
            try:
                parsed.append(ParsedEmail(eml_file))
            except Exception:
                continue
    return parsed


def _cleanup_tmp(tmp_dir: Path):
    """Remove temp directory if empty."""
    if tmp_dir.exists():
        for f in tmp_dir.iterdir():
            f.unlink()
        tmp_dir.rmdir()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Sync emails from IMAP via Himalaya into thread folders."
    )
    parser.add_argument(
        "--since",
        help="Only sync emails after this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--folder",
        help="Only sync from this IMAP folder (default: INBOX + Sent)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be synced without writing files",
    )
    parser.add_argument(
        "--rebuild",
        metavar="THREAD_FOLDER",
        help="Force rebuild a specific thread folder",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Output directory (default: current dir, i.e. Emails/)",
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    folders = [args.folder] if args.folder else DEFAULT_FOLDERS

    # Validate himalaya is available
    try:
        subprocess.run(["himalaya", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("Error: himalaya CLI not found. Install with: brew install himalaya", file=sys.stderr)
        sys.exit(1)

    sync(
        output_dir=output_dir,
        folders=folders,
        since=args.since,
        dry_run=args.dry_run,
        rebuild=args.rebuild,
    )


if __name__ == "__main__":
    main()
