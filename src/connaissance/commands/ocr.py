"""OCR LOCAL (macOS Vision) — première passe gratuite de transcription.

Produit des transcriptions sous ``Transcriptions/Documents/`` marquées
``ocr_engine: vision-local`` + ``ocr_confidence``. Les autres étapes
(``signals``/``classify``/``summarize``) les lisent comme n'importe quelle
transcription. La **repasse Mistral** est gardée en option : on identifie les
transcriptions à faible confiance et on les remet « à transcrire » (le flux
Mistral existant les reprend alors, en écrasant la version locale).
"""
from __future__ import annotations

import statistics
from pathlib import Path

import yaml

from connaissance.core import ledger as _ledger
from connaissance.core import ocr_local as _ocr
from connaissance.commands.documents import (TRANSCRIPTIONS_DIR,
                                             _merge_frontmatter, register_document)
from connaissance.core.paths import (DOCUMENTS_DIR, documents_read_path,
                                     require_paths)
from connaissance.core.tracking import TrackingDB


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
            res = _ocr.ocr_file(rp)
            text = (res or {}).get("text", "").strip() if res else ""
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
