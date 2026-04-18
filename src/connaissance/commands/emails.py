"""Module commands/emails : extraction mbox, scoring multi-signaux, threading, calibrage.

Expose (API publique utilisée par le dispatcher CLI et les outils MCP) :
- `stats(account=None, folder=None, since=None, until=None) -> EmailsStats`
- `extract(account=None, folder=None, since=None, until=None, dry_run=False, no_images=False) -> EmailsExtract`
- `threads(account=None, folder=None, since=None, until=None) -> EmailsThreads` (NEW)
- `calibrate(sample=200, since=None, until=None) -> EmailsCalibrate` (NEW `proposed_mutations`)
- `senders(sample=500, since=None, until=None) -> dict`
- `cleanup_obsolete(dry_run=True) -> EmailsCleanupObsolete`

Délègue à `emails_cleanup` pour le re-scoring rétroactif et l'archivage réversible.
"""
from __future__ import annotations
import sys

import mailbox
import email
import email.message
import email.utils
import email.header
import hashlib
import json
import re
import uuid
from pathlib import Path
from datetime import datetime, timezone
from html.parser import HTMLParser

from connaissance.core.paths import BASE_PATH, require_paths
from connaissance.core.tracking import TrackingDB
from connaissance.core.filtres import Filtres

# --- Configuration ---

ARCHIVES_ROOT = BASE_PATH / "Archives" / "Courriels"
EXTRACTION_DIR = BASE_PATH / "Connaissance" / "Transcriptions" / "Courriels"

# Extensions de fichiers pour les PJ
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".heic", ".webp", ".tiff", ".gif", ".bmp"}
DOCUMENT_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".txt"}


# --- Utilitaires HTML ---

class HTMLStripper(HTMLParser):
    """Supprime les balises HTML pour extraire le texte brut."""

    def __init__(self):
        super().__init__()
        self.result = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("br", "p", "div", "li", "tr"):
            self.result.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.result.append(data)

    def get_text(self):
        return "".join(self.result).strip()


def strip_html(html_content: str) -> str:
    """Convertit du HTML en texte brut."""
    stripper = HTMLStripper()
    try:
        stripper.feed(html_content)
        return stripper.get_text()
    except Exception:
        return html_content


# --- Utilitaires email ---

def decode_header(raw: str | None) -> str:
    """Décode un en-tête MIME (RFC 2047)."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for content, charset in parts:
        if isinstance(content, bytes):
            # Fallback gracieux : certains courriels utilisent des placeholders
            # MIME non reconnus par Python (ex: "unknown-8bit" de RFC 2045 §6.7)
            # qui lèvent LookupError. On retombe sur utf-8 puis latin-1.
            try:
                decoded.append(content.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                try:
                    decoded.append(content.decode("utf-8", errors="replace"))
                except UnicodeDecodeError:
                    decoded.append(content.decode("latin-1", errors="replace"))
        else:
            decoded.append(content)
    return " ".join(decoded).strip()


def parse_date(raw: str | None) -> datetime | None:
    """Parse une date d'en-tête de courriel."""
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue
    return None


def _extract_date_from_message(msg: email.message.Message) -> datetime | None:
    """Extraire la date d'un message avec fallbacks multiples."""
    date = parse_date(msg.get("Date"))
    if date:
        return date
    date = parse_date(msg.get("Resent-Date"))
    if date:
        return date
    received = msg.get_all("Received") or []
    for rcv in received:
        if ";" in rcv:
            date = parse_date(rcv.rsplit(";", 1)[1].strip())
            if date:
                return date
    return None


def get_from_header(msg: email.message.Message) -> str:
    """Extrait le header From, en gérant l'échappement MBOXRD (>From)."""
    from_val = msg.get("From", "")
    if not from_val:
        from_val = msg.get(">From", "")
    return from_val


def safe_decode(payload: bytes, charset: str | None) -> str:
    """Décode un payload avec fallback sur utf-8 puis latin-1."""
    for enc in [charset, "utf-8", "latin-1"]:
        if not enc:
            continue
        try:
            return payload.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def strip_quoted_replies(body: str) -> str:
    """Retire les citations de messages précédents dans un corps de courriel."""
    quote_start = re.search(
        r'^(Le\s+.+\s+a\s+[ée]crit\s*:\s*$'
        r'|On\s+.+\s+wrote\s*:\s*$'
        r'|_{3,}\s*$'
        r'|-{3,}\s*Original\s+[Mm]essage'
        r'|-{3,}\s*Message\s+d\'origine'
        r'|De\s*:.*\n\s*Envoy[ée]\s*:'
        r'|From\s*:.*\n\s*Sent\s*:'
        r')',
        body,
        re.MULTILINE | re.IGNORECASE,
    )
    if quote_start:
        body = body[:quote_start.start()].rstrip()

    lines = body.split("\n")
    cleaned = []
    trailing_quotes = False
    for line in lines:
        if re.match(r'^>\s?', line):
            trailing_quotes = True
            continue
        if trailing_quotes and line.strip() == "":
            continue
        trailing_quotes = False
        cleaned.append(line)

    return "\n".join(cleaned).rstrip()


def extract_body(msg: email.message.Message) -> str:
    """Extrait le corps texte d'un message (préfère text/plain, sinon text/html)."""
    if msg.is_multipart():
        text_part = None
        html_part = None
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            if content_type == "text/plain" and text_part is None:
                text_part = part
            elif content_type == "text/html" and html_part is None:
                html_part = part

        if text_part:
            payload = bytes(text_part.get_payload(decode=True) or b"")
            return safe_decode(payload, text_part.get_content_charset()) if payload else ""
        elif html_part:
            payload = bytes(html_part.get_payload(decode=True) or b"")
            html = safe_decode(payload, html_part.get_content_charset()) if payload else ""
            return strip_html(html)
        return ""
    else:
        content_type = msg.get_content_type()
        payload = bytes(msg.get_payload(decode=True) or b"")
        if not payload:
            return ""
        text = safe_decode(payload, msg.get_content_charset())
        if content_type == "text/html":
            return strip_html(text)
        return text


def hash_message_id(message_id: str) -> str:
    """Génère un hash court pour un Message-ID."""
    return hashlib.sha256(message_id.encode()).hexdigest()[:12]


# --- Déduplication PJ ---

def extract_attachments(msg: email.message.Message, message_hash: str,
                        filtres: 'Filtres | None' = None,
                        include_images: bool = False) -> list[dict]:
    """Extrait toutes les pièces jointes significatives.

    Tout va dans Attachments/ adjacent au .md. Pas de dédup, pas de
    promotion — c'est le skill optimiser qui nettoie après.
    """
    attachments = []
    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        # Seulement les vrais attachements — pas les images inline
        # (les images inline sont du formatage HTML : logos, bannières, etc.)
        if "attachment" not in disposition:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = decode_header(filename)
        content_type = part.get_content_type()
        payload = part.get_payload(decode=True) or b""
        size = len(payload)

        ext = Path(filename).suffix.lower()
        is_pdf = ext in PDF_EXTENSIONS
        is_image = ext in IMAGE_EXTENSIONS
        is_doc = ext in DOCUMENT_EXTENSIONS

        if not is_pdf and not is_doc and not (include_images and is_image):
            continue

        # Ignorer les petites images (signatures, avatars, logos)
        min_size = filtres.min_attachment_size() if filtres else 50_000
        if content_type.startswith("image/") and size < min_size:
            continue

        uuid_name = f"{str(uuid.uuid4()).upper()}{ext}"

        attachments.append({
            "filename": filename,
            "safe_filename": uuid_name,
            "content_type": content_type,
            "size": size,
            "payload": payload,
        })
    return attachments


# --- Parsing mbox avec index imap-backup ---

def _load_imap_index(mbox_path: Path) -> list[dict] | None:
    """Charger l'index .imap d'imap-backup (offsets et longueurs)."""
    imap_path = mbox_path.with_suffix(".imap")
    if not imap_path.exists():
        return None
    try:
        data = json.loads(imap_path.read_text())
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            # Format v3 : { "version": 3, "messages": [...] }
            if "messages" in data:
                return data["messages"]
            result = []
            for uid_str, entry in sorted(data.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
                if not uid_str.isdigit():
                    continue
                entry["uid"] = int(uid_str)
                result.append(entry)
            return result
    except (json.JSONDecodeError, ValueError):
        return None


def _read_date_from_handle(f, offset: int) -> datetime | None:
    """Lire la date d'un message à un offset sans parser le message complet.

    Essaie dans l'ordre :
    1. Header `Date:` dans les premiers 16 Ko
    2. Date du `From ` envelope mbox (première ligne)
    3. Premier header `Received:` avec date
    """
    f.seek(offset)
    header_bytes = f.read(16384)
    try:
        header_text = header_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None

    # 1. Header Date: standard
    match = re.search(r'^Date:\s*(.+)$', header_text, re.MULTILINE | re.IGNORECASE)
    if match:
        d = parse_date(match.group(1).strip())
        if d:
            return d

    # 2. From envelope mbox (première ligne: "From sender@x Mon Apr 6 08:53:21 2026")
    match = re.match(r'^From \S+\s+(.+)$', header_text)
    if match:
        d = parse_date(match.group(1).strip())
        if d:
            return d

    # 3. Received: header (date après le dernier ';')
    match = re.search(r'^Received:.*?;\s*(.+)$', header_text, re.MULTILINE | re.IGNORECASE)
    if match:
        d = parse_date(match.group(1).strip())
        if d:
            return d

    return None


def _bisect_imap_index(imap_index, mbox_path, target_date, find_first=True):
    """Recherche binaire dans l'index imap pour trouver la position d'une date.

    Les messages dans imap-backup sont triés par UID (chronologique).
    On fait une recherche binaire en lisant la date header à chaque position.

    find_first=True : trouve le premier message >= target_date (pour since)
    find_first=False : trouve le dernier message < target_date (pour until)
    """
    n = len(imap_index)
    if n == 0:
        return 0

    with open(mbox_path, "rb") as f:
        lo, hi = 0, n - 1

        # Vérifier les bornes d'abord
        first_date = _read_date_from_handle(f, imap_index[0].get("offset", 0))
        last_date = _read_date_from_handle(f, imap_index[-1].get("offset", 0))

        if first_date and target_date <= first_date:
            return 0 if find_first else 0
        if last_date and target_date > last_date:
            return n if find_first else n

        while lo < hi:
            mid = (lo + hi) // 2
            mid_date = _read_date_from_handle(f, imap_index[mid].get("offset", 0))

            if mid_date is None:
                # Pas de date — avancer linéairement
                lo = mid + 1
                continue

            if find_first:
                if mid_date < target_date:
                    lo = mid + 1
                else:
                    hi = mid
            else:
                if mid_date < target_date:
                    lo = mid + 1
                else:
                    hi = mid

    return lo


def _parse_message(msg: email.message.Message, mbox_path: Path,
                   envelope_from: str = "",
                   filtres: 'Filtres | None' = None,
                   include_images: bool = False) -> dict | None:
    """Parser un message email complet en dict structuré."""
    # Strip : les headers RFC 5322 peuvent être pliés sur plusieurs lignes
    # (Message-ID:\n <id@domain>). Sans strip, YAML parse le frontmatter en
    # multi-ligne et stocke " <id>" avec espace initial, ce qui fait que
    # has_message_id() ne matche plus à la ré-extraction → doublons.
    message_id = msg.get("Message-ID", "").strip()
    in_reply_to = msg.get("In-Reply-To", "").strip()
    references_raw = msg.get("References", "")
    references = re.findall(r"<[^>]+>", references_raw)

    from_raw = decode_header(get_from_header(msg))
    _, from_addr = email.utils.parseaddr(from_raw)
    from_addr = from_addr or from_raw

    # Fallback : envelope mbox ("From sender@domain ...") ou Return-Path
    if not from_addr or from_addr == from_raw == "":
        # Return-Path
        rp = msg.get("Return-Path", "")
        if rp:
            _, from_addr = email.utils.parseaddr(rp)
            from_raw = rp
        # Envelope mbox
        if not from_addr and envelope_from:
            from_addr = envelope_from
            from_raw = envelope_from

    to_raw = decode_header(msg.get("To", ""))
    cc_raw = decode_header(msg.get("Cc", ""))
    subject = decode_header(msg.get("Subject", ""))
    date = _extract_date_from_message(msg)

    body = extract_body(msg)

    # Détecter HTML-only (signal scoring)
    is_html_only = False
    if msg.is_multipart():
        has_plain = any(
            p.get_content_type() == "text/plain"
            and "attachment" not in str(p.get("Content-Disposition", ""))
            for p in msg.walk()
        )
        has_html = any(p.get_content_type() == "text/html" for p in msg.walk())
        is_html_only = not has_plain and has_html
    else:
        is_html_only = (msg.get_content_type() == "text/html")

    list_unsub = msg.get("List-Unsubscribe", "")

    msg_hash = hash_message_id(message_id) if message_id else hashlib.sha256(
        (subject + str(date)).encode()).hexdigest()[:12]
    attachments = extract_attachments(msg, msg_hash, filtres=filtres,
                                      include_images=include_images)

    return {
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "from": from_addr,
        "from_display": from_raw,
        "to": to_raw,
        "cc": cc_raw,
        "subject": subject,
        "date": date,
        "body": body,
        "is_html_only": is_html_only,
        "attachments": attachments,
        "folder": mbox_path.stem,
        "msg_hash": msg_hash,
        "headers": {"list-unsubscribe": list_unsub},
    }


def extract_messages_from_mbox(mbox_path: Path,
                               filtres: 'Filtres | None' = None,
                               include_images: bool = False,
                               since=None, until=None) -> list[dict]:
    """Extrait les messages d'un fichier .mbox, filtrant par date."""
    messages = []
    skipped_date = 0

    # Stratégie 1 : utiliser l'index imap-backup (accès direct par offset)
    imap_index = _load_imap_index(mbox_path)
    if imap_index and (since or until):
        # Recherche binaire pour trouver la plage de messages
        start_idx = _bisect_imap_index(imap_index, mbox_path, since, find_first=True) if since else 0
        end_idx = _bisect_imap_index(imap_index, mbox_path, until, find_first=True) if until else len(imap_index)

        candidate_entries = imap_index[start_idx:end_idx]
        skipped_date = len(imap_index) - len(candidate_entries)

        if skipped_date:
            print(f"    ({skipped_date} messages hors plage ignorés via bisect, {len(candidate_entries)} à parser)", file=sys.stderr)


        # Parser seulement les messages dans la plage
        with open(mbox_path, "rb") as f:
            for entry in candidate_entries:
                try:
                    f.seek(entry["offset"])
                    raw = f.read(entry["length"])
                    # Extraire l'adresse de l'envelope mbox et skipper la ligne
                    envelope_from = ""
                    if raw.startswith(b"From "):
                        first_line = raw.split(b"\n", 1)[0].decode("utf-8", errors="replace")
                        parts = first_line.split(" ", 2)
                        if len(parts) >= 2:
                            envelope_from = parts[1]
                        raw = raw.split(b"\n", 1)[1]
                    msg = email.message_from_bytes(raw)
                except Exception:
                    continue
                try:
                    parsed = _parse_message(msg, mbox_path, envelope_from=envelope_from,
                                            filtres=filtres, include_images=include_images)
                    if parsed:
                        messages.append(parsed)
                except Exception as e:
                    print(f"  Erreur message uid={entry.get('uid')}: {e}", file=sys.stderr)

                    continue

        return messages

    # Stratégie 2 : fallback séquentiel
    try:
        mbox = mailbox.mbox(str(mbox_path))
    except Exception as e:
        print(f"  Erreur ouverture {mbox_path}: {e}", file=sys.stderr)

        return messages

    for msg in mbox:
        try:
            date = _extract_date_from_message(msg)
            if since or until:
                if not date:
                    skipped_date += 1
                    continue
                if since and date < since:
                    skipped_date += 1
                    continue
                if until and date >= until:
                    skipped_date += 1
                    continue

            # mailbox.mbox gère l'envelope — msg.get_from() retourne l'adresse
            envelope_from = msg.get_from() or ""
            if " " in envelope_from:
                envelope_from = envelope_from.split(" ", 1)[0]
            parsed = _parse_message(msg, mbox_path, envelope_from=envelope_from,
                                    filtres=filtres, include_images=include_images)
            if parsed:
                messages.append(parsed)
        except Exception as e:
            print(f"  Erreur message: {e}", file=sys.stderr)

            continue

    mbox.close()
    if skipped_date:
        print(f"    ({skipped_date} messages hors plage de dates ignorés)", file=sys.stderr)

    return messages


# --- Détection incrémentale ---

def scan_existing_message_ids(output_dir: Path) -> set[str]:
    """Scanner les message-id déjà extraits dans le répertoire de sortie."""
    existing = set()
    if not output_dir.exists():
        return existing
    for md_file in output_dir.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            if not content.startswith("---"):
                continue
            end = content.find("---", 3)
            if end < 0:
                continue
            fm = content[3:end]
            match = re.search(r'^message-id:\s*"?(<[^>]+>)"?', fm, re.MULTILINE)
            if match:
                existing.add(match.group(1))
        except Exception:
            continue
    return existing


# --- Formatage de sortie ---

def format_email(msg: dict) -> str:
    """Formatte un courriel en Markdown avec frontmatter."""
    import yaml
    date_str = msg["date"].strftime("%Y-%m-%dT%H:%M:%S") if msg["date"] else "0000-00-00T00:00:00"

    # Utiliser yaml.safe_dump pour échapper correctement tous les caractères
    # spéciaux (<>, ", \t, \n...) dans les champs from/to avec « "Nom" <email> »,
    # qui rendraient un frontmatter composé en f-string non-parsable.
    def _clean(v):
        if isinstance(v, str):
            return v.replace("\t", " ").strip()
        return v

    frontmatter = {
        "type": "courriel",
        "created": date_str,
        "modified": date_str,
        "from": _clean(msg["from"]),
        "to": _clean(msg["to"]),
        "subject": _clean(msg["subject"]),
        "message-id": _clean(msg["message_id"]),
        "folder": _clean(msg["folder"]),
    }
    if msg.get("cc"):
        frontmatter["cc"] = _clean(msg["cc"])
    if msg.get("in_reply_to"):
        frontmatter["in-reply-to"] = _clean(msg["in_reply_to"])
    if msg.get("references"):
        frontmatter["references"] = [_clean(r) for r in msg["references"]]

    fm_yaml = yaml.safe_dump(frontmatter, allow_unicode=True,
                             default_flow_style=False, sort_keys=False).rstrip()
    lines = ["---", fm_yaml, "---", ""]

    # En-tête lisible
    lines.append(f'# {msg["subject"]}')
    lines.append("")
    lines.append(f'**De :** {msg["from_display"]}')
    lines.append(f'**À :** {msg["to"]}')
    if msg.get("cc"):
        lines.append(f'**Cc :** {msg["cc"]}')
    lines.append(f'**Date :** {date_str}')
    lines.append("")

    # Corps
    body = strip_quoted_replies(msg["body"])
    if body:
        lines.append(body)
    lines.append("")

    # Pièces jointes (toutes dans Attachments/)
    if msg["attachments"]:
        lines.append("## Pièces jointes")
        lines.append("")
        for att in msg["attachments"]:
            path = att.get("path", "")
            if att["content_type"].startswith("image/"):
                lines.append(f'![{att["filename"]}]({path})')
            else:
                lines.append(f'- [{att["filename"]}]({path})')
        lines.append("")

    return "\n".join(lines)


# --- Sauvegarde des PJ ---

def save_attachments(attachments: list[dict], attachments_dir: Path) -> None:
    """Sauvegarde toutes les PJ dans Attachments/ adjacent au .md."""
    if not attachments:
        return
    attachments_dir.mkdir(parents=True, exist_ok=True)
    for att in attachments:
        if "payload" not in att:
            continue
        att_path = attachments_dir / att["safe_filename"]
        if not att_path.exists():
            att_path.write_bytes(att["payload"])
        att["path"] = f"Attachments/{att['safe_filename']}"
        del att["payload"]


# --- Calcul du chemin de sortie miroir ---

def mbox_to_output_dir(mbox_path: Path) -> Path:
    """Calcule le dossier de sortie miroir pour un fichier .mbox.

    ~/Archives/Courriels/Fastmail/Guillaume/INBOX.mbox
    → ~/Connaissance/Transcriptions/Courriels/Fastmail/Guillaume/INBOX/
    """
    try:
        rel = mbox_path.relative_to(ARCHIVES_ROOT)
    except ValueError:
        rel = Path(mbox_path.stem)
    # Retirer l'extension .mbox pour en faire un dossier
    return EXTRACTION_DIR / rel.with_suffix("")


# --- Main ---

def _collect_sample(mbox_files, filtres, sample_size, since=None, until=None):
    """Collecter un échantillon de messages depuis les mbox pour calibrage/validation."""
    all_messages = []
    for mbox_path in mbox_files:
        if "_extraction" in str(mbox_path):
            continue
        if filtres.is_courriel_folder_ignored(mbox_path.stem):
            continue
        messages = extract_messages_from_mbox(
            mbox_path, filtres=filtres, include_images=False,
            since=since, until=until)
        all_messages.extend(messages)
    # Prendre un échantillon réparti (pas juste les premiers)
    if len(all_messages) > sample_size:
        step = len(all_messages) / sample_size
        all_messages = [all_messages[int(i * step)] for i in range(sample_size)]
    return all_messages


def calibrer(mbox_files, filtres, sample_size=200, since=None, until=None):
    """Scorer un échantillon et produire un rapport de calibrage.

    Écrit ~/Connaissance/.config/scoring-courriels.calibrage.yaml
    """
    import yaml

    print(f"  Calibrage sur {sample_size} messages...", file=sys.stderr)

    messages = _collect_sample(mbox_files, filtres, sample_size, since, until)
    print(f"  Échantillon : {len(messages)} messages collectés", file=sys.stderr)


    seuils = filtres.scoring_config.get("seuils", {})
    seuil_capturer = seuils.get("capturer", 0)
    seuil_ignorer = seuils.get("ignorer", -1)

    captured = []
    zone_grise = []
    ignored = []

    for msg in messages:
        score, reasons = filtres.score_courriel(msg)
        entry = {
            "from": msg.get("from", ""),
            "subject": msg.get("subject", "")[:80],
            "date": msg["date"].strftime("%Y-%m-%d") if msg.get("date") else "?",
            "score": score,
            "reasons": reasons,
            "folder": msg.get("folder", ""),
        }
        if score >= seuil_capturer:
            captured.append(entry)
        elif score <= seuil_ignorer:
            ignored.append(entry)
        else:
            zone_grise.append(entry)

    # Trier par score
    captured.sort(key=lambda x: x["score"])
    ignored.sort(key=lambda x: x["score"], reverse=True)

    # Captures suspectes (score minimal parmi les capturés)
    captures_suspectes = [e for e in captured if e["score"] <= seuil_capturer + 1][:10]

    # Ignorés suspects (score maximal parmi les ignorés)
    ignores_suspects = [e for e in ignored if e["score"] >= seuil_ignorer - 1][:10]

    # Recommandations automatiques
    recommandations = []
    # Domaines fréquents dans les ignorés suspects
    domaines_ignores = {}
    for e in ignored:
        domain = e["from"].split("@")[-1] if "@" in e["from"] else ""
        if domain:
            domaines_ignores[domain] = domaines_ignores.get(domain, 0) + 1
    for domain, count in sorted(domaines_ignores.items(), key=lambda x: -x[1])[:5]:
        if count >= 3:
            recommandations.append(
                f"Domaine fréquent ignoré : {domain} ({count}× dans l'échantillon)")

    total = len(messages)
    rapport = {
        "echantillon": total,
        "seuils": {"capturer": seuil_capturer, "ignorer": seuil_ignorer},
        "repartition": {
            "captures": len(captured),
            "zone_grise": len(zone_grise),
            "ignores": len(ignored),
            "pct_captures": round(len(captured) / total * 100, 1) if total else 0,
            "pct_zone_grise": round(len(zone_grise) / total * 100, 1) if total else 0,
            "pct_ignores": round(len(ignored) / total * 100, 1) if total else 0,
        },
        "captures_suspectes": captures_suspectes,
        "ignores_suspects": ignores_suspects,
        "recommandations": recommandations,
    }

    # Écrire le rapport
    output = BASE_PATH / "Connaissance" / ".config" / "scoring-courriels.calibrage.yaml"
    output.parent.mkdir(parents=False, exist_ok=True)
    output.write_text(yaml.dump(rapport, default_flow_style=False, allow_unicode=True,
                                sort_keys=False), encoding="utf-8")

    # Affichage
    print(f"\n  === Calibrage ===", file=sys.stderr)

    print(f"  Échantillon : {total} messages", file=sys.stderr)

    print(f"  Capturés :    {len(captured):4d} ({rapport['repartition']['pct_captures']}%)", file=sys.stderr)

    print(f"  Zone grise :  {len(zone_grise):4d} ({rapport['repartition']['pct_zone_grise']}%)", file=sys.stderr)

    print(f"  Ignorés :     {len(ignored):4d} ({rapport['repartition']['pct_ignores']}%)", file=sys.stderr)


    if captures_suspectes:
        print(f"\n  Captures suspectes ({len(captures_suspectes)}) :", file=sys.stderr)

        for e in captures_suspectes[:5]:
            print(f"    [{e['score']:+d}] {e['from'][:35]:35s} | {e['subject'][:40]}", file=sys.stderr)


    if ignores_suspects:
        print(f"\n  Ignorés suspects ({len(ignores_suspects)}) :", file=sys.stderr)

        for e in ignores_suspects[:5]:
            print(f"    [{e['score']:+d}] {e['from'][:35]:35s} | {e['subject'][:40]}", file=sys.stderr)


    print(f"\n  Rapport → {output}", file=sys.stderr)



def valider_expediteurs(mbox_files, filtres, sample_size=500, since=None, until=None):
    """Analyser les expéditeurs borderline et proposer whitelist/blacklist.

    Écrit ~/Connaissance/.config/scoring-courriels.expediteurs.yaml
    """
    import yaml
    from collections import defaultdict

    print(f"  Analyse des expéditeurs sur {sample_size} messages...", file=sys.stderr)

    messages = _collect_sample(mbox_files, filtres, sample_size, since, until)
    print(f"  Échantillon : {len(messages)} messages collectés", file=sys.stderr)


    seuils = filtres.scoring_config.get("seuils", {})
    seuil_capturer = seuils.get("capturer", 0)
    seuil_ignorer = seuils.get("ignorer", -1)

    domaines_personnels = {d.lower() for d in filtres.scoring_config.get("domaines_personnels", [])}
    domaines_marketing = {d.lower() for d in filtres.scoring_config.get("domaines_marketing", [])}

    # Grouper par domaine
    by_domain = defaultdict(lambda: {"captured": [], "ignored": [], "zone_grise": []})

    for msg in messages:
        score, reasons = filtres.score_courriel(msg)
        from_addr = msg.get("from", "")
        domain = from_addr.split("@")[-1].lower() if "@" in from_addr else ""
        if not domain:
            continue

        entry = {
            "from": from_addr,
            "subject": msg.get("subject", "")[:60],
            "score": score,
            "reasons": [r.split("[")[0].strip() for r in reasons[:3]],
        }

        if score >= seuil_capturer:
            by_domain[domain]["captured"].append(entry)
        elif score <= seuil_ignorer:
            by_domain[domain]["ignored"].append(entry)
        else:
            by_domain[domain]["zone_grise"].append(entry)

    # Candidats whitelist : domaines avec beaucoup d'ignorés mais des sujets potentiellement importants
    candidats_whitelist = []
    for domain, data in by_domain.items():
        if domain in domaines_personnels or domain in domaines_marketing:
            continue
        n_ignored = len(data["ignored"])
        n_total = n_ignored + len(data["captured"]) + len(data["zone_grise"])
        if n_ignored >= 2 and n_ignored / n_total > 0.5:
            candidats_whitelist.append({
                "domain": domain,
                "ignored": n_ignored,
                "total": n_total,
                "exemples_sujets": [e["subject"] for e in data["ignored"][:3]],
                "signaux": list({r for e in data["ignored"] for r in e["reasons"]})[:5],
            })
    candidats_whitelist.sort(key=lambda x: -x["ignored"])

    # Candidats blacklist : domaines capturés avec score minimal (bruit)
    candidats_blacklist = []
    for domain, data in by_domain.items():
        if domain in domaines_personnels or domain in domaines_marketing:
            continue
        n_captured = len(data["captured"])
        if n_captured < 2:
            continue
        avg_score = sum(e["score"] for e in data["captured"]) / n_captured
        if avg_score <= seuil_capturer + 1:
            candidats_blacklist.append({
                "domain": domain,
                "captured": n_captured,
                "score_moyen": round(avg_score, 1),
                "exemples_sujets": [e["subject"] for e in data["captured"][:3]],
                "signaux": list({r for e in data["captured"] for r in e["reasons"]})[:5],
            })
    candidats_blacklist.sort(key=lambda x: -x["captured"])

    # Candidats en revue : domaines avec mélange capturés/ignorés
    candidats_revue = []
    for domain, data in by_domain.items():
        if domain in domaines_personnels or domain in domaines_marketing:
            continue
        n_cap = len(data["captured"])
        n_ign = len(data["ignored"])
        if n_cap >= 1 and n_ign >= 1:
            candidats_revue.append({
                "domain": domain,
                "captured": n_cap,
                "ignored": n_ign,
                "zone_grise": len(data["zone_grise"]),
                "exemples_captures": [e["subject"] for e in data["captured"][:2]],
                "exemples_ignores": [e["subject"] for e in data["ignored"][:2]],
            })
    candidats_revue.sort(key=lambda x: -(x["captured"] + x["ignored"]))

    rapport = {
        "echantillon": len(messages),
        "domaines_analyses": len(by_domain),
        "candidats_whitelist": candidats_whitelist[:20],
        "candidats_blacklist": candidats_blacklist[:20],
        "candidats_revue": candidats_revue[:20],
    }

    output = BASE_PATH / "Connaissance" / ".config" / "scoring-courriels.expediteurs.yaml"
    output.parent.mkdir(parents=False, exist_ok=True)
    output.write_text(yaml.dump(rapport, default_flow_style=False, allow_unicode=True,
                                sort_keys=False), encoding="utf-8")

    print(f"\n  === Expéditeurs ===", file=sys.stderr)

    print(f"  {len(by_domain)} domaines analysés", file=sys.stderr)

    print(f"  Candidats whitelist :  {len(candidats_whitelist)}", file=sys.stderr)

    print(f"  Candidats blacklist :  {len(candidats_blacklist)}", file=sys.stderr)

    print(f"  Candidats en revue :   {len(candidats_revue)}", file=sys.stderr)

    print(f"\n  Rapport → {output}", file=sys.stderr)



# --- API publique ---


def _parse_dates(since, until):
    if isinstance(since, str):
        since = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if isinstance(until, str):
        until = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return since, until


def _collect_mbox_files(account=None, folder=None):
    search_root = Path(account) if account else ARCHIVES_ROOT
    require_paths(search_root, context="emails")
    if folder:
        folders = [f.strip() for f in folder.split(",")] if isinstance(folder, str) else list(folder)
        result = []
        for f in folders:
            result.extend(search_root.rglob(f"{f}.mbox"))
        return sorted(set(result))
    return sorted(search_root.rglob("*.mbox"))


def stats(account=None, folder=None, since=None, until=None) -> dict:
    """Compte des courriels par dossier mbox (schema EmailsStats)."""
    since, until = _parse_dates(since, until)
    mbox_files = _collect_mbox_files(account, folder)
    filtres = Filtres()

    folders_out: list[dict] = []
    total_count = 0
    total_size = 0
    for mbox_path in mbox_files:
        if "_extraction" in str(mbox_path):
            continue
        if filtres.is_courriel_folder_ignored(mbox_path.stem):
            continue
        messages = extract_messages_from_mbox(
            mbox_path, filtres=filtres, since=since, until=until)
        size = sum(len(m.get("body") or "") for m in messages)
        try:
            name = str(mbox_path.relative_to(ARCHIVES_ROOT))
        except ValueError:
            name = str(mbox_path)
        folders_out.append({"name": name, "count": len(messages), "size": size})
        total_count += len(messages)
        total_size += size

    return {
        "folders": folders_out,
        "totals": {"count": total_count, "size": total_size},
    }


def extract(account=None, folder=None, since=None, until=None,
            dry_run: bool = False, no_images: bool = False,
            db: TrackingDB | None = None) -> dict:
    """Extraire les courriels en markdown (schema EmailsExtract)."""
    since, until = _parse_dates(since, until)
    mbox_files = _collect_mbox_files(account, folder)
    if db is None:
        db = TrackingDB()
    filtres = Filtres()

    extracted = 0
    dedup_skipped = 0
    filtered: dict[str, int] = {}
    written_paths: list[str] = []

    for mbox_path in mbox_files:
        if "_extraction" in str(mbox_path):
            continue
        if filtres.is_courriel_folder_ignored(mbox_path.stem):
            continue
        messages = extract_messages_from_mbox(
            mbox_path, filtres=filtres, include_images=not no_images,
            since=since, until=until,
        )
        output_dir = mbox_to_output_dir(mbox_path)
        attachments_dir = output_dir / "Attachments"
        for msg in messages:
            mid = msg["message_id"]
            if mid and db.has_message_id(mid):
                dedup_skipped += 1
                continue
            ok, reason = filtres.filter_courriel(msg)
            if not ok:
                reason_key = (reason or "filtre").split(":", 1)[0]
                filtered[reason_key] = filtered.get(reason_key, 0) + 1
                continue
            if dry_run:
                extracted += 1
                continue

            save_attachments(msg["attachments"], attachments_dir)
            content = format_email(msg)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{msg['msg_hash']}.md"
            if output_path.exists():
                dedup_skipped += 1
                continue
            output_path.write_text(content, encoding="utf-8")
            extracted += 1
            rel_path = str(output_path.relative_to(BASE_PATH / "Connaissance"))
            written_paths.append(rel_path)
            date_str = msg["date"].strftime("%Y-%m-%dT%H:%M:%S") if msg["date"] else None
            db.register_file(rel_path, "transcription",
                             source_type="courriel",
                             source_path=str(mbox_path),
                             message_id=mid,
                             created=date_str, modified=date_str)
            db.log("transcription", "extract_email",
                   source_type="courriel",
                   source_path=str(mbox_path),
                   dest_path=rel_path,
                   details={"message_id": mid, "folder": msg["folder"]})

    return {
        "extracted": extracted,
        "dedup_skipped": dedup_skipped,
        "filtered": [{"reason": k, "count": v} for k, v in sorted(filtered.items())],
        "written": written_paths,
        "dry_run": dry_run,
    }


def threads(account=None, folder=None, since=None, until=None) -> dict:
    """Regrouper les messages en fils via In-Reply-To/References.

    Construit un graphe des messages par leurs liens RFC 5322, applique
    union-find pour regrouper les composantes connexes, puis retourne un
    dict `{threads, orphans, filtered_below_score}`. Déterministe — pas
    de jugement Claude requis pour le threading.
    """
    since, until = _parse_dates(since, until)
    mbox_files = _collect_mbox_files(account, folder)
    filtres = Filtres()

    messages_by_id: dict[str, dict] = {}
    filtered_below: list[dict] = []
    connaissance_root = BASE_PATH / "Connaissance"

    for mbox_path in mbox_files:
        if "_extraction" in str(mbox_path):
            continue
        if filtres.is_courriel_folder_ignored(mbox_path.stem):
            continue
        output_dir = mbox_to_output_dir(mbox_path)
        for msg in extract_messages_from_mbox(
                mbox_path, filtres=filtres, since=since, until=until):
            ok, reason = filtres.filter_courriel(msg)
            if not ok:
                filtered_below.append({
                    "message_id": msg.get("message_id") or "",
                    "reason": reason,
                })
                continue
            mid = (msg.get("message_id") or "").strip()
            if not mid:
                continue
            msg_path = output_dir / f"{msg['msg_hash']}.md"
            try:
                rel = str(msg_path.relative_to(connaissance_root))
            except ValueError:
                rel = str(msg_path)
            parents: set[str] = set()
            if msg.get("in_reply_to"):
                parents.add(msg["in_reply_to"].strip())
            for r in msg.get("references") or []:
                parents.add(r.strip())
            messages_by_id[mid] = {
                "path": rel,
                "date": msg.get("date"),
                "parents": parents,
            }

    # Union-find sur les messages liés
    parent_map: dict[str, str] = {mid: mid for mid in messages_by_id}

    def find(x: str) -> str:
        while parent_map.get(x, x) != x:
            parent_map[x] = parent_map.get(parent_map[x], parent_map[x])
            x = parent_map[x]
        return x

    def union(a: str, b: str):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent_map[ra] = rb

    for mid, info in messages_by_id.items():
        for p in info["parents"]:
            if p in messages_by_id:
                union(mid, p)

    groups: dict[str, list[str]] = {}
    for mid in messages_by_id:
        root = find(mid)
        groups.setdefault(root, []).append(mid)

    thread_list: list[dict] = []
    orphans: list[dict] = []
    for members in groups.values():
        if len(members) == 1:
            info = messages_by_id[members[0]]
            orphans.append({
                "message_id": members[0],
                "path": info["path"],
                "date": info["date"].isoformat() if info["date"] else None,
            })
            continue
        dates = [messages_by_id[m]["date"] for m in members if messages_by_id[m]["date"]]
        latest = max(dates).isoformat() if dates else None
        members_sorted = sorted(members, key=lambda m: messages_by_id[m]["date"] or datetime.min.replace(tzinfo=timezone.utc))
        thread_list.append({
            "message_ids": members_sorted,
            "paths": [messages_by_id[m]["path"] for m in members_sorted],
            "latest_date": latest,
        })

    return {
        "threads": thread_list,
        "orphans": orphans,
        "filtered_below_score": filtered_below,
    }


def calibrate(sample: int = 200, since=None, until=None, account=None) -> dict:
    """Calibrage du scoring avec `proposed_mutations` pré-calculées.

    Invoque `calibrer()` pour produire le rapport YAML, puis le lit
    et traduit les candidats whitelist/blacklist en atomes `proposed_mutations`
    conformes à l'input schema de `config scoring-set`.
    """
    from datetime import timedelta
    since, until = _parse_dates(since, until)
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=90)

    mbox_files = _collect_mbox_files(account, None)
    filtres = Filtres()

    # calibrer() écrit ~/.config/scoring-courriels.calibrage.yaml
    calibrer(mbox_files, filtres, sample_size=sample, since=since, until=until)

    # Lire le rapport produit
    from connaissance.core.paths import CONNAISSANCE_ROOT
    import yaml as _yaml
    rapport_path = CONNAISSANCE_ROOT / ".config" / "scoring-courriels.calibrage.yaml"
    rapport: dict = {}
    if rapport_path.exists():
        try:
            rapport = _yaml.safe_load(rapport_path.read_text()) or {}
        except _yaml.YAMLError:
            rapport = {}

    # Dériver proposed_mutations depuis les candidats
    candidats_bl = rapport.get("candidats_blacklist", []) or []
    candidats_wl = rapport.get("candidats_whitelist", []) or []

    add_marketing: list[str] = []
    add_personnel: list[str] = []
    for item in candidats_bl:
        dom = (item.get("domaine") if isinstance(item, dict) else str(item))
        if dom:
            add_marketing.append(dom)
    for item in candidats_wl:
        dom = (item.get("domaine") if isinstance(item, dict) else str(item))
        if dom:
            add_personnel.append(dom)

    return {
        "sample": sample,
        "seuils": rapport.get("seuils", {}),
        "repartition": rapport.get("repartition", {}),
        "candidats": {
            "whitelist": candidats_wl,
            "blacklist": candidats_bl,
            "revue": rapport.get("candidats_revue", []),
        },
        "proposed_mutations": {
            "add_domain_marketing": sorted(set(add_marketing)),
            "add_domain_personnel": sorted(set(add_personnel)),
        },
        "rapport_path": str(rapport_path),
    }


def senders(sample: int = 500, since=None, until=None, account=None) -> dict:
    """Analyse des expéditeurs borderline (whitelist/blacklist)."""
    from datetime import timedelta
    since, until = _parse_dates(since, until)
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=90)

    mbox_files = _collect_mbox_files(account, None)
    filtres = Filtres()
    valider_expediteurs(mbox_files, filtres, sample_size=sample,
                        since=since, until=until)

    from connaissance.core.paths import CONNAISSANCE_ROOT
    import yaml as _yaml
    rapport_path = CONNAISSANCE_ROOT / ".config" / "scoring-courriels.expediteurs.yaml"
    rapport: dict = {}
    if rapport_path.exists():
        try:
            rapport = _yaml.safe_load(rapport_path.read_text()) or {}
        except _yaml.YAMLError:
            rapport = {}

    return {
        "sample": sample,
        "candidats": {
            "whitelist": rapport.get("candidats_whitelist", []),
            "blacklist": rapport.get("candidats_blacklist", []),
            "revue": rapport.get("candidats_revue", []),
        },
        "rapport_path": str(rapport_path),
    }


def cleanup_obsolete(dry_run: bool = True, only_domain: str | None = None,
                     only_entity: str | None = None, since=None, until=None,
                     db: TrackingDB | None = None) -> dict:
    """Re-scorer les courriels existants et archiver ceux qui tombent sous seuil."""
    from connaissance.commands import emails_cleanup
    return emails_cleanup.cleanup_obsolete(
        dry_run=dry_run, only_domain=only_domain, only_entity=only_entity,
        since=since, until=until, db=db,
    )
