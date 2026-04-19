"""Module commands/documents : scan, registration et audit des documents.

Expose :
- `scan(since, until, db=None) -> DocumentsScan`
- `register(source, transcription, ...)` + `register_existing_all()`
- `suspects() -> DocumentsSuspects`
"""

import sys
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from connaissance.core.paths import BASE_PATH, require_paths
from connaissance.core.tracking import TrackingDB
from connaissance.core.filtres import Filtres

DOCUMENTS_DIR = BASE_PATH / "Documents"
TRANSCRIPTIONS_DIR = BASE_PATH / "Connaissance" / "Transcriptions" / "Documents"

# Champs canoniques du frontmatter de transcription de document.
# Les autres champs présents dans un frontmatter existant sont préservés.
# `created`/`modified` sont dérivés du filesystem de la source (birthtime,
# mtime) et injectés pour que le pipeline puisse filtrer par date métier
# sans avoir à inventer de fallback sur le nom de fichier.
TRANSCRIPTION_FRONTMATTER_FIELDS = (
    "source", "source_hash", "source_size", "transcribed_at",
    "created", "modified",
)


def _date_from_filename(name_or_path: str) -> str | None:
    """Extraire une date ``YYYY-MM-DD`` d'un nom de fichier s'il en contient une.

    Cherche un motif ``\\d{4}-\\d{2}-\\d{2}`` plausible (année 1990-2099, mois
    01-12, jour 01-31). Retourne ``YYYY-MM-DDT00:00:00`` ou ``None``.

    Utilisé comme fallback uniquement quand la source n'existe plus sur disque
    — la convention de nommage ``YYYY-MM-DD foo.pdf`` est une donnée
    intentionnelle de l'utilisateur, pas une invention du pipeline.
    """
    m = re.search(r"\b(19[9]\d|20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b",
                  name_or_path)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00"


def _source_dates(source_path: Path) -> tuple[str | None, str | None]:
    """Retourner (created, modified) d'une source au format ISO, ou (None, None).

    Priorité :
    1. Filesystem (birthtime pour ``created`` si dispo, mtime sinon) — valeur
       la plus fiable quand la source existe.
    2. Fallback sur une date présente dans le chemin (``YYYY-MM-DD``) quand
       la source n'existe plus sur disque. Dans ce cas, ``created`` est
       renseigné, ``modified`` reste ``None``.

    Formats : ``YYYY-MM-DDTHH:MM:SS`` (sans timezone pour la cohérence
    avec le reste du pipeline : les courriels et notes utilisent déjà
    ce format).
    """
    try:
        st = source_path.stat()
    except OSError:
        # Source introuvable : dernier recours, utiliser une date éventuellement
        # encodée dans le nom du chemin (ex. "Documents/.../2026-01-25 foo.pdf").
        return _date_from_filename(str(source_path)), None
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    birthtime = None
    if hasattr(st, "st_birthtime") and st.st_birthtime > 0:
        birthtime = datetime.fromtimestamp(st.st_birthtime, tz=timezone.utc)
    created = (birthtime or mtime).strftime("%Y-%m-%dT%H:%M:%S")
    modified = mtime.strftime("%Y-%m-%dT%H:%M:%S")
    return created, modified


def hash_file(path):
    """Calculer le SHA256 d'un fichier."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _merge_frontmatter(content: str, new_fields: dict) -> str:
    """Injecter ou mettre à jour un frontmatter YAML en tête d'un document markdown.

    Si `content` commence par un bloc frontmatter (--- ... ---), les clés de
    `new_fields` sont fusionnées dedans ; les champs existants non listés sont
    préservés. Sinon, un nouveau bloc est prepend.

    Les valeurs None dans `new_fields` sont ignorées (pas d'écrasement par None).
    """
    # Filtrer les None
    updates = {k: v for k, v in new_fields.items() if v is not None}
    if not updates:
        return content

    if content.startswith("---"):
        # Chercher la fin du frontmatter : "\n---" suivi de "\n" ou EOF
        # On accepte aussi "---" en toute fin de fichier.
        end_nl = content.find("\n---\n", 4)
        end_eof = content.find("\n---", 4)
        if end_nl >= 0:
            fm_end = end_nl
            body_start = end_nl + len("\n---\n")
        elif end_eof >= 0 and content[end_eof:].rstrip() == "---":
            fm_end = end_eof
            body_start = len(content)
        else:
            # Frontmatter mal formé : on prepend quand même un nouveau bloc
            fm_end = None
            body_start = 0

        if fm_end is not None:
            raw = content[4:fm_end].lstrip("\n")
            try:
                existing = yaml.safe_load(raw) or {}
                if not isinstance(existing, dict):
                    existing = {}
            except yaml.YAMLError:
                existing = {}
            existing.update(updates)
            # Forcer les valeurs datetime/date en chaîne ISO avec T — sinon
            # yaml.safe_dump ré-émet "2024-03-15 12:00:00" (espace, YAML
            # canonique) au lieu de "2024-03-15T12:00:00" (ISO 8601).
            for k, v in list(existing.items()):
                if hasattr(v, "strftime"):
                    existing[k] = (v.strftime("%Y-%m-%dT%H:%M:%S")
                                   if hasattr(v, "hour") else v.isoformat())
            new_fm = yaml.safe_dump(existing, sort_keys=False,
                                    allow_unicode=True, default_flow_style=False).strip()
            return f"---\n{new_fm}\n---\n{content[body_start:]}"

    # Pas de frontmatter existant : idem, normaliser les datetime dans updates.
    for k, v in list(updates.items()):
        if hasattr(v, "strftime"):
            updates[k] = (v.strftime("%Y-%m-%dT%H:%M:%S")
                          if hasattr(v, "hour") else v.isoformat())
    new_fm = yaml.safe_dump(updates, sort_keys=False,
                            allow_unicode=True, default_flow_style=False).strip()
    separator = "\n\n" if content else "\n"
    return f"---\n{new_fm}\n---{separator}{content}"


def _upsert_transcription_frontmatter(trans_path: Path, source_path: Path,
                                      file_hash: str | None,
                                      source_size: int | None) -> None:
    """Injecter ou mettre à jour le frontmatter canonique d'une transcription.

    Idempotent : ré-exécutable sans effet secondaire. Les champs non standards
    d'un frontmatter existant sont préservés.
    """
    if not trans_path.exists():
        return

    try:
        source_rel = str(source_path.relative_to(BASE_PATH))
    except ValueError:
        source_rel = str(source_path)

    created, modified = _source_dates(source_path)

    # Lire le frontmatter existant pour décider si `transcribed_at` est à
    # (re)mettre à jour : on veut un horodatage stable tant que le hash de
    # la source n'a pas changé — ça évite de polluer les diffs à chaque
    # reindex et permet une vraie traçabilité (date de la dernière OCR
    # effective). On rafraîchit toujours `created`/`modified` depuis la
    # source (la date métier bouge via rename/touch même sans re-OCR).
    try:
        content = trans_path.read_text(encoding="utf-8")
    except OSError:
        return

    existing_fm: dict = {}
    if content.startswith("---"):
        end = content.find("\n---", 4)
        if end > 0:
            try:
                existing_fm = yaml.safe_load(content[4:end]) or {}
                if not isinstance(existing_fm, dict):
                    existing_fm = {}
            except yaml.YAMLError:
                existing_fm = {}

    prev_hash = str(existing_fm.get("source_hash") or "").removeprefix("sha256:")
    new_hash_str = f"sha256:{file_hash}" if file_hash else None
    # `transcribed_at` ne bouge que si (a) absent ou (b) le hash change
    # (ré-OCR réelle).
    keep_transcribed_at = (
        existing_fm.get("transcribed_at")
        and (not file_hash or prev_hash == file_hash)
    )
    transcribed_at = (
        existing_fm["transcribed_at"]
        if keep_transcribed_at
        else datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    )

    new_fields = {
        "source": source_rel,
        "source_hash": new_hash_str,
        "source_size": source_size if source_size is not None else None,
        "transcribed_at": transcribed_at,
        "created": created,
        "modified": modified,
    }

    new_content = _merge_frontmatter(content, new_fields)
    if new_content != content:
        try:
            trans_path.write_text(new_content, encoding="utf-8")
        except OSError:
            pass


def backlog_count(since=None, until=None) -> dict:
    """Compte rapide de documents à transcrire.

    **Ne calcule AUCUN SHA256** — contrairement à `scan`, ce qui lit chaque
    fichier intégralement. Parcourt l'arborescence `~/Documents/`, applique
    le filtre (extension, dossiers exclus, dates par mtime) et vérifie
    seulement l'existence de la transcription miroir (`Transcriptions/
    Documents/<rel>.md`).

    Trade-off vs `scan` :
    - Perd la détection `source_changed` (PDF remplacé à même chemin ne
      sera pas détecté — pas de comparaison de hash).
    - Perd la dédup par SHA256 contre `tracking.db` (un PDF déplacé vers
      un nouveau chemin sera compté comme à transcrire alors qu'il l'est
      déjà par hash).

    Retourne une **borne supérieure** du backlog documents. Pour un compte
    exact, utiliser `scan`. Conçu comme alternative timeout-safe pour les
    overviews du skill `pipeline`.
    """
    if not DOCUMENTS_DIR.exists():
        return {
            "total_to_transcribe": 0,
            "by_year": {},
            "skipped": {},
            "note": "~/Documents n'existe pas.",
        }

    if isinstance(since, str):
        since = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if isinstance(until, str):
        until = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    filtres = Filtres()
    total = 0
    by_year: dict[str, int] = {}
    skipped: dict[str, int] = {}

    for f in sorted(DOCUMENTS_DIR.rglob("*")):
        if not f.is_file():
            continue
        ok, reason = filtres.filter_document(f, since=since, until=until)
        if not ok:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        rel = f.relative_to(DOCUMENTS_DIR)
        trans_path = TRANSCRIPTIONS_DIR / rel.with_suffix(".md")
        if trans_path.exists():
            skipped["existant"] = skipped.get("existant", 0) + 1
            continue
        total += 1
        try:
            year = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y")
        except OSError:
            year = "?"
        by_year[year] = by_year.get(year, 0) + 1

    return {
        "total_to_transcribe": total,
        "by_year": dict(sorted(by_year.items(), reverse=True)),
        "skipped": skipped,
        "note": (
            "Borne supérieure du backlog documents : pas de hash SHA256, "
            "pas de détection de source_changed, pas de dedup par hash "
            "contre tracking.db. Pour un compte exact, lancer "
            "`documents_scan`."
        ),
    }


def scan_documents(since=None, until=None, db=None):
    """Scanner ~/Documents/ et retourner les fichiers à transcrire."""
    if not DOCUMENTS_DIR.exists():
        return [], {}

    filtres = Filtres()
    if db is None:
        db = TrackingDB()
    to_process = []
    skipped = {}

    for f in sorted(DOCUMENTS_DIR.rglob("*")):
        if not f.is_file():
            continue

        # Filtre unifié
        ok, reason = filtres.filter_document(f, since=since, until=until)
        if not ok:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue

        # Transcription existante (miroir)
        rel = f.relative_to(DOCUMENTS_DIR)
        trans_path = TRANSCRIPTIONS_DIR / rel.with_suffix(".md")
        if trans_path.exists():
            # Vérifier si le source a changé (PDF remplacé) : comparer le hash
            # du frontmatter canonique avec le hash actuel du fichier source.
            file_hash = hash_file(f)
            if file_hash:
                stored_hash = None
                try:
                    content = trans_path.read_text(encoding="utf-8")
                    if content.startswith("---"):
                        end = content.find("\n---", 4)
                        if end > 0:
                            fm = yaml.safe_load(content[4:end])
                            if isinstance(fm, dict) and fm.get("source_hash"):
                                h = str(fm["source_hash"])
                                stored_hash = h[len("sha256:"):] if h.startswith("sha256:") else h
                except (OSError, yaml.YAMLError):
                    pass
                if stored_hash and stored_hash != file_hash:
                    to_process.append({
                        "source": str(f),
                        "transcription": str(trans_path),
                        "rel": str(rel),
                        "size": f.stat().st_size,
                        "hash": file_hash,
                        "reason": "source_changed",
                    })
                    continue
            skipped["existant"] = skipped.get("existant", 0) + 1
            continue

        # Déduplication SHA256 via la DB de tracking
        file_hash = hash_file(f)
        if file_hash and db.has_hash(file_hash):
            skipped["hash_connu"] = skipped.get("hash_connu", 0) + 1
            continue

        to_process.append({
            "source": str(f),
            "transcription": str(trans_path),
            "rel": str(rel),
            "size": f.stat().st_size,
            "hash": file_hash,
        })

    return to_process, skipped


def register_document(db, source_path, transcription_path, file_hash=None):
    """Enregistrer un document transcrit dans la DB + frontmatter canonique."""
    source_path = Path(source_path)
    transcription_path = Path(transcription_path)

    if not file_hash:
        file_hash = hash_file(source_path)

    try:
        source_size = source_path.stat().st_size if source_path.exists() else None
    except OSError:
        source_size = None

    # Injecter le frontmatter canonique dans le fichier transcription.
    # Source de vérité pour source/hash/size ; la DB est un index dérivé.
    _upsert_transcription_frontmatter(transcription_path, source_path,
                                      file_hash, source_size)

    if file_hash:
        db.register_hash(file_hash, str(source_path), source_size or 0)

    try:
        rel = str(transcription_path.relative_to(BASE_PATH / "Connaissance"))
    except ValueError:
        rel = str(transcription_path)

    created, modified = _source_dates(source_path)
    db.register_file(rel, "transcription",
                     source_type="document",
                     source_path=str(source_path),
                     hash=file_hash,
                     created=created,
                     modified=modified)

    db.log("connaissance", "ocr",
           source_type="document",
           source_path=str(source_path),
           dest_path=rel,
           details={"hash": file_hash})


def _parse_table_rows(content: str) -> list[dict]:
    """Extraire les tableaux markdown et compter les cellules vides par ligne.

    Un tableau est détecté par une séquence de lignes commençant par '|'
    dont une ligne de séparateur '| --- |' est présente. Retourne une liste
    de dicts : [{start_line, end_line, col_count, rows: [{cells_total, cells_empty}]}].
    """
    tables = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        # Un tableau commence par une ligne de pipes avec une ligne séparateur juste après
        if line.startswith("|") and i + 1 < len(lines):
            sep = lines[i + 1].strip()
            if re.match(r'^\|[\s\-:|]+\|$', sep) and "---" in sep:
                # C'est bien une table
                start = i
                header_cells = [c.strip() for c in line.strip("|").split("|")]
                col_count = len(header_cells)
                rows = []
                j = i + 2  # Skip header + separator
                while j < len(lines) and lines[j].strip().startswith("|"):
                    row_line = lines[j].rstrip()
                    cells = [c.strip() for c in row_line.strip("|").split("|")]
                    cells_empty = sum(1 for c in cells if not c)
                    rows.append({
                        "cells_total": len(cells),
                        "cells_empty": cells_empty,
                    })
                    j += 1
                tables.append({
                    "start_line": start,
                    "end_line": j - 1,
                    "col_count": col_count,
                    "rows": rows,
                })
                i = j
                continue
        i += 1
    return tables


def _find_orphan_pipe_blocks(content: str, tables: list[dict]) -> list[int]:
    """Trouver les blocs de lignes pipes qui ne sont pas dans un tableau valide.

    Un « bloc orphelin » est une séquence de lignes consécutives commençant par
    '|' qui n'a pas de ligne de séparation '| --- |' — du coup, elle est
    sémantiquement du texte brut avec des pipes, pas un tableau.

    Retourne la liste des numéros de ligne (1-based) où chaque bloc orphelin
    commence.
    """
    lines = content.split("\n")
    covered = set()
    for tbl in tables:
        for k in range(tbl["start_line"], tbl["end_line"] + 1):
            covered.add(k)

    orphans = []
    in_block = False
    block_start = 0
    for i, line in enumerate(lines):
        is_orphan_pipe = line.strip().startswith("|") and i not in covered
        if is_orphan_pipe:
            if not in_block:
                in_block = True
                block_start = i + 1
        else:
            if in_block:
                orphans.append(block_start)
                in_block = False
    if in_block:
        orphans.append(block_start)
    return orphans


def detect_suspicious_transcriptions() -> list[dict]:
    """Scanner Transcriptions/Documents/ pour les transcriptions avec patterns suspects.

    Flags les fichiers où :
    - Un tableau contient >= 2 lignes dont > 50% des cellules sont vides
      (typique : une vraie ligne + des lignes clé-valeur fusionnées dans la même table)
    - Un tableau a plus de 6 colonnes (probabilité élevée de fusion erronée)
    - Des lignes pipes apparaissent sans séparateur `| --- |` au-dessus
      (« tableau orphelin » : rendu comme texte brut avec des pipes)

    Retourne une liste de dicts : [{path, rel, tables, score, reasons}].
    """
    suspects: list[dict] = []
    if not TRANSCRIPTIONS_DIR.exists():
        return suspects

    for trans in sorted(TRANSCRIPTIONS_DIR.rglob("*.md")):
        if trans.name.startswith("_") or "Attachments" in trans.parts:
            continue
        try:
            content = trans.read_text(encoding="utf-8")
        except OSError:
            continue
        tables = _parse_table_rows(content)
        orphan_starts = _find_orphan_pipe_blocks(content, tables)
        if not tables and not orphan_starts:
            continue

        reasons = []
        score = 0

        if orphan_starts:
            # Chaque bloc orphelin vaut 2 points — signal fort qu'un tableau
            # est mal rendu (lignes pipes non interprétées comme table par
            # markdown, rendu comme texte brut).
            score += 2 * len(orphan_starts)
            preview = ", ".join(f"L{s}" for s in orphan_starts[:5])
            more = f" et {len(orphan_starts) - 5} autres" if len(orphan_starts) > 5 else ""
            reasons.append(
                f"{len(orphan_starts)} bloc(s) de lignes pipes sans séparateur "
                f"(orphelin, {preview}{more})"
            )

        for tbl in tables:
            n_rows = len(tbl["rows"])
            # Lignes avec > 50% de cellules vides
            mostly_empty = [r for r in tbl["rows"]
                            if r["cells_total"] > 0
                            and r["cells_empty"] / r["cells_total"] > 0.5]
            n_non_empty = n_rows - len(mostly_empty)

            # Ne pas flagger les templates de formulaire entièrement vides :
            # le sous-mode fix-ocr du skill transcrire ne peut rien en faire (préservation stricte).
            if n_non_empty == 0:
                continue

            # Flagger le pattern de fusion clé-valeur : au moins 2 lignes mostly-empty
            # ET au moins 40% des lignes sont mostly-empty (sinon c'est probablement
            # des sous-lignes légitimes de type « continuation » ou « sous-total »).
            if len(mostly_empty) >= 2 and len(mostly_empty) / n_rows >= 0.4:
                score += len(mostly_empty)
                reasons.append(
                    f"Table de {tbl['col_count']} colonnes (L{tbl['start_line']+1}) : "
                    f"{len(mostly_empty)} lignes avec > 50% de cellules vides"
                )
            if tbl["col_count"] >= 7 and n_rows >= 3:
                # Tableau large avec plusieurs lignes : risque de fusion
                score += 1
                reasons.append(
                    f"Table large ({tbl['col_count']} colonnes, {n_rows} lignes) L{tbl['start_line']+1}"
                )

        if score >= 2:  # Seuil : au moins 2 points pour être flagué
            try:
                rel = str(trans.relative_to(BASE_PATH / "Connaissance"))
            except ValueError:
                rel = str(trans)
            suspects.append({
                "path": str(trans),
                "rel": rel,
                "score": score,
                "reasons": reasons,
                "tables_count": len(tables),
            })

    return suspects


def register_existing(db):
    """Enregistrer tous les documents déjà transcrits dans la DB."""
    if not TRANSCRIPTIONS_DIR.exists():
        print("Pas de transcriptions existantes.", file=sys.stderr)

        return 0

    extensions = Filtres().docs_config.get("extensions", [])
    count = 0
    for trans in sorted(TRANSCRIPTIONS_DIR.rglob("*.md")):
        if trans.name.startswith("_"):
            continue
        if "Attachments" in trans.parts:
            continue

        rel = trans.relative_to(TRANSCRIPTIONS_DIR)
        original = None
        for ext in extensions:
            candidate = DOCUMENTS_DIR / rel.with_suffix(ext)
            if candidate.exists():
                original = candidate
                break

        file_hash = hash_file(original) if original else None
        register_document(db, original or trans, trans, file_hash)
        count += 1

    return count


# --- API publique (appelée par le dispatcher CLI et les outils MCP) ---


def scan(since=None, until=None, output_file: str | None = None, db=None) -> dict:
    """Scanner ~/Documents/ et retourner les fichiers à transcrire (schema DocumentsScan).

    Si ``output_file`` est fourni, le payload complet (qui peut dépasser le Mo
    sur une base documentaire conséquente) est écrit dans ce fichier et
    seules des métadonnées sont renvoyées : ``{output_file, total_bytes,
    total_to_transcribe, total_skipped, skipped}``.
    """
    require_paths(DOCUMENTS_DIR, context="documents scan")

    if isinstance(since, str):
        since = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if isinstance(until, str):
        until = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    to_process, skipped = scan_documents(since, until, db=db)
    skipped_list = [{"reason": k, "count": v} for k, v in sorted(skipped.items())]
    payload = {
        "to_transcribe": to_process,
        "registered_existing": [],
        "skipped": skipped_list,
    }
    from connaissance.core.output_file import write_or_inline

    def _summary(p: dict) -> dict:
        items = p["to_transcribe"]
        # Répartition par année : on réutilise `_source_dates()` — exactement
        # la même logique que le filtre `--since`/`--until` côté
        # scan_documents (via filtres._check_date_file → birthtime/mtime du
        # filesystem, fallback date dans le nom de fichier). C'est aussi la
        # valeur qui se retrouve dans `created:` du frontmatter de
        # transcription. Garantit la cohérence : « by_year dit 9 en 2026 »
        # → `--since 2026-01-01 --until 2027-01-01` ramène exactement 9
        # fichiers.
        year_counts: dict[str, int] = {}
        for it in items:
            source = it.get("source")
            if not source:
                year_counts["inconnu"] = year_counts.get("inconnu", 0) + 1
                continue
            created, _ = _source_dates(Path(source))
            key = created[:4] if created and len(created) >= 4 and created[:4].isdigit() else "inconnu"
            year_counts[key] = year_counts.get(key, 0) + 1
        by_year = dict(sorted(year_counts.items()))
        # Échantillon de chemins (5 premiers) pour inspection rapide sans
        # ouvrir le fichier complet.
        sample = [it.get("rel") or it.get("source") for it in items[:5]]
        return {
            "total_to_transcribe": len(items),
            "total_skipped": sum(x["count"] for x in p["skipped"]),
            "skipped": p["skipped"],
            "by_year": by_year,
            "sample_to_transcribe": sample,
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)


def register(source_file: str, transcription: str, file_hash: str | None = None,
             db: TrackingDB | None = None) -> dict:
    """Enregistrer un document transcrit (frontmatter + DB)."""
    if db is None:
        db = TrackingDB()
    register_document(db, source_file, transcription, file_hash)
    return {
        "registered": 1,
        "source": str(source_file),
        "transcription": str(transcription),
        "frontmatter_injected": True,
    }


def register_existing_all(db: TrackingDB | None = None) -> dict:
    """Enregistrer tous les documents déjà transcrits (recovery)."""
    require_paths(DOCUMENTS_DIR, TRANSCRIPTIONS_DIR, context="documents register-existing")
    if db is None:
        db = TrackingDB()
    count = register_existing(db)
    return {"registered": count, "skipped": []}


def suspects() -> dict:
    """Détecter les transcriptions avec patterns de tableaux suspects (schema DocumentsSuspects)."""
    s = detect_suspicious_transcriptions()
    return {"count": len(s), "suspects": s}


# --- Vérification de préservation du contenu textuel ---


def _strip_frontmatter_for_verify(md: str) -> str:
    if not md.startswith("---"):
        return md
    end = md.find("\n---", 4)
    if end < 0:
        return md
    return md[end + 4:].lstrip("\n")


def tokenize_content(md: str) -> list[str]:
    """Tokeniser un markdown en retirant la syntaxe (tables, emphases, liens)."""
    md = _strip_frontmatter_for_verify(md)
    md = re.sub(r'\n\s*\|[\s\-|:]+\|\s*(?=\n|$)', '\n', md)
    md = md.replace('|', ' ')
    md = re.sub(r'^#{1,6}\s+', '', md, flags=re.MULTILINE)
    md = re.sub(r'\*\*([^*]+)\*\*', r'\1', md)
    md = re.sub(r'\*([^*]+)\*', r'\1', md)
    md = re.sub(r'__([^_]+)__', r'\1', md)
    md = re.sub(r'_([^_]+)_', r'\1', md)
    md = re.sub(r'^\s*[-*+]\s+', '', md, flags=re.MULTILINE)
    md = re.sub(r'^\s*\d+\.\s+', '', md, flags=re.MULTILINE)
    md = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', md)
    md = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', md)
    md = re.sub(r'^\s*>\s*', '', md, flags=re.MULTILINE)
    md = re.sub(r'```[^\n]*\n', '', md)
    md = md.replace('```', '').replace('`', '')
    tokens = re.findall(r"[\w']+", md.lower())
    tokens = [t.replace("'", "") for t in tokens if t.strip("'")]
    return tokens


def verify_preserve(before: str, after: str) -> dict:
    """Vérifier que le contenu textuel est préservé entre deux markdowns.

    `before` et `after` peuvent être des chemins de fichiers ou le contenu
    direct (détection automatique : si le path commence par ---, c'est du
    contenu ; sinon, tentative de lecture du fichier).
    """
    from collections import Counter

    def _read(source: str) -> str:
        p = Path(source)
        try:
            if p.exists() and p.is_file():
                return p.read_text(encoding="utf-8")
        except OSError:
            pass
        return source

    old_md = _read(before)
    new_md = _read(after)

    old_tokens = tokenize_content(old_md)
    new_tokens = tokenize_content(new_md)

    old_count = Counter(old_tokens)
    new_count = Counter(new_tokens)

    missing = old_count - new_count
    added = new_count - old_count

    return {
        "ok": len(missing) == 0 and len(added) == 0,
        "missing_tokens": list(missing.keys()),
        "added_tokens": list(added.keys()),
        "total_tokens_old": len(old_tokens),
        "total_tokens_new": len(new_tokens),
    }
