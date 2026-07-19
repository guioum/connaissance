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

import json


from connaissance.core import filtres as _filtres
from connaissance.core import ledger as _ledger
from connaissance.core import ocr_local as _ocr
from connaissance.commands.documents import (TRANSCRIPTIONS_DIR,
                                             _merge_frontmatter, register_document)
from connaissance.commands.triage import (BUNDLE_SUFFIXES, CODE_MARKERS,
                                          MARKER_DIRS)
from connaissance.core.frontmatter import parse_frontmatter
from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import (DOCUMENTS_DIR, SPECIAL_TOP_DIRS,
                                     documents_read_path, require_paths)
from connaissance.core.tracking import TrackingDB

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
             ".webp", ".gif", ".bmp"}
_VIEW_TOP = set(SPECIAL_TOP_DIRS)   # source unique (était une liste divergente)


def _read_frontmatter(content: str) -> dict:
    """Frontmatter YAML d'une transcription (dict vide si absent/invalide)."""
    return parse_frontmatter(content) or {}


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


def review_candidates(max_confidence: float = 0.85,
                      engine: str | None = "mistral",
                      output_file: str | None = None,
                      db: TrackingDB | None = None) -> dict:
    """Lister les transcriptions à **confiance basse** (≤ seuil) pour REVUE.

    Flag qualité OCR : par défaut les transcriptions **Mistral** (moteur
    terminal — pas de repasse au-delà, mais un ``ocr_confidence`` bas signale un
    OCR douteux à vérifier *avant* de s'y fier en classement/résumé). Lit
    ``ocr_confidence`` (le minimum des pages) du frontmatter ; les transcriptions
    sans score (p.ex. demandées sans ``confidence_scores``) sont ignorées.
    ``engine=None`` → tous moteurs. N'écrit/ne déplace rien.

    Sortie **compacte par défaut** (total + distribution par tranche + par
    dossier + top-20 des pires) pour rester lisible même à plusieurs centaines de
    candidats. ``output_file`` écrit la **liste complète** sur disque."""
    out: list[dict] = []
    for f in TRANSCRIPTIONS_DIR.rglob("*.md"):
        try:
            fm = _read_frontmatter(f.read_text(encoding="utf-8")) or {}
        except OSError:
            continue
        if engine and fm.get("ocr_engine") != engine:
            continue
        c = fm.get("ocr_confidence")
        if c is None:
            continue
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c <= max_confidence:
            out.append({"transcription": str(f.relative_to(TRANSCRIPTIONS_DIR)),
                        "confidence": c,
                        "confidence_avg": fm.get("ocr_confidence_avg"),
                        "source": str(fm.get("source") or "")})
    out.sort(key=lambda d: d["confidence"])

    # Agrégats pour une lecture d'un coup d'œil (où ça se concentre, les pires).
    buckets = {"≤0.3": 0, "0.3–0.5": 0, "0.5–0.7": 0, "0.7+": 0}
    by_folder: dict[str, int] = {}
    for d in out:
        c = d["confidence"]
        key = ("≤0.3" if c <= 0.3 else "0.3–0.5" if c <= 0.5
               else "0.5–0.7" if c <= 0.7 else "0.7+")
        buckets[key] += 1
        folder = "/".join(d["transcription"].split("/")[:2])
        by_folder[folder] = by_folder.get(folder, 0) + 1
    top_folders = dict(sorted(by_folder.items(), key=lambda kv: -kv[1])[:10])

    # Toujours compact inline (la liste brute dépasse vite la limite de tokens) ;
    # la liste complète ne part QUE dans output_file si demandé.
    summary = {"engine": engine, "max_confidence": max_confidence,
               "total": len(out), "by_confidence": buckets,
               "by_folder": top_folders, "top_worst": out[:20]}
    if output_file:
        p = Path(output_file).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**summary, "candidates": out},
                                ensure_ascii=False), encoding="utf-8")
        summary["output_file"] = str(p)
    return summary


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


# $2 / 1000 pages : Mistral OCR 4 en batch (migration 2026-07-19 ; OCR 3
# était à $1/1000). Doit suivre le modèle épinglé dans mistral-ocr/cli.py.
_MISTRAL_PAGE_COST = 0.002
# Types OCRisables côté image (sans le point), pour la cible de la repasse.
_OCR_IMAGE_TYPES = {e.lstrip(".") for e in _IMG_EXTS}


def transcribe_plan(max_pages: int = 10, include_missing: bool = True,
                    include_born_digital: bool = False,
                    dedup_content: bool = True,
                    scope: str | None = None, output_file: str | None = None,
                    db: TrackingDB | None = None) -> dict:
    """Worklist de la **repasse Mistral**, bornée par le nombre de pages (coût).

    Cible : documents qui ont besoin d'OCR (PDF scannés, images-documents) et qui
    n'ont PAS encore de transcription Mistral — soit une transcription
    ``vision-local`` à *upgrader* vers le markdown structuré de Mistral, soit
    (``include_missing``) un scanné sans aucune transcription. Les PDF
    **born-digital** (couche texte propre) sont exclus et comptés
    (``born_digital_skip``) : pas de coût OCR inutile — sauf
    ``include_born_digital``, qui les embarque aussi (un seul moteur, un seul
    format de transcription pour toute la base ; comptés
    ``born_digital_included``). Borne ``--max-pages`` : un
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
              "born_digital_skip": 0, "born_digital_included": 0,
              "deferred_pages": 0, "phantom_dupes": 0, "content_dupes": 0,
              "user_excluded": 0, "encrypted_or_broken": 0,
              "non_ocr_type_skip": 0}
    excluded: list[dict] = []
    content_dupes: list[dict] = []   # même CONTENU (hash) qu'un représentant
    exclude_set = _filtres.load_exclude_set()   # exclusions utilisateur (payant)
    try:
        for rel, pkt in db.all_doc_signals():
            if scope and not rel.startswith(scope):
                continue
            if unicodedata.normalize("NFC", rel) in exclude_set:
                counts["user_excluded"] += 1     # exclu manuellement du payant
                continue
            typ = pkt.get("type")
            ts = pkt.get("text_source")
            born = pkt.get("born_digital")
            is_pdf = typ == "pdf"
            is_image = typ in _OCR_IMAGE_TYPES
            # Cible OCR : seulement des formats OCRisables (PDF + images). Un PDF
            # scanné (born False) ou un PDF/image déjà transcrit par Vision
            # (ocr_cache). On EXCLUT explicitement les formats à texte structuré
            # (epub/mobi/azw3/markdown/office) même s'ils ont une transcription :
            # les envoyer à un OCR n'a aucun sens (cf. ebooks captés par erreur).
            ocr_target = (is_pdf and (born is False or ts == "ocr_cache")) \
                or (is_image and ts == "ocr_cache")
            if not ocr_target and include_born_digital \
                    and is_pdf and born is True:
                # Uniformisation sur Mistral (décision 2026-06) : la couche
                # texte reste fidèle mais sans structure ; le markdown Mistral
                # vaut ~$1/1000 p, borné par --max-pages comme le reste.
                ocr_target = True
            if not ocr_target:
                if is_pdf and born is True:
                    counts["born_digital_skip"] += 1
                elif ts == "ocr_cache":   # transcription mais format non-OCR
                    counts["non_ocr_type_skip"] += 1
                continue
            # Écarter en amont les PDF qu'un OCR ne pourra pas traiter
            # (protégés par mot de passe / corrompus) — Mistral échouerait dessus.
            if pkt.get("pdf_status") in ("encrypted", "unreadable"):
                counts["encrypted_or_broken"] += 1
                excluded.append({"rel": rel, "pdf_status": pkt.get("pdf_status")})
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
            if engine == _ocr.OCR_ENGINE:
                reason = "upgrade_vision"
            elif include_born_digital and is_pdf and born is True:
                reason = "born_digital_included"
            else:
                reason = "missing"
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

        # Dédup par CONTENU (hash) : ne jamais envoyer deux fois le même fichier
        # à Mistral. Un représentant par hash reste dans la worklist ; les autres
        # vont en `content_dupes` (→ register-batch leur COPIERA la transcription
        # du représentant, sans re-OCR ni suppression du fichier). Hash via le
        # miroir SSD, mis en cache (1er passage lent, ensuite instantané).
        if dedup_content:
            by_hash: dict[str, dict] = {}
            kept: list[dict] = []
            for e in worklist:
                h = db.get_or_compute_hash(Path(e["source"]),
                                           read_path=Path(e["read_source"]))
                e["hash"] = h
                if h and h in by_hash:
                    content_dupes.append({**e, "same_as": by_hash[h]["transcription"]})
                else:
                    if h:
                        by_hash[h] = e
                    kept.append(e)
            counts["content_dupes"] = len(content_dupes)
            worklist[:] = kept
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
               "deferred": deferred,
               "content_dupes": content_dupes,
               "excluded": excluded}

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
    # Reprise idempotente : sauter les images déjà jugées (doc OU photo) lors d'un
    # balayage précédent — sinon on re-OCRise toutes les photos rejetées.
    processed = set() if force else db.image_ocr_logged_rels()
    skipped["deja_traite"] = 0
    stop = False
    try:
        for dp, dirs, fs in os.walk(base):
            if stop:
                break
            d = Path(dp)
            if d == DOCUMENTS_DIR:
                dirs[:] = [n for n in dirs if n not in _VIEW_TOP]
            dirs[:] = [n for n in dirs if not (d / n).is_symlink()]  # jamais un lien
            if (d.suffix.lower() in BUNDLE_SUFFIXES
                    or (set(fs) & CODE_MARKERS) or (set(dirs) & MARKER_DIRS)):
                dirs[:] = []
                continue
            for fn in fs:
                if fn.startswith(".") or Path(fn).suffix.lower() not in _IMG_EXTS:
                    continue
                if (d / fn).is_symlink():   # fichier-lien = doublon
                    continue
                # NFC obligatoire : os.walk renvoie du NFD sur APFS alors que
                # le journal stocke du NFC — sans normaliser, la reprise rate
                # les chemins accentués et re-OCRise ces images à chaque run.
                rel = unicodedata.normalize(
                    "NFC", str((d / fn).relative_to(DOCUMENTS_DIR)))
                if rel in processed:
                    skipped["deja_traite"] += 1
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
                res = _ocr.ocr_file(rp, max_pages=1)
                text = (res or {}).get("text", "").strip() if res else ""
                lines = text.count("\n") + 1 if text else 0
                conf = round(float((res or {}).get("confidence") or 0), 3)
                is_document = len(text) >= min_chars and lines >= min_lines
                if is_document:
                    trans.parent.mkdir(parents=True, exist_ok=True)
                    trans.write_text(text + "\n", encoding="utf-8")
                    register_document(db, ab, trans)
                    trans.write_text(_merge_frontmatter(
                        trans.read_text(encoding="utf-8"),
                        {"ocr_engine": _ocr.OCR_ENGINE, "ocr_confidence": conf,
                         "ocr_kind": "image"}), encoding="utf-8")
                    docs.append({"rel": rel, "chars": len(text), "confidence": conf})
                else:
                    non_doc += 1
                    if 0 < len(text) < min_chars * 2:   # proche du seuil → à revoir
                        borderline.append({"rel": rel, "chars": len(text), "conf": conf})
                # Journaliser le verdict (doc/photo) → reprise idempotente.
                db.log_image_ocr(rel, is_document, len(text), conf)
                if limit and len(docs) >= limit:
                    stop = True
                    break
    finally:
        if owns:
            db.close()
    confs = [d["confidence"] for d in docs]
    return {"documents_images": len(docs), "non_documents": non_doc,
            "skipped": skipped, "min_chars": min_chars, "min_lines": min_lines,
            "avg_confidence": round(statistics.mean(confs), 3) if confs else None,
            "sample_documents": docs[:12], "sample_borderline": borderline[:12]}
