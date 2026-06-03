"""Phase B — extraction de signaux d'un document, SANS OCR.

Produit un « paquet de signaux » par document pour alimenter le pré-classement
(Phase C : entité + catégorie + date + titre + sujet) — sans jamais payer
d'OCR Mistral. Lecture seule.

Principe : **cascade du moins cher au plus cher**, pour n'ouvrir pypdfium2
(dépendance optionnelle) qu'en tout dernier recours.

  1. Nom + chemin + dates filesystem + type            (stdlib, sans contenu)
  2. Métadonnées Office (docProps/core.xml)             (stdlib : zipfile+XML)
  3. Texte du cache OCR existant                        (fourni par l'appelant)
  4. Texte Office (.docx) / fichier texte (.txt/.csv)   (stdlib)
  5. Couche texte des PDF born-digital, page(s) 1       (pypdfium2 SI installé)

La détection born-digital vs scanné se fait à l'étape 5 : page 1 sans couche
texte ⇒ scanné (aucun texte gratuit ⇒ signaux nom/chemin/métadonnées seulement,
l'OCR viendra plus tard). pypdfium2 absent ⇒ on dégrade proprement.

``extract_signals`` est pur : il reçoit le chemin canonique (identité + stat),
un ``read_path`` optionnel (miroir SSD pour lire le CONTENU sans download
iCloud ; ``None`` = ne pas lire le contenu, ex. fichier dataless sans miroir),
et le texte du cache OCR s'il existe.
"""
from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from connaissance.core import summarize_extractif as _sum

# pypdfium2 : dépendance OPTIONNELLE (extra `pdf`). Absente ⇒ pas de texte PDF
# born-digital, le reste des signaux fonctionne quand même.
try:
    import pypdfium2 as _pdfium
except Exception:                       # pragma: no cover - dépend de l'install
    _pdfium = None

PLAIN_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv"}
OFFICE_META_EXTS = {".docx", ".xlsx", ".pptx"}
_PDF_MAX_PAGES = 2
_PDF_MAX_CHARS = 4000
_PLAIN_MAX_CHARS = 20000
_BORN_DIGITAL_MIN_CHARS = 20   # texte page 1 sous ce seuil ⇒ scanné

_DATE_IN_NAME_RE = re.compile(
    r"\b(19[89]\d|20\d{2})[-_. ]?(0[1-9]|1[0-2])[-_. ]?(0[1-9]|[12]\d|3[01])\b")

# Indices de TYPE déduits du nom (FR/EN), sans lire le contenu.
_TYPE_HINTS = [
    ("facture", re.compile(r"\b(facture|invoice|recu|reçu|receipt)\b", re.I)),
    ("releve", re.compile(r"\b(relev[eé]|statement|bordereau)\b", re.I)),
    ("contrat", re.compile(r"\b(contrat|contract|entente|mandat|bail|convention)\b", re.I)),
    ("impot", re.compile(r"\b(imp[oô]ts?|t[45]|rl[123]|fiscal|d[eé]claration)\b", re.I)),
    ("paie", re.compile(r"\b(paie|salaire|payslip|paystub|bulletin)\b", re.I)),
    ("assurance", re.compile(r"\b(assurance|police|insurance)\b", re.I)),
    ("lettre", re.compile(r"\b(lettre|courrier|letter)\b", re.I)),
    ("cv", re.compile(r"\b(cv|curriculum|r[eé]sum[eé])\b", re.I)),
    ("certificat", re.compile(r"\b(certificat|dipl[oô]me|attestation|certificate)\b", re.I)),
]

# Espaces de noms Office (docProps/core.xml).
_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _fs_dates(path: Path) -> tuple[str | None, str | None]:
    """(created, modified) du filesystem — métadonnées seules, jamais de download."""
    try:
        st = path.stat()
    except OSError:
        return None, None
    modified = _iso(st.st_mtime)
    birth = getattr(st, "st_birthtime", 0)
    created = _iso(birth) if birth and birth > 0 else modified
    return created, modified


def _date_from_name(name: str) -> str | None:
    m = _DATE_IN_NAME_RE.search(name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _type_hint(name: str) -> str | None:
    for label, rx in _TYPE_HINTS:
        if rx.search(name):
            return label
    return None


def _office_metadata(path: Path) -> dict:
    """Titre/auteur/date depuis docProps/core.xml d'un fichier Office (stdlib)."""
    out: dict = {}
    try:
        with zipfile.ZipFile(path) as z:
            with z.open("docProps/core.xml") as f:
                root = ET.parse(f).getroot()
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return out
    title = root.findtext("dc:title", default="", namespaces=_NS).strip()
    author = root.findtext("dc:creator", default="", namespaces=_NS).strip()
    created = root.findtext("dcterms:created", default="", namespaces=_NS).strip()
    if title:
        out["title"] = title
    if author:
        out["author"] = author
    if created:
        out["created"] = created
    return out


def _docx_text(path: Path, max_chars: int = _PLAIN_MAX_CHARS) -> str:
    """Texte d'un .docx via word/document.xml (stdlib, sans lib Office)."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""
    # Sauts de paragraphe puis texte des <w:t>.
    xml = re.sub(r"</w:p>", "\n", xml)
    parts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, flags=re.S)
    text = " ".join(parts)
    # Déséchapper les entités XML de base.
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(a, b)
    return text[:max_chars]


def _plain_text(read_path: Path, max_chars: int = _PLAIN_MAX_CHARS) -> str:
    try:
        return read_path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        return ""


def _pdf_text_and_meta(read_path: Path) -> tuple[str | None, dict, bool]:
    """(texte page(s) 1, métadonnées, pypdfium2_disponible).

    ``texte`` vaut ``None`` si pypdfium2 est absent (impossible de trancher),
    ``""`` si le PDF est ouvert mais sans couche texte (⇒ scanné).
    """
    if _pdfium is None:
        return None, {}, False
    try:
        doc = _pdfium.PdfDocument(str(read_path))
    except Exception:
        return "", {}, True
    try:
        meta: dict = {}
        try:
            md = doc.get_metadata_dict() or {}
            for src, dst in (("Title", "title"), ("Author", "author"),
                             ("CreationDate", "created")):
                v = (md.get(src) or "").strip()
                if v:
                    meta[dst] = v
        except Exception:
            pass
        texts: list[str] = []
        total = 0
        for i in range(min(len(doc), _PDF_MAX_PAGES)):
            try:
                page = doc[i]
                tp = page.get_textpage()
                t = tp.get_text_bounded()
            except Exception:
                continue
            texts.append(t)
            total += len(t)
            if total >= _PDF_MAX_CHARS:
                break
        return "\n".join(texts)[:_PDF_MAX_CHARS], meta, True
    finally:
        try:
            doc.close()
        except Exception:
            pass


def extract_signals(path, *, rel: str | None = None,
                    read_path=None, ocr_cache_text: str | None = None) -> dict:
    """Paquet de signaux d'un document (schema DocumentSignals).

    ``path`` : chemin canonique (identité + stat, jamais de download).
    ``rel``  : chemin relatif (pour le dossier d'origine → sujet). Défaut: nom.
    ``read_path`` : où lire le CONTENU (miroir SSD). ``None`` ⇒ ne pas lire le
        contenu (fichier dataless sans miroir) : signaux nom/chemin/dates seuls.
    ``ocr_cache_text`` : texte d'une transcription existante (gratuit), prioritaire.
    """
    path = Path(path)
    ext = path.suffix.lower()
    rel = rel or path.name
    rel_parts = Path(rel).parts
    origin_folder = rel_parts[-2] if len(rel_parts) >= 2 else None

    created, modified = _fs_dates(path)
    meta: dict = {}
    text = ""
    text_source = "none"
    born_digital: bool | None = None
    pdf_available = _pdfium is not None

    read = Path(read_path) if read_path is not None else None

    # Étape 3 : cache OCR (prioritaire, gratuit).
    if ocr_cache_text and ocr_cache_text.strip():
        text = ocr_cache_text
        text_source = "ocr_cache"

    if read is not None:
        # Étape 2 : métadonnées Office (même si on a déjà le texte du cache).
        if ext in OFFICE_META_EXTS:
            meta = _office_metadata(read)

        # Étapes 4-5 : n'acquérir du texte que si le cache n'en a pas fourni.
        if text_source == "none":
            if ext in PLAIN_EXTS:
                text = _plain_text(read)
                text_source = "plain" if text.strip() else "none"
            elif ext == ".docx":
                text = _docx_text(read)
                text_source = "office" if text.strip() else "none"
            elif ext == ".pdf":
                pdf_text, pdf_meta, pdf_available = _pdf_text_and_meta(read)
                meta = {**pdf_meta, **meta}
                if pdf_text is None:
                    born_digital = None          # indécidable sans pypdfium2
                elif len(pdf_text.strip()) >= _BORN_DIGITAL_MIN_CHARS:
                    text, text_source = pdf_text, "pdf_embedded"
                    born_digital = True
                else:
                    born_digital = False         # page 1 sans texte ⇒ scanné

    summary = _sum.summarize(text) if text.strip() else \
        {"keywords": [], "sentences": [], "entities": {}, "chars": 0}

    return {
        "rel": rel,
        "type": ext.lstrip("."),
        "origin_folder": origin_folder,
        "type_hint": _type_hint(path.name),
        "name_keywords": _sum.keywords(path.stem.replace("_", " ").replace("-", " "),
                                       top_n=6),
        "dates": {
            "from_name": _date_from_name(path.name),
            "filesystem_created": created,
            "filesystem_modified": modified,
            "metadata": meta.get("created"),
        },
        "title_meta": meta.get("title"),
        "author_meta": meta.get("author"),
        "born_digital": born_digital,
        "text_source": text_source,
        "pdf_available": pdf_available,
        "summary": summary,
    }
