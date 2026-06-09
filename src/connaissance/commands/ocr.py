"""OCR LOCAL (macOS Vision) — première passe gratuite de transcription.

Produit des transcriptions sous ``Transcriptions/Documents/`` marquées
``ocr_engine: vision-local`` + ``ocr_confidence``. Les autres étapes
(``signals``/``classify``/``summarize``) les lisent comme n'importe quelle
transcription. La **repasse Mistral** est gardée en option : on identifie les
transcriptions à faible confiance et on les remet « à transcrire » (le flux
Mistral existant les reprend alors, en écrasant la version locale).
"""
from __future__ import annotations

import os
import statistics
import unicodedata
from pathlib import Path

import yaml

from connaissance.core import ledger as _ledger
from connaissance.core import ocr_local as _ocr
from connaissance.commands.documents import (TRANSCRIPTIONS_DIR,
                                             _merge_frontmatter, register_document)
from connaissance.commands.triage import (BUNDLE_SUFFIXES, CODE_MARKERS,
                                          MARKER_DIRS)
from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import (DOCUMENTS_DIR, documents_read_path,
                                     require_paths)
from connaissance.core.tracking import TrackingDB

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
             ".webp", ".gif", ".bmp"}
_VIEW_TOP = {"- Sujets", "- Par catégorie", "- Historique", "- Médias"}


def _read_frontmatter(content: str) -> dict:
    """Frontmatter YAML d'une transcription (dict vide si absent/invalide)."""
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 4)
    if end < 0:
        return {}
    try:
        fm = yaml.safe_load(content[4:end])
        return fm if isinstance(fm, dict) else {}
    except yaml.YAMLError:
        return {}


def ocr_local(limit: int | None = None, force: bool = False,
              scope: str | None = None, db: TrackingDB | None = None) -> dict:
    """OCR local des PDF scannés (texte born-digital absent) sans transcription.

    Écrit une transcription marquée ``vision-local`` + confiance, et l'enregistre.
    Idempotent (saute si transcription présente, sauf ``--force``). ``--limit``
    pour un lot, ``--scope`` pour un sous-dossier."""
    require_paths(DOCUMENTS_DIR, context="documents ocr-local")
    if not _ocr.available():
        return {"error": "OCR local indisponible (swiftc absent ou hors macOS)."}
    owns = db is None
    if db is None:
        db = TrackingDB()
    done: list[dict] = []
    skipped = {"transcription_existe": 0, "sans_miroir": 0, "echec_ou_vide": 0}
    try:
        for rel, pkt in db.all_doc_signals():
            if pkt.get("type") != "pdf" or pkt.get("text_source") != "none":
                continue
            if scope and not rel.startswith(scope):
                continue
            trans = TRANSCRIPTIONS_DIR / Path(rel).with_suffix(".md")
            if trans.exists() and not force:
                skipped["transcription_existe"] += 1
                continue
            ab = DOCUMENTS_DIR / rel
            rp = documents_read_path(ab)
            if not rp or not Path(rp).is_file():
                skipped["sans_miroir"] += 1
                continue
            res = _ocr.ocr_file(rp) or {}
            text = res.get("text", "").strip()
            if not text:
                skipped["echec_ou_vide"] += 1
                continue
            conf = round(float(res.get("confidence") or 0), 3)
            trans.parent.mkdir(parents=True, exist_ok=True)
            trans.write_text(text + "\n", encoding="utf-8")
            register_document(db, ab, trans)        # frontmatter canonique + DB
            # Provenance (extras préservés par _merge_frontmatter).
            trans.write_text(_merge_frontmatter(
                trans.read_text(encoding="utf-8"),
                {"ocr_engine": _ocr.OCR_ENGINE, "ocr_confidence": conf}),
                encoding="utf-8")
            done.append({"rel": rel, "confidence": conf,
                         "chars": len(text), "pages": res.get("pages")})
            if limit and len(done) >= limit:
                break
    finally:
        if owns:
            db.close()
    confs = [d["confidence"] for d in done]
    return {"ocr_local": len(done), "skipped": skipped, "engine": _ocr.OCR_ENGINE,
            "avg_confidence": round(statistics.mean(confs), 3) if confs else None,
            "sample": done[:10]}


def _vision_transcriptions(max_confidence: float | None):
    """Transcriptions produites par l'OCR local, filtrées par confiance max."""
    out = []
    for f in TRANSCRIPTIONS_DIR.rglob("*.md"):
        try:
            fm = _read_frontmatter(f.read_text(encoding="utf-8"))
        except OSError:
            continue
        if (fm or {}).get("ocr_engine") != _ocr.OCR_ENGINE:
            continue
        c = fm.get("ocr_confidence")
        c = float(c) if c is not None else 0.0
        if max_confidence is None or c <= max_confidence:
            out.append((f, c, str(fm.get("source") or "")))
    out.sort(key=lambda t: t[1])
    return out


def repass_candidates(max_confidence: float = 0.6,
                      db: TrackingDB | None = None) -> dict:
    """Lister les transcriptions OCR local à **faible confiance** (≤ seuil) —
    candidates à une repasse Mistral. N'écrit/ne déplace rien."""
    items = _vision_transcriptions(max_confidence)
    return {"max_confidence": max_confidence, "total": len(items),
            "candidates": [{"transcription": str(f.relative_to(TRANSCRIPTIONS_DIR)),
                            "confidence": c, "source": src}
                           for f, c, src in items[:200]]}


def repass(max_confidence: float = 0.6, apply: bool = False,
           db: TrackingDB | None = None) -> dict:
    """Mettre les transcriptions OCR local faibles « à retranscrire » : les
    envoie à la corbeille ledger (réversible) → elles redeviennent manquantes,
    le flux Mistral les reprendra. **Dry-run par défaut** (``--apply``)."""
    require_paths(DOCUMENTS_DIR, context="documents ocr-local repass")
    owns = db is None
    if db is None:
        db = TrackingDB()
    items = _vision_transcriptions(max_confidence)
    moved = []
    try:
        if apply and items:
            run_id = _ledger.new_run_id("ocr-repass")
            for f, c, src in items:
                with db.transaction():
                    _ledger.safe_trash(db, f, f"ocr-repass conf={c}", run_id, commit=False)
                moved.append(str(f.relative_to(TRANSCRIPTIONS_DIR)))
            return {"apply": True, "trashed": len(moved), "ledger_run": run_id,
                    "max_confidence": max_confidence}
        return {"apply": False, "would_trash": len(items),
                "max_confidence": max_confidence,
                "sample": [str(f.relative_to(TRANSCRIPTIONS_DIR)) for f, _, _ in items[:10]]}
    finally:
        if owns:
            db.close()


_MISTRAL_PAGE_COST = 0.001   # $1 / 1000 pages (Mistral OCR, batch).


def transcribe_plan(max_pages: int = 10, include_missing: bool = True,
                    scope: str | None = None, output_file: str | None = None,
                    db: TrackingDB | None = None) -> dict:
    """Worklist de la **repasse Mistral**, bornée par le nombre de pages (coût).

    Cible : documents qui ont besoin d'OCR (PDF scannés, images-documents) et qui
    n'ont PAS encore de transcription Mistral — soit une transcription
    ``vision-local`` à *upgrader* vers le markdown structuré de Mistral, soit
    (``include_missing``) un scanné sans aucune transcription. Les PDF
    **born-digital** (couche texte propre) sont exclus et comptés
    (``born_digital_skip``) : pas de coût OCR inutile. Borne ``--max-pages`` : un
    document de plus de N pages part dans ``deferred`` (au-dessus du budget).

    Issu de la DB (``doc_signals``). **Déduplique** les lignes-fantômes
    (variantes NFC/NFD/casse du même fichier sur un FS insensible à la casse) par
    ``rel`` normalisé → un fichier physique = une entrée. Chaque entrée porte le
    format de manifeste ``documents scan`` (consommable tel quel par
    ``mistral-ocr --files-from-json`` ET ``documents register-batch``) :
    ``source`` (canonique, pour le frontmatter + le miroir ``--preserve-paths``),
    ``read_source`` (miroir SSD, lu par l'OCR sans download iCloud),
    ``transcription`` (chemin cible sous ``Transcriptions/Documents``).

    N'écrit/ne déplace rien (sauf le manifeste si ``--output-file``). Flux :
    ``transcribe-plan --output-file`` → ``mistral-ocr ocr_batch_submit
    files_from_json=…`` → ``mistral-ocr ocr_batch_results output=Transcriptions``
    → ``documents register-batch --ocr-engine mistral``.

    Prérequis : signaux à jour (``documents signals`` en v≥6) pour ``pages`` +
    les transcriptions Vision existantes en ``text_source=ocr_cache``."""
    require_paths(DOCUMENTS_DIR, context="documents transcribe-plan")
    owns = db is None
    if db is None:
        db = TrackingDB()
    worklist: list[dict] = []
    deferred: list[dict] = []
    seen: set[str] = set()          # rel normalisé → dédup des lignes-fantômes
    counts = {"upgrade_vision": 0, "missing": 0, "already_mistral": 0,
              "born_digital_skip": 0, "deferred_pages": 0, "phantom_dupes": 0}
    try:
        for rel, pkt in db.all_doc_signals():
            if scope and not rel.startswith(scope):
                continue
            typ = pkt.get("type")
            ts = pkt.get("text_source")
            born = pkt.get("born_digital")
            is_pdf = typ == "pdf"
            # Cible OCR : transcription Vision déjà là (ocr_cache, couvre PDF
            # scannés ET images-documents) OU PDF scanné encore sans transcription.
            ocr_target = (ts == "ocr_cache") or (is_pdf and born is False)
            if not ocr_target:
                if is_pdf and born is True:
                    counts["born_digital_skip"] += 1
                continue
            key = unicodedata.normalize("NFC", rel).lower()
            if key in seen:
                counts["phantom_dupes"] += 1
                continue
            seen.add(key)
            trans = TRANSCRIPTIONS_DIR / Path(rel).with_suffix(".md")
            engine = None
            if trans.exists():
                try:
                    engine = _read_frontmatter(
                        trans.read_text(encoding="utf-8")).get("ocr_engine")
                except OSError:
                    engine = None
            if engine == "mistral":
                counts["already_mistral"] += 1
                continue
            if not trans.exists() and not include_missing:
                continue
            pages = pkt.get("pages")
            n = pages if isinstance(pages, int) and pages > 0 else 1
            reason = "upgrade_vision" if engine == _ocr.OCR_ENGINE else "missing"
            src = DOCUMENTS_DIR / rel
            entry = {"rel": rel,
                     "source": str(src),                       # canonique
                     "read_source": str(documents_read_path(src)),  # miroir SSD
                     "transcription": str(trans),
                     "pages": n, "reason": reason, "current_engine": engine}
            if n > max_pages:
                counts["deferred_pages"] += 1
                deferred.append(entry)
                continue
            counts[reason] += 1
            worklist.append(entry)
    finally:
        if owns:
            db.close()
    worklist.sort(key=lambda e: e["pages"])
    est_pages = sum(e["pages"] for e in worklist)
    payload = {"max_pages": max_pages,
               "worklist_count": len(worklist),
               "deferred_count": len(deferred),
               "estimated_pages": est_pages,
               "estimated_cost_usd": round(est_pages * _MISTRAL_PAGE_COST, 2),
               "counts": counts,
               # `to_transcribe` : clé consommée par mistral-ocr / register-batch.
               # `worklist` : alias conservé pour les appelants existants.
               "to_transcribe": worklist,
               "worklist": worklist,
               "deferred": deferred}

    def _summary(p: dict) -> dict:
        keep = ("max_pages", "worklist_count", "deferred_count",
                "estimated_pages", "estimated_cost_usd", "counts")
        return {**{k: p[k] for k in keep},
                "sample": [e["rel"] for e in p["to_transcribe"][:10]]}

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)


def ocr_images(limit: int | None = None, min_chars: int = 100, min_lines: int = 3,
               scope: str | None = None, force: bool = False,
               db: TrackingDB | None = None) -> dict:
    """Détecter + OCRiser les IMAGES qui sont des DOCUMENTS (reçus, captures,
    scans, **photos de documents prises à l'appareil**). La passe Vision sert de
    détecteur : densité de texte (``chars`` ≥ ``min_chars`` ET ≥ ``min_lines``
    lignes) → document (transcription écrite, marquée ``ocr_kind: image``) ;
    sinon → photo/déco (ignorée, comptée). **Aucune exclusion par EXIF** : une
    photo d'appareil peut être un document scanné. Conteneurs code/bundles élagués
    (pas où vivent les scans perso). ``--limit`` borne les documents écrits."""
    require_paths(DOCUMENTS_DIR, context="documents ocr-images")
    if not _ocr.available():
        return {"error": "OCR local indisponible (swiftc absent ou hors macOS)."}
    owns = db is None
    if db is None:
        db = TrackingDB()
    docs: list[dict] = []
    non_doc = 0
    borderline: list[dict] = []
    skipped = {"transcription_existe": 0, "sans_miroir": 0}
    base = (DOCUMENTS_DIR / scope) if scope else DOCUMENTS_DIR
    stop = False
    try:
        for dp, dirs, fs in os.walk(base):
            if stop:
                break
            d = Path(dp)
            if d == DOCUMENTS_DIR:
                dirs[:] = [n for n in dirs if n not in _VIEW_TOP]
            if (d.suffix.lower() in BUNDLE_SUFFIXES
                    or (set(fs) & CODE_MARKERS) or (set(dirs) & MARKER_DIRS)):
                dirs[:] = []
                continue
            for fn in fs:
                if fn.startswith(".") or Path(fn).suffix.lower() not in _IMG_EXTS:
                    continue
                rel = str((d / fn).relative_to(DOCUMENTS_DIR))
                trans = TRANSCRIPTIONS_DIR / Path(rel).with_suffix(".md")
                if trans.exists() and not force:
                    skipped["transcription_existe"] += 1
                    continue
                ab = DOCUMENTS_DIR / rel
                rp = documents_read_path(ab)
                if not rp or not Path(rp).is_file():
                    skipped["sans_miroir"] += 1
                    continue
                res = _ocr.ocr_file(rp, max_pages=1)
                text = (res or {}).get("text", "").strip() if res else ""
                lines = text.count("\n") + 1 if text else 0
                conf = round(float((res or {}).get("confidence") or 0), 3)
                if len(text) >= min_chars and lines >= min_lines:
                    trans.parent.mkdir(parents=True, exist_ok=True)
                    trans.write_text(text + "\n", encoding="utf-8")
                    register_document(db, ab, trans)
                    trans.write_text(_merge_frontmatter(
                        trans.read_text(encoding="utf-8"),
                        {"ocr_engine": _ocr.OCR_ENGINE, "ocr_confidence": conf,
                         "ocr_kind": "image"}), encoding="utf-8")
                    docs.append({"rel": rel, "chars": len(text), "confidence": conf})
                    if limit and len(docs) >= limit:
                        stop = True
                        break
                else:
                    non_doc += 1
                    if 0 < len(text) < min_chars * 2:   # proche du seuil → à revoir
                        borderline.append({"rel": rel, "chars": len(text), "conf": conf})
    finally:
        if owns:
            db.close()
    confs = [d["confidence"] for d in docs]
    return {"documents_images": len(docs), "non_documents": non_doc,
            "skipped": skipped, "min_chars": min_chars, "min_lines": min_lines,
            "avg_confidence": round(statistics.mean(confs), 3) if confs else None,
            "sample_documents": docs[:12], "sample_borderline": borderline[:12]}
