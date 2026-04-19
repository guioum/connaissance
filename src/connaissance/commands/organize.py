"""Module commands/organize : déplacement par entité depuis manifeste.

Expose :
- `plan() -> OrganizePlan` : génère un manifeste auto/alias_match/a_confirmer.
- `enrich(manifest_path, qmd_results) -> OrganizePlan` : injecte des candidats.
- `apply(manifest_path, dry_run=False) -> OrganizeApply` : applique un manifeste.
- `resolve(name=None, date=None, title=None, alias=None) -> OrganizeResolve`.
"""

import sys
import json
import re
import shutil
from pathlib import Path

import yaml

from connaissance.core.paths import BASE_PATH
from connaissance.core.tracking import TrackingDB
from connaissance.core.resolution import construire_slug, construire_nom_fichier, chercher_alias

CONNAISSANCE = BASE_PATH / "Connaissance"
TRANSCRIPTIONS = CONNAISSANCE / "Transcriptions"
RESUMES = CONNAISSANCE / "Résumés"
DOCUMENTS_DIR = BASE_PATH / "Documents"

# Dossiers à ne jamais supprimer même s'ils sont vides
PROTECTED_ROOTS = {
    DOCUMENTS_DIR,
    TRANSCRIPTIONS / "Documents",
    TRANSCRIPTIONS / "Courriels",
    TRANSCRIPTIONS / "Notes",
    RESUMES / "Documents",
    RESUMES / "Courriels",
    RESUMES / "Notes",
    CONNAISSANCE,
}


def _cleanup_empty_parents(path):
    """Remonter et supprimer les dossiers parents devenus vides.

    Supprime aussi les .DS_Store orphelins et les dossiers Attachments/ vides.
    S'arrête aux PROTECTED_ROOTS (Transcriptions/Courriels/, etc.).
    """
    parent = path.parent
    while parent and parent not in PROTECTED_ROOTS:
        try:
            # Contenu réel (sans .DS_Store)
            children = [c for c in parent.iterdir() if c.name != ".DS_Store"]
            # Vérifier si les children sont tous des dossiers vides (ex: Attachments/)
            non_empty = False
            for c in children:
                if c.is_file():
                    non_empty = True
                    break
                if c.is_dir():
                    sub = [s for s in c.iterdir() if s.name != ".DS_Store"]
                    if sub:
                        non_empty = True
                        break
                    else:
                        # Dossier vide → supprimer
                        ds = c / ".DS_Store"
                        if ds.exists():
                            ds.unlink()
                        c.rmdir()
            if non_empty:
                break
            ds = parent / ".DS_Store"
            if ds.exists():
                ds.unlink()
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


_ATT_REF_PATTERN = re.compile(r'\(\.?/?Attachments/([^)]+)\)')


def _extract_attachment_filenames(md_path):
    """Lire un .md et extraire les noms de fichiers Attachments/ référencés."""
    filenames = set()
    try:
        content = md_path.read_text(encoding="utf-8")
        for match in _ATT_REF_PATTERN.findall(content):
            filenames.add(match)
    except OSError:
        pass
    return filenames


def _move_with_attachments(src, dst, source_type="documents"):
    """Déplacer un fichier .md et ses attachements référencés.

    Uniforme pour les trois sources (documents, courriels, notes) :
    ``shutil.move`` pour éviter la duplication. Si un autre .md du dossier
    source référence encore un attachement, on le laisse en place (move
    conditionnel sur « plus aucun référent restant »).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    if src.exists() and not dst.exists():
        shutil.move(str(src), str(dst))

        # Annotations (documents uniquement)
        ann_src = src.with_name(src.stem + "_annotations.json")
        ann_dst = dst.with_name(dst.stem + "_annotations.json")
        if ann_src.exists() and not ann_dst.exists():
            shutil.move(str(ann_src), str(ann_dst))

        att_src_dir = src.parent / "Attachments"
        att_dst_dir = dst.parent / "Attachments"

        filenames = _extract_attachment_filenames(dst)
        if filenames and att_src_dir.is_dir():
            # Quels autres .md du dossier source référencent encore ces
            # attachements ? Si un autre référent subsiste, on copie
            # (attachement partagé). Sinon on déplace.
            remaining_refs: set[str] = set()
            for other_md in att_src_dir.parent.glob("*.md"):
                if other_md == src or other_md == dst:
                    continue
                remaining_refs |= _extract_attachment_filenames(other_md)

            att_dst_dir.mkdir(parents=True, exist_ok=True)
            for fname in filenames:
                src_file = att_src_dir / fname
                dst_file = att_dst_dir / fname
                if not src_file.exists() or dst_file.exists():
                    continue
                if fname in remaining_refs:
                    shutil.copy2(str(src_file), str(dst_file))
                else:
                    shutil.move(str(src_file), str(dst_file))

        _cleanup_empty_parents(src)
        return True
    return False


def _find_original_document(resume_rel, ext_candidates=None):
    """Trouver le document original dans ~/Documents/ à partir du chemin
    relatif du résumé (miroir de la transcription, miroir de ~/Documents/).

    resume_rel : chemin relatif depuis Résumés/Documents/
    Ex: admin/facture.md → ~/Documents/admin/facture.pdf
    """
    if ext_candidates is None:
        ext_candidates = [".pdf", ".png", ".jpg", ".jpeg", ".heic", ".webp",
                          ".tiff", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"]

    for ext in ext_candidates:
        candidate = DOCUMENTS_DIR / resume_rel.with_suffix(ext)
        if candidate.exists():
            return candidate
    return None


def _apply_manifest(manifest_path, dry_run=False) -> dict:
    """Appliquer un manifeste d'organisation par entité.

    Accepte deux formats :
    - Tableau direct : `[{source, resume_path, ...}, ...]`
    - Enveloppe produite par `--generer-manifeste` :
      `{total, auto, alias_match, a_confirmer, entrees: [...]}`
    """
    empty_result = {"moved": 0, "skipped": 0, "errors": 0}
    raw = json.loads(Path(manifest_path).read_text())
    if isinstance(raw, dict) and "entrees" in raw:
        entries = raw["entrees"] or []
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError(
            "Format de manifeste non reconnu : attendu une liste "
            "ou un dict avec clé 'entrees'."
        )

    if not entries:
        print("Manifeste vide, rien à faire.", file=sys.stderr)
        return empty_result

    db = TrackingDB()
    try:
        return _apply_manifest_impl(entries, dry_run, db)
    finally:
        db.close()


def _apply_manifest_impl(entries: list, dry_run: bool, db: TrackingDB) -> dict:
    print(f"\n{'='*60}", file=sys.stderr)

    print(f"  Organisation de {len(entries)} fichiers par entité", file=sys.stderr)

    if dry_run:
        print(f"  MODE SIMULATION — aucun fichier ne sera déplacé", file=sys.stderr)

    print(f"{'='*60}\n", file=sys.stderr)


    # Décompte par source
    by_source = {}
    for e in entries:
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1
    for src, count in sorted(by_source.items()):
        print(f"  {src:12s} : {count}", file=sys.stderr)

    print(file=sys.stderr)


    moved = 0
    skipped = 0
    errors = 0

    for entry in entries:
        source = entry["source"]
        resume_path = Path(entry["resume_path"])
        entity_type = entry["entity_type"]
        entity_slug = entry["entity_slug"]
        new_name = entry["new_name"]  # sans extension, ex: "2025-09-01 avis-cotisation"
        confidence = entry.get("confidence", "high")

        # Guard : routage incomplet → erreur, pas de crash. Un plan sain ne
        # produit plus ce cas (cf. `construire_manifeste` qui exige entity_slug
        # non vide pour status=auto), mais un manifeste patché à la main
        # peut toujours arriver ici.
        if not entity_type or not entity_slug or not new_name:
            print(
                f"  ✗ Entrée sans routage complet "
                f"(entity_type={entity_type!r}, entity_slug={entity_slug!r}, "
                f"new_name={new_name!r}) : {resume_path}",
                file=sys.stderr,
            )
            errors += 1
            continue

        # Calculer le chemin relatif du résumé par rapport à Résumés/{Source}/
        source_label = source.capitalize()
        try:
            resume_rel = resume_path.relative_to(RESUMES / source_label)
        except ValueError:
            # Le chemin est peut-être absolu
            if resume_path.is_absolute():
                try:
                    resume_rel = resume_path.relative_to(RESUMES / source_label)
                except ValueError:
                    print(f"  ✗ Chemin invalide : {resume_path}", file=sys.stderr)

                    errors += 1
                    continue
            else:
                resume_rel = Path(entry.get("resume_rel", str(resume_path)))

        # Destinations (structure entité)
        dest_resume = RESUMES / source_label / entity_type / entity_slug / f"{new_name}.md"
        dest_trans = TRANSCRIPTIONS / source_label / entity_type / entity_slug / f"{new_name}.md"

        # Affichage
        label = f"  [{confidence}] " if confidence == "low" else "  "
        print(f"{label}{source} : {resume_rel}", file=sys.stderr)

        print(f"    → {entity_type}/{entity_slug}/{new_name}", file=sys.stderr)


        if dest_resume.exists():
            print(f"    ○ Destination résumé existe déjà, ignoré", file=sys.stderr)

            skipped += 1
            continue

        if dry_run:
            moved += 1
            continue

        try:
            # 1. Déplacer le résumé (pas d'attachements dans les résumés miroir)
            _move_with_attachments(resume_path, dest_resume, source)

            # 2. Déplacer la transcription + ses attachements UUID
            trans_path = TRANSCRIPTIONS / source_label / resume_rel
            _move_with_attachments(trans_path, dest_trans, source)

            # 3. Pour les documents : déplacer aussi l'original dans ~/Documents/
            if source == "documents":
                original = _find_original_document(resume_rel)
                if original:
                    dest_original = DOCUMENTS_DIR / entity_type / entity_slug / f"{new_name}{original.suffix}"
                    dest_original.parent.mkdir(parents=True, exist_ok=True)
                    if not dest_original.exists():
                        shutil.move(str(original), str(dest_original))
                        _cleanup_empty_parents(original)

            # 4. Pour les courriels (fils) : déplacer toutes les transcriptions du fil
            if source == "courriels" and "message_ids" in entry:
                for mid_hash in entry.get("other_hashes", []):
                    other_trans = trans_path.parent / f"{mid_hash}.md"
                    other_dest = dest_trans.parent / f"{mid_hash}.md"
                    _move_with_attachments(other_trans, other_dest, "courriels")

            moved += 1

            # Mettre à jour le champ source: dans le résumé (pointe vers la transcription déplacée)
            try:
                new_trans_rel = str(dest_trans.relative_to(CONNAISSANCE))
                content = dest_resume.read_text(encoding="utf-8")
                if "source:" in content:
                    content = re.sub(r'^source: .*$', f'source: {new_trans_rel}',
                                     content, count=1, flags=re.MULTILINE)
                    dest_resume.write_text(content, encoding="utf-8")
            except Exception:
                pass

            # Tracking — mettre à jour résumé ET transcription dans la DB
            try:
                old_resume_rel = str(resume_path.relative_to(CONNAISSANCE))
                new_resume_rel = str(dest_resume.relative_to(CONNAISSANCE))
                db.move_file(old_resume_rel, new_resume_rel, entity_type, entity_slug)

                old_trans_rel = str(trans_path.relative_to(CONNAISSANCE))
                new_trans_rel = str(dest_trans.relative_to(CONNAISSANCE))
                db.move_file(old_trans_rel, new_trans_rel, entity_type, entity_slug)

                db.log("connaissance", "organize",
                       source_type=source,
                       source_path=old_resume_rel,
                       dest_path=new_resume_rel,
                       details={"entity_type": entity_type,
                                "entity_slug": entity_slug,
                                "new_name": new_name,
                                "confidence": confidence})
            except Exception:
                pass

        except Exception as e:
            print(f"    ✗ Erreur : {e}", file=sys.stderr)

            errors += 1

    print(f"\n  {'─'*40}", file=sys.stderr)

    action = "à déplacer" if dry_run else "déplacés"
    print(f"  ✓ {moved} {action}", file=sys.stderr)

    if skipped:
        print(f"  ○ {skipped} ignorés", file=sys.stderr)

    if errors:
        print(f"  ✗ {errors} erreurs", file=sys.stderr)

    return {"moved": moved, "skipped": skipped, "errors": errors}


def generer_manifeste():
    """Lire les résumés non organisés et pré-remplir un manifeste JSON.

    Pour chaque résumé, extrait le frontmatter, construit le new_name via
    _resolution.py, et vérifie les aliases pour les confidence: low.

    Chaque entrée a un champ 'status' :
    - "auto" : confidence high, accepté directement
    - "alias_match" : confidence low mais alias trouvé
    - "a_confirmer" : confidence low, pas de match alias
    """
    # Trouver les résumés non organisés
    entity_dirs = {"personnes", "organismes", "divers", "inconnus"}
    manifeste = []

    for source_label in ("Documents", "Courriels", "Notes"):
        source_dir = RESUMES / source_label
        if not source_dir.exists():
            continue
        for md_file in source_dir.rglob("*.md"):
            # Vérifier si déjà dans un dossier entité
            rel = md_file.relative_to(source_dir)
            parts = rel.parts
            if len(parts) >= 2 and parts[0] in entity_dirs:
                continue  # Déjà organisé

            # Lire le frontmatter
            try:
                content = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not content.startswith("---"):
                continue
            try:
                fm_text = content.split("---", 2)[1]
                fm = yaml.safe_load(fm_text)
            except (IndexError, yaml.YAMLError, ValueError):
                continue
            if not fm or not isinstance(fm, dict):
                continue

            entity_type = fm.get("entity_type", "inconnus")
            entity_slug = fm.get("entity_slug", "")
            entity_name = fm.get("entity_name", "")
            confidence = fm.get("confidence", "low")
            date_val = str(fm.get("date", "")) if fm.get("date") else ""
            title = fm.get("title", "")

            # Construire le new_name
            if date_val and title:
                new_name = construire_nom_fichier(date_val, title)
            else:
                new_name = md_file.stem

            # Déterminer le statut. "auto" exige que TOUS les champs de
            # routage soient présents : sinon `apply` planterait au
            # `RESUMES / entity_type / entity_slug / ...` (Path ne concatène
            # pas None/"").
            if (confidence == "high"
                    and entity_type
                    and entity_type != "inconnus"
                    and entity_slug):
                status = "auto"
            else:
                # Chercher un alias
                from_field = fm.get("from", "")
                identifiants = [entity_name, from_field] if from_field else [entity_name]
                alias_found = None
                for ident in identifiants:
                    if ident:
                        alias_found = chercher_alias(ident)
                        if alias_found:
                            break
                if alias_found:
                    # L'alias résout l'entité
                    parts = alias_found.split("/", 1)
                    if len(parts) == 2:
                        entity_type = parts[0]
                        entity_slug = parts[1]
                    status = "alias_match"
                else:
                    status = "a_confirmer"

            manifeste.append({
                "source": source_label.lower(),
                "resume_path": str(md_file),
                "entity_type": entity_type,
                "entity_slug": entity_slug,
                "entity_name": entity_name,
                "new_name": new_name,
                "confidence": confidence,
                "status": status,
            })

    return manifeste


# --- API publique ---


def plan() -> dict:
    """Générer un manifeste d'organisation (schema OrganizePlan).

    Écrit le manifeste dans ~/Connaissance/.config/organize-manifest.json
    et retourne la structure en mémoire.
    """
    from connaissance.core.paths import CONNAISSANCE_ROOT
    entries = generer_manifeste()
    auto = sum(1 for e in entries if e["status"] == "auto")
    alias = sum(1 for e in entries if e["status"] == "alias_match")
    confirmer = sum(1 for e in entries if e["status"] == "a_confirmer")

    manifest_path = CONNAISSANCE_ROOT / ".config" / "organize-manifest.json"
    manifest_path.parent.mkdir(parents=False, exist_ok=True)

    envelope = {
        "total": len(entries),
        "auto": auto,
        "alias_match": alias,
        "a_confirmer": confirmer,
        "entrees": entries,
    }
    manifest_path.write_text(
        json.dumps(envelope, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "total": len(entries),
        "auto": auto,
        "alias_match": alias,
        "a_confirmer": confirmer,
        "manifest_path": str(manifest_path),
        "entries": entries,
    }


def enrich(manifest_path: str, qmd_results: list[dict]) -> dict:
    """Injecter des candidats qmd dans les entrées `a_confirmer` d'un manifeste.

    `qmd_results` est une liste de `{id, candidates: [...]}` où `id` est
    le `resume_path` de l'entrée à enrichir. Écrit le manifeste mis à jour
    sur place et retourne la structure.
    """
    path = Path(manifest_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        entries = data.get("entrees") or []
    else:
        entries = data or []

    candidates_by_id = {item["id"]: item.get("candidates", []) for item in qmd_results}

    for entry in entries:
        key = entry.get("resume_path") or entry.get("id")
        if key in candidates_by_id and entry.get("status") == "a_confirmer":
            entry["qmd_candidates"] = candidates_by_id[key]

    if isinstance(data, dict):
        data["entrees"] = entries
    else:
        data = entries

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "manifest_path": str(path),
        "enriched": sum(1 for e in entries if "qmd_candidates" in e),
        "total": len(entries),
    }


def apply(manifest: str, dry_run: bool = False) -> dict:
    """Appliquer un manifeste (schema OrganizeApply)."""
    result = _apply_manifest(manifest, dry_run=dry_run)
    return {
        "moved": result.get("moved", 0),
        "skipped": result.get("skipped", 0),
        "errors": result.get("errors", 0),
        "manifest": str(manifest),
        "dry_run": dry_run,
    }


def resolve(name: str | None = None, date: str | None = None,
            title: str | None = None, alias: str | None = None) -> dict:
    """Résoudre un nom/date/alias vers slug/filename/type-slug (schema OrganizeResolve)."""
    result: dict = {"slug": "", "filename": "", "alias_match": None}
    if name:
        result["slug"] = construire_slug(name)
    if date and title:
        result["filename"] = construire_nom_fichier(date, title)
    if alias:
        result["alias_match"] = chercher_alias(alias)
    return result
