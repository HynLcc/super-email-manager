#!/usr/bin/env python3
"""
Send Email via Himalaya CLI

Constructs HTML email with signature and CC to support, then sends via himalaya.
Threading relies on In-Reply-To / References headers (RFC 2822).

Usage:
    python3 send_email.py \
        --to "recipient@example.com" \
        --subject "Re: Subject" \
        --body "<p>Hello</p>" \
        [--reply-to-id 9740] \
        [--folder "First one"] \
        [--in-reply-to "<message-id>"] \
        [--references "<message-id>"] \
        [--cc "extra@example.com"] \
        [--no-cc]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # Python < 3.11

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.toml"


def load_config() -> dict:
    """Load config from config.toml. Exit with helpful message if missing."""
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


CONFIG = load_config()
USER_NAME = CONFIG["user"]["name"]
USER_TITLE = CONFIG["user"].get("title", "")
USER_EMAIL = CONFIG["user"]["email"]
USER_COMPANY = CONFIG["user"].get("company", "")
DEFAULT_CC = CONFIG["user"].get("cc", "")
FROM_ADDRESS = f"{USER_NAME} <{USER_EMAIL}>"
HIMALAYA_ACCOUNT = CONFIG.get("himalaya", {}).get("account", "work")
SIGNATURE_HTML = CONFIG.get("signature", {}).get("html", "")
CLOSING = CONFIG.get("signature", {}).get("closing", "Best regards,")


def _sign_off_subtitle() -> str:
    """Build the subtitle line under the sender name (title | company)."""
    parts = [p for p in (USER_TITLE, USER_COMPANY) if p]
    if not parts:
        return ""
    return f'<br/><span style="font-size:12px;color:#666;">{" | ".join(parts)}</span>'


def _sanitize_header(value: str) -> str:
    """Strip CR/LF to prevent header injection."""
    return value.replace("\r", "").replace("\n", "")


def _ensure_angle_brackets(value: str) -> str:
    """Ensure each Message-ID in the value is wrapped in <angle brackets>."""
    result = []
    for part in value.split():
        if not part.startswith("<"):
            part = "<" + part
        if not part.endswith(">"):
            part = part + ">"
        result.append(part)
    return " ".join(result)


def _extract_headers_from_eml(envelope_id: str, folder: str | None = None) -> dict:
    """Export the original message and extract Message-ID and References headers."""
    import email as email_mod
    import email.policy as email_policy

    with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    cmd = ["himalaya", "message", "export", envelope_id, "--account", HIMALAYA_ACCOUNT,
         "--full", "--destination", str(tmp_path)]
    if folder:
        cmd.extend(["--folder", folder])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(f"[WARN] Timed out exporting message {envelope_id}", file=sys.stderr)
        tmp_path.unlink(missing_ok=True)
        return {}
    if result.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        return {}

    try:
        with open(tmp_path, "rb") as f:
            msg = email_mod.message_from_binary_file(f, policy=email_policy.default)
        return {
            "message_id": (msg.get("Message-ID") or "").strip(),
            "references": (msg.get("References") or "").strip(),
        }
    finally:
        tmp_path.unlink(missing_ok=True)


def resolve_reply_headers(envelope_id: str, folder: str | None = None) -> dict:
    """Resolve In-Reply-To and References for replying to an envelope.

    Uses 'himalaya template reply' for In-Reply-To, then builds the full
    References chain from the original message's headers (RFC 2822).
    """
    cmd = ["himalaya", "template", "reply", envelope_id, "--account", HIMALAYA_ACCOUNT]
    if folder:
        cmd.extend(["--folder", folder])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(f"[WARN] Timed out getting reply template for {envelope_id}", file=sys.stderr)
        return {}
    if result.returncode != 0:
        print(f"[WARN] Failed to get reply template for {envelope_id}: {result.stderr}", file=sys.stderr)
        return {}

    headers = {}
    for line in result.stdout.splitlines():
        if line == "":
            break
        if line.startswith("In-Reply-To: "):
            headers["in_reply_to"] = line[len("In-Reply-To: "):]
        elif line.startswith("References: "):
            headers["references"] = line[len("References: "):]

    # Build References chain from original message if himalaya didn't provide it
    if not headers.get("references") and headers.get("in_reply_to"):
        orig = _extract_headers_from_eml(envelope_id, folder=folder)
        if orig:
            parts = []
            if orig.get("references"):
                parts.append(orig["references"])
            if orig.get("message_id"):
                parts.append(orig["message_id"])
            if parts:
                headers["references"] = " ".join(parts)

    return headers


def build_eml(
    to: str,
    subject: str,
    body_html: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    extra_cc: str | None = None,
    no_cc: bool = False,
) -> str:
    """Build a raw EML string with HTML body + signature."""

    signature_html = SIGNATURE_HTML

    # Build CC list
    cc_list = []
    if not no_cc and DEFAULT_CC:
        cc_list.append(DEFAULT_CC)
    if extra_cc:
        for addr in extra_cc.split(","):
            addr = addr.strip()
            if addr and addr.lower() != DEFAULT_CC.lower():
                cc_list.append(addr)

    # Headers (sanitize to prevent header injection)
    headers = [
        f"From: {_sanitize_header(FROM_ADDRESS)}",
        f"To: {_sanitize_header(to)}",
    ]
    if cc_list:
        headers.append(f"Cc: {', '.join(_sanitize_header(a) for a in cc_list)}")
    headers.append(f"Subject: {_sanitize_header(subject)}")
    if in_reply_to:
        headers.append(f"In-Reply-To: {_ensure_angle_brackets(in_reply_to)}")
    if references:
        headers.append(f"References: {_ensure_angle_brackets(references)}")
    headers.append("Content-Type: text/html; charset=utf-8")

    # Inject inline styles into body HTML tags
    styled_body = re.sub(r'<p>', '<p style="margin:0 0 24px 0;line-height:1.8;">', body_html)
    styled_body = re.sub(r'<li>', '<li style="margin:0 0 6px 0;line-height:1.8;">', styled_body)
    styled_body = re.sub(r'<ul>', '<ul style="margin:0 0 24px 0;padding-left:24px;">', styled_body)

    # HTML body with signature
    html = f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#333333;">
{styled_body}

<p style="margin:0 0 24px 0;line-height:1.8;">{CLOSING}</p>
<p style="margin:0 0 24px 0;line-height:1.8;"><strong>{USER_NAME}</strong>{_sign_off_subtitle()}</p>
{signature_html}
</body></html>"""

    return "\n".join(headers) + "\n\n" + html


def send(eml_content: str) -> bool:
    """Send via himalaya."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".eml", encoding="utf-8", delete=False
    ) as f:
        f.write(eml_content)
        tmp_path = Path(f.name)

    try:
        try:
            result = subprocess.run(
                ["himalaya", "message", "send", "--account", HIMALAYA_ACCOUNT],
                input=tmp_path.read_text(encoding="utf-8"),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            print("[ERROR] himalaya send timed out", file=sys.stderr)
            return False
        if result.returncode != 0:
            print(f"[ERROR] himalaya send failed: {result.stderr}", file=sys.stderr)
            return False
        print("Message successfully sent!")
        return True
    finally:
        tmp_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Send email via Himalaya with signature + CC support")
    parser.add_argument("--to", required=True, help="Recipient email")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", required=True, help="HTML body content (without <html> wrapper)")
    parser.add_argument("--reply-to-id", help="Himalaya envelope ID — auto-extracts In-Reply-To/References")
    parser.add_argument("--folder", help="IMAP folder where the original message lives (default: INBOX)")
    parser.add_argument("--in-reply-to", help="In-Reply-To message ID (manual override)")
    parser.add_argument("--references", help="References message ID(s) (manual override)")
    parser.add_argument("--cc", help="Additional CC addresses (comma-separated)")
    parser.add_argument("--no-cc", action="store_true", help="Skip default CC to support")
    parser.add_argument("--dry-run", action="store_true", help="Print EML without sending")

    args = parser.parse_args()

    # Check himalaya is available
    try:
        subprocess.run(["himalaya", "--version"], capture_output=True, check=True, timeout=10)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("[ERROR] himalaya CLI not found. Install: brew install himalaya", file=sys.stderr)
        sys.exit(1)

    in_reply_to = args.in_reply_to
    references = args.references

    # Auto-resolve threading headers from envelope ID
    if args.reply_to_id and not in_reply_to:
        reply_headers = resolve_reply_headers(args.reply_to_id, folder=args.folder)
        in_reply_to = reply_headers.get("in_reply_to")
        references = references or reply_headers.get("references")
        if not in_reply_to:
            print(f"[WARN] Could not resolve threading headers for envelope {args.reply_to_id}. "
                  f"Email will be sent without thread context.", file=sys.stderr)

    eml = build_eml(
        to=args.to,
        subject=args.subject,
        body_html=args.body,
        in_reply_to=in_reply_to,
        references=references,
        extra_cc=args.cc,
        no_cc=args.no_cc,
    )

    if args.dry_run:
        print(eml)
        return

    if not send(eml):
        sys.exit(1)


if __name__ == "__main__":
    main()
