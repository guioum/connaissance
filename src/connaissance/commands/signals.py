"""Phase B — `documents signals` : extraire les signaux des documents en vrac.

Parcourt les vrais documents (groupe A) de ~/Documents et produit un paquet de
signaux par fichier (voir [`core/signals.py`](../core/signals.py)), pour
alimenter le pré-classement (Phase C). **Lecture seule, zéro OCR, zéro réseau.**

Garde-fous, cohérents avec le reste du chantier :
  - **Conteneurs élagués** (repos, bundles, deps) comme `triage`/`secrets`.
  - **Secrets exclus** : un fichier en quarantaine ou au nom de clé n'est pas
    ouvert (on ne veut pas son contenu dans un paquet de signaux).
  - **Via le SSD** (`documents_read_path`) ; un `dataless` sans miroir n'est pas
    lu (signaux nom/chemin/dates seulement, jamais de download iCloud).
  - **Cache** `tracking.db` keyé `(rel, size, mtime)` → re-runs instantanés.
"""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from connaissance.commands.triage import (BUNDLE_SUFFIXES, CODE_MARKERS,
                                           DOC_EXTS, MARKER_DIRS)
from connaissance.core import filtres as _filtres
from connaissance.core import secrets as _secrets
from connaissance.core import signals as _signals
from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import (CONNAISSANCE_ROOT, DOCUMENTS_DIR,
                                      documents_read_path, is_dataless)
from connaissance.core.tracking import TrackingDB

TRANSCRIPTIONS_DIR = CONNAISSANCE_ROOT / "Transcriptions" / "Documents"
_SKIP_TOP = {"- Par catégorie", "- Sujets", "organismes", "personnes",
             "divers", "promus"}
_NOISE_DIRS = {"bower_components", "Pods", "site-packages", ".tox", ".venv",
               "venv", "dist", "build", ".next", ".nuxt", "__pycache__"}
# Extensions image (sans le point). Les images ne sont PAS scannées en masse
# (sinon 13k+ photos souvenir polluent doc_signals) : une image n'entre dans les
# signaux que si elle a déjà une transcription, c.-à-d. qu'elle a été reconnue
# comme DOCUMENT par `documents ocr-images` (densité de texte). Les photos
# souvenir, sans transcription, restent hors pipeline.
_IMG_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "tiff", "tif", "webp",
             "gif", "bmp"}


def _strip_frontmatter(md: str) -> str:
    if not md.startswith("---"):
        return md
    end = md.find("\n---", 4)
    return md[end + 4:].lstrip("\n") if end >= 0 else md


def _ocr_cache_text(rel: str) -> str | None:
    """Texte d'une transcription existante (cache OCR gratuit), ou None."""
    trans = TRANSCRIPTIONS_DIR / Path(rel).with_suffix(".md")
    if not trans.exists():
        return None
    try:
        return _strip_frontmatter(trans.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def scan(scope: str | None = None, output_file: str | None = None,
         db: TrackingDB | None = None) -> dict:
    """Extraire les signaux des documents du groupe A (schema DocumentsSignals)."""
    base = DOCUMENTS_DIR if scope is None else (DOCUMENTS_DIR / scope)
    quarantine = _filtres.load_quarantine_set()
    packets: list[dict] = []
    skipped = {"dataless": 0, "secret": 0, "image_non_document": 0}
    seen = 0

    if not base.exists():
        return {"total": 0, "documents": [], "skipped": skipped,
                "note": f"{base} n'existe pas."}

    owns_db = db is None
    if db is None:
        db = TrackingDB()

    try:
        for dirpath, dirnames, filenames in os.walk(base):
            d = Path(dirpath)
            if d == DOCUMENTS_DIR:
                dirnames[:] = [n for n in dirnames if n not in _SKIP_TOP]
            is_bundle = d.suffix.lower() in BUNDLE_SUFFIXES
            is_repo = bool(set(filenames) & CODE_MARKERS) \
                or bool(set(dirnames) & MARKER_DIRS)
            if is_bundle or is_repo:
                dirnames[:] = []
                continue
            dirnames[:] = [n for n in dirnames if n not in _NOISE_DIRS]

            for fname in filenames:
                if fname.startswith("."):
                    continue
                ext = Path(fname).suffix.lower().lstrip(".")
                is_img = ext in _IMG_EXTS
                if ext not in DOC_EXTS and not is_img:
                    continue
                fpath = d / fname
                rel = str(fpath.relative_to(DOCUMENTS_DIR))

                # Secrets : ne pas ouvrir le contenu d'un fichier sensible.
                sig = _secrets.filename_signal(fname)
                if (sig and sig[1] == "high") or \
                        unicodedata.normalize("NFC", rel) in quarantine:
                    skipped["secret"] += 1
                    continue

                ocr = _ocr_cache_text(rel)

                # Image SANS transcription = photo souvenir (pas un document) :
                # hors doc_signals. Avec transcription = reconnue document par
                # `ocr-images` → traitée comme les PDF (pré-classement, repasse).
                if is_img and ocr is None:
                    skipped["image_non_document"] += 1
                    continue

                seen += 1

                read_path = documents_read_path(fpath)
                read = read_path
                if read_path == fpath and is_dataless(fpath):
                    read = None
                    skipped["dataless"] += 1

                def _compute(p, _rel=rel, _read=read, _ocr=ocr):
                    return _signals.extract_signals(p, rel=_rel, read_path=_read,
                                                    ocr_cache_text=_ocr)

                if read is None:
                    # Contenu non lu (dataless) : packet partiel, pas de cache
                    # (size/mtime ne changent pas si le fichier se matérialise).
                    packet = _compute(fpath)
                else:
                    packet = db.get_or_compute_signals(fpath, rel, _compute)
                if packet is not None:
                    packets.append(packet)
    finally:
        if owns_db:
            db.close()

    payload = {
        "total": len(packets),
        "scanned": seen,
        "documents": packets,
        "skipped": skipped,
        "pdf_text_layer": _signals._pdfium is not None,
        "note": ("Lecture seule, zéro OCR. Signaux destinés au pré-classement "
                 "(Phase C). " + (
                     "pypdfium2 actif ⇒ PDF born-digital lus + nb de pages capturé."
                     if _signals._pdfium is not None else
                     "⚠️ pypdfium2 ABSENT ⇒ PDF born-digital NON lus, pages NON "
                     "capturées (installer l'extra `pdf` avant de re-signaler, "
                     "sinon le cache born-digital se dégrade).")),
    }

    def _summary(p: dict) -> dict:
        from collections import Counter
        docs = p["documents"]
        src = Counter(x["text_source"] for x in docs)
        born = Counter(str(x["born_digital"]) for x in docs)
        types = Counter(x["type"] for x in docs)
        folders = Counter(x["origin_folder"] for x in docs if x["origin_folder"])
        return {
            "total": p["total"],
            "scanned": p["scanned"],
            "skipped": p["skipped"],
            "by_text_source": dict(src.most_common()),
            "by_born_digital": dict(born.most_common()),
            "by_type": dict(types.most_common(12)),
            "top_origin_folders": dict(folders.most_common(15)),
            "sample": [x["rel"] for x in docs[:10]],
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)
