"""Module commands/notes : scan et copie des notes Apple.

Source : l'export Markdown quotidien d'Apple Notes produit par `mac-automations`
(`anotes export --incremental --git`) sous `~/Archives/Notes/` — voir
`core/paths.py` (`NOTES_EXPORT_DIR`). Un `.export_state.json` à sa racine est
réécrit à chaque export : son mtime sert de **sonde de fraîcheur**, rapportée
par `scan` et `backlog_count` (`export`), pour qu'un job d'export en panne ne
fasse pas ingérer un instantané périmé en silence.

Expose :
- `scan(since, until) -> dict`
- `backlog_count(since, until) -> dict`
- `copy(dry_run=False, since=None, until=None, db=None) -> NotesCopy`
"""
from __future__ import annotations
import sys

import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from connaissance.core.frontmatter import (body_sha256, dump_frontmatter,
                                           parse_frontmatter, split_frontmatter)
from connaissance.core.paths import BASE_PATH, NOTES_EXPORT_DIR, require_paths
from connaissance.core.schemas import NotesCopy
from connaissance.core.tracking import TrackingDB, resolve_file_path
from connaissance.core.filtres import Filtres

NOTES_DIR = NOTES_EXPORT_DIR
TRANSCRIPTIONS_DIR = BASE_PATH / "Connaissance" / "Transcriptions" / "Notes"

# Marqueur écrit par `anotes export` à chaque passage (gitignoré dans l'export).
EXPORT_STATE_FILE = ".export_state.json"
# Au-delà de cet âge, l'export est signalé périmé (le job est quotidien).
EXPORT_STALE_DAYS = 7


def export_status(notes_dir: Path | None = None) -> dict:
    """État de fraîcheur de l'export Apple Notes.

    Retourne ``{last_export, age_days, stale, source}`` :
    - ``last_export`` : date ISO (UTC) du dernier export, d'après le mtime de
      `.export_state.json` ; ``None`` si le marqueur est absent (export jamais
      fait, ou dossier qui n'est pas un export anotes) ;
    - ``age_days`` : jours écoulés depuis ``last_export`` (``None`` si inconnu) ;
    - ``stale`` : ``True`` si l'âge dépasse ``EXPORT_STALE_DAYS`` **ou** si le
      marqueur manque — dans les deux cas, l'ingestion travaille sur un
      instantané dont personne ne garantit la fraîcheur.
    """
    root = notes_dir if notes_dir is not None else NOTES_DIR
    marker = root / EXPORT_STATE_FILE
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        return {"last_export": None, "age_days": None, "stale": True,
                "source": str(root)}
    last = datetime.fromtimestamp(mtime, tz=timezone.utc)
    age = (datetime.now(tz=timezone.utc) - last).days
    return {
        "last_export": last.isoformat(timespec="seconds"),
        "age_days": age,
        "stale": age > EXPORT_STALE_DAYS,
        "source": str(root),
    }


_FM_DATE_RE = r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}:\d{2}))?"


def _parse_frontmatter_dates(content: str) -> dict[str, str]:
    """Extraire created/modified du frontmatter YAML, en ISO.

    L'export anotes écrit ``modified: 2026-01-06 03:34:27`` ; on normalise en
    ``2026-01-06T03:34:27`` (la forme stockée dans ``files.modified``). Sans
    heure, la date seule est conservée.
    """
    dates = {}
    parts = split_frontmatter(content)
    if parts is None:
        return dates
    fm_text = parts[0]
    for field in ("created", "modified"):
        match = re.search(rf"^{field}:\s*['\"]?{_FM_DATE_RE}", fm_text, re.MULTILINE)
        if match:
            day, clock = match.group(1), match.group(2)
            dates[field] = f"{day}T{clock}" if clock else day
    return dates


# Préfixes sous lesquels `files.source_path` a historiquement désigné une note
# de l'export (relatifs au home, cf. `canon_file_path`). `Connaissance/Notes/`
# vient de l'ancien export `~/Notes/` re-préfixé par `_CONN_TOPS`.
_SOURCE_PREFIXES = ("Archives/Notes/", "Connaissance/Notes/", "Notes/")


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def note_rel_from_source(source_path: str | None) -> str | None:
    """Chemin d'une note RELATIF à la racine de l'export, d'après la valeur
    ``files.source_path`` — quelle que soit la convention avec laquelle elle a
    été écrite (absolue `~/Notes/…` ou `~/Archives/Notes/…`, relative
    `Connaissance/Notes/…`, `Notes/…`, `Archives/Notes/…`). ``None`` si la
    valeur ne désigne pas une note de l'export."""
    if not source_path:
        return None
    s = _nfc(str(source_path))
    for root in (NOTES_DIR, BASE_PATH / "Notes"):
        r = _nfc(str(root)) + "/"
        if s.startswith(r):
            return s[len(r):]
    for pfx in _SOURCE_PREFIXES:
        if s.startswith(pfx):
            return s[len(pfx):]
    return None


class _Known:
    """Notes déjà ingérées, indexées par identité stable (``apple_id`` →
    ``files.source_id``) et par chemin relatif à l'export.

    L'``apple_id`` prime : une note renommée dans Apple Notes change de nom de
    fichier dans l'export, mais pas d'identité. Le chemin sert aux lignes
    héritées (copies d'avant ``source_id``)."""

    def __init__(self, rows):
        self.by_id: dict[str, dict] = {}
        self.by_rel: dict[str, dict] = {}
        for row in rows:
            sid = row.get("source_id")
            if sid:
                self.by_id[str(sid)] = row
            rel = note_rel_from_source(row.get("source_path"))
            if rel:
                self.by_rel[_nfc(rel)] = row

    def __len__(self) -> int:
        return len({id(r) for r in (*self.by_id.values(), *self.by_rel.values())})

    def lookup(self, apple_id: str | None, rel: str) -> dict | None:
        if apple_id and apple_id in self.by_id:
            return self.by_id[apple_id]
        return self.by_rel.get(_nfc(rel))


def _known_notes(db: TrackingDB | None) -> _Known:
    """Notes déjà ingérées (vide sans DB)."""
    return _Known(db.note_transcriptions() if db is not None else [])


def _note_apple_id(content: str) -> str | None:
    """``apple_id`` du frontmatter d'une note de l'export (regex, sans YAML)."""
    parts = split_frontmatter(content)
    if parts is None:
        return None
    m = re.search(r"^apple_id:\s*['\"]?([0-9A-Fa-f-]{8,})", parts[0], re.MULTILINE)
    return m.group(1) if m else None


def _local_iso_to_ts(value: str | None) -> float | None:
    """``files.updated_at`` (``YYYY-MM-DDTHH:MM:SS``, heure locale) → epoch."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
    except ValueError:
        return None


def _norm_text(body: str) -> str:
    """Texte « nu » d'un corps Markdown, pour comparer deux rendus.

    L'export `~/Notes/` (exporteur d'avant mars 2026) et `anotes export` ne
    rendent pas une même note à l'identique : emphase placée autrement
    (``**🚀 Lointain:**`` / ``🚀 **Lointain:**``), liens de tags présents ou
    non, échappements. Pour une transcription héritée (sans ``files.hash``),
    comparer les hashs bruts classerait « modifiée » toute note jamais
    retouchée — et périmerait son résumé pour rien (constaté le 2026-08-23 :
    14 des 42 « modifiées » n'étaient que du rendu). On neutralise : liens →
    leur texte, marqueurs d'emphase / titres / listes / échappements retirés,
    blancs repliés, lignes vides ignorées. Les mots restent : une case cochée,
    une ligne ajoutée, un chiffre changé se voient toujours.
    """
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", body)
    t = re.sub(r"[*_`\\#>|-]", "", t)
    lines = (re.sub(r"\s+", " ", line).strip() for line in t.splitlines())
    return "\n".join(line for line in lines if line)


def _extract_attachment_refs(content: str) -> set[str]:
    """Extraire les noms de fichiers Attachments/ référencés dans le contenu."""
    refs = set()
    for match in re.findall(r'(?:!\[.*?\]|)\(Attachments/([^)]+)\)', content):
        refs.add(match)
    # Liens Markdown classiques aussi
    for match in re.findall(r'\[.*?\]\(Attachments/([^)]+)\)', content):
        refs.add(match)
    return refs


def backlog_count(since=None, until=None, db: TrackingDB | None = None) -> dict:
    """Compte rapide de notes à copier/mettre à jour, sans lire les contenus.

    **Ne lit AUCUN contenu Markdown** — contrairement à `scan`, qui lit
    chaque `.md` pour extraire le frontmatter YAML et filtrer par dates
    `created/modified`. Ici, on se base uniquement sur :

    - `rglob("*.md")` sur l'export `~/Archives/Notes/`
    - `f.stat().st_mtime` pour le filtre `since/until` (approximation du
      champ `created` du frontmatter, mais cohérent avec la sémantique
      « quelque chose à (re)copier »).
    - `tracking.db` (``files``, ``source_type='note'``) pour les notes déjà
      ingérées : une note connue compte « à mettre à jour » si le fichier de
      l'export (réécrit par `anotes export --incremental` quand la note
      change) est plus récent que l'enregistrement (``updated_at``) ;
      sinon elle est à jour. Le miroir `Transcriptions/Notes/<rel>` ne sert
      qu'aux notes inconnues de la DB : `organize apply` déplace les
      transcriptions par entité, son existence ne prouve rien.

    Retourne un count (`to_copy`, `to_update`) sans parser les notes.
    Trade-off : le filtre par date utilise `mtime` et non `created` du
    frontmatter ; une note ancienne modifiée récemment compte comme
    récente. Pour un compte exact (hash de contenu), utiliser `scan`.
    """
    if db is None:
        db = TrackingDB()
    if not NOTES_DIR.exists():
        return {
            "total_to_copy": 0,
            "to_copy": 0,
            "to_update": 0,
            "skipped_total": 0,
            "note": f"L'export Apple Notes {NOTES_DIR} n'existe pas.",
            "export": export_status(),
        }

    # Bornes de date en epoch pour éviter de re-parser à chaque fichier.
    since_ts: float | None = None
    until_ts: float | None = None
    if since:
        if isinstance(since, str):
            try:
                since_ts = datetime.strptime(since, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                since_ts = None
        elif isinstance(since, datetime):
            since_ts = since.timestamp()
    if until:
        if isinstance(until, str):
            try:
                until_ts = datetime.strptime(until, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                until_ts = None
        elif isinstance(until, datetime):
            until_ts = until.timestamp()

    to_copy = 0
    to_update = 0
    skipped = 0
    known = _known_notes(db)

    for f in NOTES_DIR.rglob("*.md"):
        if not f.is_file() or "Attachments" in f.parts:
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            skipped += 1
            continue

        if since_ts is not None and mtime < since_ts:
            skipped += 1
            continue
        if until_ts is not None and mtime >= until_ts:
            skipped += 1
            continue

        try:
            rel = f.relative_to(NOTES_DIR)
        except ValueError:
            skipped += 1
            continue

        row = known.by_rel.get(_nfc(str(rel)))
        if row is not None:
            registered = _local_iso_to_ts(row.get("updated_at"))
            # `updated_at` est à la seconde : tolérance d'une seconde pour
            # qu'une note enregistrée dans la même seconde ne repasse pas.
            if registered is not None and mtime > registered + 1:
                to_update += 1
            else:
                skipped += 1
            continue

        dest = TRANSCRIPTIONS_DIR / rel
        if not dest.exists():
            to_copy += 1
            continue
        try:
            if mtime > dest.stat().st_mtime:
                to_update += 1
            else:
                skipped += 1
        except OSError:
            skipped += 1

    return {
        "total_to_copy": to_copy + to_update,
        "to_copy": to_copy,
        "to_update": to_update,
        "skipped_total": skipped,
        "known_in_db": len(known),
        "export": export_status(),
        "note": (
            "Borne approximative du backlog notes : filtre par mtime du "
            "fichier au lieu du champ `created` du frontmatter ; « à mettre "
            "à jour » = export plus récent que l'enregistrement en DB, sans "
            "lire le contenu. Pour un compte exact (hash), lancer `notes_scan`."
        ),
    }


def scan_notes(since=None, until=None, db: TrackingDB | None = None):
    """Scanner l'export Apple Notes et retourner les notes à copier/mettre à jour.

    Trois statuts :

    - ``nouveau`` — note inconnue de ``tracking.db`` et sans miroir : copie
      brute vers `Transcriptions/Notes/<rel>`.
    - ``modifie`` — note connue dont le **corps** (hash, frontmatter exclu)
      diffère de la transcription ACTUELLE (``files.path``, là où `organize
      apply` l'a rangée) : réécriture sur place, frontmatter enrichi préservé.
    - ``manquante`` — note connue dont la transcription a disparu du disque :
      recréée à l'emplacement enregistré (la DB reste cohérente).

    Une note connue au corps identique est « à jour », même si le miroir
    `Transcriptions/Notes/<rel>` n'existe plus — c'est le cas de toutes les
    notes rangées par entité.
    """
    if not NOTES_DIR.exists():
        return [], {}

    filtres = Filtres()
    to_process = []
    skipped = {}
    known = _known_notes(db)

    for f in sorted(NOTES_DIR.rglob("*.md")):
        if not f.is_file():
            continue
        if "Attachments" in f.parts:
            continue

        # Filtrage (dossiers ignorés + dates frontmatter)
        try:
            content = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped["erreur_lecture"] = skipped.get("erreur_lecture", 0) + 1
            continue

        ok, reason = filtres.filter_note(f, content=content, since=since,
                                            until=until, root=NOTES_DIR)
        if not ok:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue

        rel = f.relative_to(NOTES_DIR)
        note_hash = body_sha256(content)
        apple_id = _note_apple_id(content)
        row = known.lookup(apple_id, str(rel))
        # Même identité, autre chemin dans l'export : la note a été renommée
        # ou déplacée dans Apple Notes → rafraîchir `source_path` (frontmatter
        # + DB) même si le corps n'a pas changé.
        renamed = (row is not None
                   and note_rel_from_source(row.get("source_path")) is not None
                   and _nfc(note_rel_from_source(row.get("source_path"))) != _nfc(str(rel)))

        if row is not None:
            # Déjà ingérée : la vérité est la transcription ACTUELLE.
            dest = resolve_file_path(row["path"])
            if not dest.exists():
                status = "manquante"
            else:
                current = row.get("hash")
                if current:
                    same = current == note_hash
                else:
                    # Ligne héritée (copie d'avant le hash) : la transcription
                    # vient peut-être d'un autre exporteur → comparer le texte
                    # nu, pas le rendu.
                    try:
                        old_body = split_frontmatter(
                            dest.read_text(encoding="utf-8"))
                        old_body = old_body[1] if old_body else ""
                        same = _norm_text(old_body) == _norm_text(
                            (split_frontmatter(content) or ("", content))[1])
                    except (OSError, UnicodeDecodeError):
                        same = False
                    if same and not renamed and body_sha256(old_body) != note_hash:
                        skipped["a_jour_rendu"] = skipped.get("a_jour_rendu", 0) + 1
                        continue
                if same and renamed:
                    status = "renommee"
                elif same:
                    skipped["a_jour"] = skipped.get("a_jour", 0) + 1
                    continue
                else:
                    status = "modifie"
        else:
            # Inconnue de la DB : miroir (copies antérieures au suivi).
            dest = TRANSCRIPTIONS_DIR / rel
            status = "nouveau"
            if dest.exists():
                if f.stat().st_mtime > dest.stat().st_mtime:
                    status = "modifie"
                else:
                    skipped["a_jour"] = skipped.get("a_jour", 0) + 1
                    continue

        # Attachements référencés
        att_refs = _extract_attachment_refs(content)
        att_dir = f.parent / "Attachments"
        attachments = []
        for att_name in att_refs:
            att_src = att_dir / att_name
            if att_src.exists():
                attachments.append(att_name)

        dates = _parse_frontmatter_dates(content)

        to_process.append({
            "source": str(f),
            "destination": str(dest),
            "rel": str(rel),
            "status": status,
            "tracked": row is not None,
            "apple_id": apple_id,
            "hash": note_hash,
            "size": f.stat().st_size,
            "attachments": attachments,
            "created": dates.get("created"),
            "modified": dates.get("modified"),
        })

    return to_process, skipped


# Clés du frontmatter de la note qui priment sur celles de la transcription
# lors d'une mise à jour ; le reste (``date``, enrichissements d'`organize`)
# est conservé.
_NOTE_FM_KEYS = ("title", "apple_id", "source")


def _write_transcription(src: Path, dest: Path, content: str, dates: dict,
                         rel: str) -> None:
    """Écrire (ou réécrire) la transcription d'une note.

    Toujours par recomposition — jamais de copie brute — pour que le
    frontmatter porte ``source_path`` : le chemin de la note RELATIF à la
    racine de l'export. C'est l'identité d'origine qui permet à
    ``audit reindex-db`` de reconstruire ``files.source_path`` une fois la
    transcription rangée par entité (son chemin ne dit plus d'où elle vient).
    ``apple_id`` (déjà dans le frontmatter de l'export) est l'identité stable.

    Transcription existante : son frontmatter enrichi (``date``, formes ISO
    quotées) est conservé ; ``title``/``apple_id``/``source``, les dates et
    ``source_path`` sont rafraîchis depuis la note ; le corps est celui de la
    note. Transcription absente : frontmatter de la note + ``source_path``.
    """
    note_parts = split_frontmatter(content)
    note_fm = parse_frontmatter(content) or {}
    body = note_parts[1] if note_parts is not None else "\n" + content
    fm: dict = {}
    if dest.exists():
        fm = parse_frontmatter(dest.read_text(encoding="utf-8")) or {}
    else:
        fm = dict(note_fm)
    for key in _NOTE_FM_KEYS:
        if key in note_fm:
            fm[key] = note_fm[key]
    for key in ("created", "modified"):
        if dates.get(key):
            fm[key] = dates[key]
    fm["source_path"] = rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(dump_frontmatter(fm, body), encoding="utf-8")


def copy_notes(items, db, dry_run=False):
    """Copier les notes et leurs attachements."""
    copied = 0
    updated = 0
    att_copied = 0

    for item in items:
        src = Path(item["source"])
        dest = Path(item["destination"])
        status = item["status"]

        if dry_run:
            label = {"nouveau": "→ copier", "manquante": "→ recréer",
                     "renommee": "→ renommée (origine)"}.get(status, "→ mettre à jour")
            print(f"  {label} : {item['rel']}", file=sys.stderr)

            if item["attachments"]:
                print(f"    + {len(item['attachments'])} attachement(s)", file=sys.stderr)

            if status == "nouveau":
                copied += 1
            else:
                updated += 1
            att_copied += len(item["attachments"])
            continue

        # Nouveau : frontmatter de la note + source_path. Existante (rangée
        # par entité ou non) : corps de la note, frontmatter enrichi conservé.
        # `files.mtime` passe au présent ci-dessous → `resumes_perimes` voit
        # le résumé périmé (préfiltre mtime, puis hash vs source_content_hash).
        # Renommée : même corps, seul `source_path` change.
        _write_transcription(src, dest, src.read_text(encoding="utf-8"),
                             {"created": item.get("created"),
                              "modified": item.get("modified")},
                             item["rel"])

        # Copier les attachements référencés
        att_src_dir = src.parent / "Attachments"
        att_dst_dir = dest.parent / "Attachments"
        for att_name in item["attachments"]:
            att_src = att_src_dir / att_name
            att_dst = att_dst_dir / att_name
            if att_src.exists() and (not att_dst.exists() or att_src.stat().st_mtime > att_dst.stat().st_mtime):
                att_dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(att_src), str(att_dst))
                att_copied += 1

        # Tracking
        try:
            rel_path = str(dest.relative_to(BASE_PATH / "Connaissance"))
        except ValueError:
            rel_path = str(dest)

        try:
            st = dest.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime, size = None, None
        db.register_file(rel_path, "transcription",
                         source_type="note",
                         source_path=str(src),
                         created=item.get("created"),
                         modified=item.get("modified"),
                         hash=item.get("hash"),
                         mtime=mtime, size=size,
                         source_id=item.get("apple_id"))
        db.log("connaissance",
               "copy_note" if status == "nouveau" else "update_note",
               source_type="note",
               source_path=str(src),
               dest_path=rel_path)

        if status == "nouveau":
            copied += 1
        else:
            updated += 1

    return copied, updated, att_copied


# --- API publique ---


def _parse_dates(since, until):
    if isinstance(since, str):
        since = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if isinstance(until, str):
        until = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return since, until


def scan(since=None, until=None, output_file: str | None = None,
         db: TrackingDB | None = None) -> dict:
    """Lister les notes à copier (schema dict avec to_copy + skipped).

    Si ``output_file`` est fourni, le payload complet (~700 Ko sur un Apple
    Notes chargé) est écrit dans ce fichier et seules des métadonnées sont
    renvoyées : ``{output_file, total_bytes, total_to_copy, total_skipped,
    skipped}``.
    """
    require_paths(NOTES_DIR, context="notes scan")
    since, until = _parse_dates(since, until)
    if db is None:
        db = TrackingDB()
    to_process, skipped = scan_notes(since, until, db=db)
    skipped_list = [{"reason": k, "count": v} for k, v in sorted(skipped.items())]
    by_status: dict[str, int] = {}
    for it in to_process:
        by_status[it["status"]] = by_status.get(it["status"], 0) + 1
    payload = {
        "to_copy": to_process,
        "skipped": skipped_list,
        "by_status": dict(sorted(by_status.items())),
        "export": export_status(),
    }
    from connaissance.core.output_file import write_or_inline

    def _summary(p: dict) -> dict:
        items = p["to_copy"]
        # Répartition par année à partir du `created` du frontmatter
        # (champ déjà extrait par `scan_notes`).
        year_counts: dict[str, int] = {}
        for it in items:
            created = str(it.get("created") or "")[:4]
            key = created if created.isdigit() else "inconnu"
            year_counts[key] = year_counts.get(key, 0) + 1
        by_year = dict(sorted(year_counts.items()))
        sample = [it.get("rel") or it.get("source") for it in items[:5]]
        return {
            "total_to_copy": len(items),
            "total_skipped": sum(x["count"] for x in p["skipped"]),
            "skipped": p["skipped"],
            "by_status": p["by_status"],
            "by_year": by_year,
            "sample_to_copy": sample,
            "export": p["export"],
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)


def copy(dry_run: bool = False, since=None, until=None,
         db: TrackingDB | None = None) -> NotesCopy:
    """Copier les notes (schema NotesCopy)."""
    require_paths(NOTES_DIR, context="notes copy")
    since, until = _parse_dates(since, until)
    if db is None:
        db = TrackingDB()
    to_process, _ = scan_notes(since, until, db=db)
    if not to_process:
        return {"copied": 0, "skipped": 0, "errors": []}
    copied, updated, att_copied = copy_notes(to_process, db, dry_run=dry_run)
    return {
        "copied": copied + updated,
        "skipped": 0,
        "errors": [],
        "attachments_copied": att_copied,
        "dry_run": dry_run,
    }
