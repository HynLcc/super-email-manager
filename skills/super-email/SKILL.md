---
name: super-email
description: Full email workflow — sync emails (Himalaya CLI), browse threads, read messages, draft replies, send via SMTP, and archive threads. Triggers on "sync emails", "check inbox", "read email", "reply to email", "send email", "draft an email", or when the user pastes email content for a reply, references a specific email thread, or wants to handle support emails.
---

# Email Manager

Full email management workflow via Himalaya CLI + local thread folders.

## Core Principle

**The Emails/ folder is an archive of processed emails, not a mirror of your inbox.**

- Never bulk-sync all emails to local — that just creates another inbox
- Only emails the user has actively worked on get synced to the local Emails/ folder

## Installation

1. Install Himalaya CLI: `brew install himalaya`
2. Configure your Himalaya account: `~/.config/himalaya/config.toml`
3. Copy and fill in the config file:
   ```bash
   cp config.example.toml config.toml
   ```
4. Edit `config.toml` with your name, email, title, IMAP folders, and signature

## Directory Structure

Emails are stored locally (via `--output-dir`), one folder per thread:

```
Emails/
├── sender-name_subject-slug/
│   ├── thread.md           # Main conversation document
│   ├── raw/                # Raw EML files
│   └── attachments/        # Attachments (created when present)
├── .sync_state.json        # Incremental sync state
```

## Tools

- **Himalaya CLI**: `himalaya` — account name configured in config.toml `[himalaya].account`
- **Sync script**: `scripts/sync_emails.py`
- **Send script**: `scripts/send_email.py` — auto-embeds HTML signature + CC (from config.toml)

## Workflow 1: Browse Inbox (read-only, no local writes)

When the user says "check email", "inbox", "any new emails", use Himalaya CLI directly — **do not sync to local**:

```bash
# Latest 10 inbox emails
himalaya envelope list --page-size 10 --output json

# Latest 10 sent emails (folder name varies by provider, see config.toml [imap].folders)
himalaya envelope list --folder "Sent" --page-size 10 --output json

# Search by sender
himalaya envelope list --page-size 20 --output json from <pattern>

# Search by subject
himalaya envelope list --page-size 20 --output json subject <pattern>

# Filter by date
himalaya envelope list --page-size 20 --output json after 2026-03-18
```

```bash
# Read a single email
himalaya message read <envelope_id>

# Preview mode (don't mark as read)
himalaya message read <envelope_id> --preview

# Read all messages in a thread
himalaya message thread <envelope_id>
```

After browsing, present a brief overview (sender, subject, date) and let the user decide which ones to handle.

## Workflow 2: Sync Selected Emails to Local

**Only sync to the local Emails/ folder in these scenarios:**

1. User explicitly says "handle this one", "reply to this", "sync this thread" → sync that thread on demand
2. User says "sync emails", "sync" → sync (only the range the user specifies)
3. After sending a reply → sync that thread to update local records

```bash
# Sync a specific date range (when the user explicitly asks)
python3 scripts/sync_emails.py --output-dir <emails_dir> --since YYYY-MM-DD

# Sync inbox only
python3 scripts/sync_emails.py --output-dir <emails_dir> --folder INBOX --since YYYY-MM-DD

# Rebuild a specific thread folder (re-parse and regroup)
python3 scripts/sync_emails.py --output-dir <emails_dir> --rebuild <thread-folder-name>

# Preview mode
python3 scripts/sync_emails.py --output-dir <emails_dir> --dry-run --since YYYY-MM-DD
```

IMAP folders are read from config.toml `[imap].folders`.

## Workflow 3: Browse Processed Threads

Read local `Emails/*/thread.md` files. Each thread.md frontmatter contains:
- subject, from, participants, started, last_date, message_count, status

Every thread here has been actively processed by the user and can be reviewed directly.

## Workflow 4: Draft and Send a Reply

### Step 1: Understand Context & Note the Envelope ID

- Read thread.md for the full conversation history
- Or use `himalaya message read <envelope_id>` to read the target email
- If the user pastes email content directly, extract sender name/email, subject, history, and the latest question
- **Record the envelope ID** of the email being replied to — pass it via `--reply-to-id <envelope_id>` so the script auto-extracts In-Reply-To/References for thread linking
- If the user provides a standalone `.eml` file instead of an envelope ID, prefer using the "user's latest email" `Message-ID` as `In-Reply-To`
- In that case, `References` should carry the full chain: at minimum the original thread's `References` plus the user's latest `Message-ID`
- Don't use system notification email `Message-ID` as the reply anchor — clients may display it as detached from the user's latest follow-up

### Step 2: Discuss Reply Strategy

Discuss with the user:
- Summarize the sender's core question
- Propose reply talking points
- **Wait for user confirmation before sending** — don't skip the discussion

This step is important — users frequently add product details, correct the direction, or adjust tone. Don't skip discussion and send directly.

### Step 3: Send (requires explicit user confirmation)

After the user says "send" / confirms, use `send_email.py`. The script auto-handles:
- HTML signature (reads name/title/company from config.toml)
- CC (reads default CC from config.toml, use `--no-cc` to skip)
- Inline styles (line-height 1.8, margin 24px — compatible with all email clients)

```bash
# Reply to an email (auto-extracts In-Reply-To/References, just pass envelope ID)
python3 scripts/send_email.py \
    --to "recipient@example.com" \
    --subject "Re: Original Subject" \
    --body "<p>Email body...</p>" \
    --reply-to-id 9740 \
    --folder "Sent"  # IMAP folder of the original message, required when not INBOX

# New email (no in-reply-to)
python3 scripts/send_email.py \
    --to "recipient@example.com" \
    --subject "Subject" \
    --body "<p>Email body...</p>"

# Additional CC
python3 scripts/send_email.py \
    --to "recipient@example.com" \
    --subject "Subject" \
    --body "<p>Body</p>" \
    --cc "extra@example.com"

# No CC
python3 scripts/send_email.py \
    --to "recipient@example.com" \
    --subject "Subject" \
    --body "<p>Body</p>" \
    --no-cc

# Preview (don't send)
python3 scripts/send_email.py ... --dry-run
```

Notes:
- Thread linking relies on `In-Reply-To` / `References` headers (RFC 2822), not quote blocks
- `--body` takes an HTML fragment (`<p>`-wrapped paragraphs), the script auto-wraps `<html><body>` + signature
- Content-Type is automatically set to `text/html; charset=utf-8`

**Important: Always get explicit user confirmation on content and recipients before sending.**

### Step 4: Sync & Update Local Thread

After sending, re-sync to update the local thread.md:

```bash
python3 scripts/sync_emails.py --output-dir <emails_dir> --since YYYY-MM-DD
```

### Step 5: Update Thread Summary

After syncing, read thread.md and write or update a summary under `## Summary`, covering the full thread context (who initiated, core issues, key replies per round, current status). If a summary already exists, append the latest developments.

**This step must not be skipped** — the summary is the only quick-reference entry point for later review.

## Reply Style Guidelines

Emails represent your organization's external communication — keep the tone professional, friendly, and practical:

- **Answer the core question first**: lead with a direct response
- **Avoid absolutes**: don't use "absolutely", "definitely", "certainly", "best", "perfect"
- **Don't over-promise**: don't casually say "we'll add this feature"
- **Keep it concise**: don't add unnecessary fluff
- **Don't proactively collect requirements**: unless the user asks, don't end with "could you share more about your use case"

## Our Email Addresses

Configured in config.toml `[email].our_addresses`. Used to distinguish received emails from sent emails.
