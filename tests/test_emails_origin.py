"""Le frontmatter d'une transcription de courriel porte son origine (mbox)."""
from datetime import datetime

from connaissance.commands import emails


def test_format_email_ecrit_source_path_du_mbox():
    msg = {
        "from": "a@exemple.org", "from_display": "A", "to": "b@exemple.org",
        "subject": "Sujet", "message_id": "<m1@exemple.org>", "folder": "INBOX",
        "mbox": "Archives/Courriels/Fastmail/Guillaume/INBOX.mbox",
        "date": datetime(2026, 3, 30, 20, 11, 25), "body": "Corps.",
        "attachments": [], "headers": {},
    }
    out = emails.format_email(msg)
    assert "source_path: Archives/Courriels/Fastmail/Guillaume/INBOX.mbox" in out
    assert "message-id: <m1@exemple.org>" in out
