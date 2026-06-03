"""Phase C — pré-classement heuristique (le « hint » gratuit, zéro LLM).

Transforme un paquet de signaux (Phase B) en une proposition de rangement :
date, entité (+ type), catégorie, titre, sujet, confiance — par règles, sans
appel Claude. Deux rôles :

  1. classer seuls les cas **évidents** (noms déjà structurés
     « AAAA-MM-JJ - Entité - Catégorie - Titre ») ;
  2. servir de **hint** que l'agent Claude confirme/corrige (approche hybride :
     input court = on envoie tout à Claude avec ce point de départ + la liste
     des entités connues, pour une classification cohérente).

Pur et testable. La réconciliation finale de l'entité contre le registre
existant se fait en aval (``resolution.py``).
"""
from __future__ import annotations

import re
import unicodedata

# Catégories par mots-clés (FR/EN). L'ordre = priorité. Termes de NOM + de
# CONTENU (minés du corpus : debit/credit/ytd, cotisation, cumul…) pour attraper
# la catégorie même quand le nom de fichier est muet (« scan001.pdf »).
_CATEGORY_RULES = [
    ("impot", re.compile(r"\b(imp[oô]ts?|t[45]\b|rl[123]\b|fiscal|d[eé]clarations?|cotisations?|avis de cotisation|revenu (?:qu[eé]bec|canada)|d'imp[oô]t)\b", re.I)),
    ("taxes", re.compile(r"\b(taxes?\s+(?:municipales?|scolaires?)|compte de taxes)\b", re.I)),
    ("paie", re.compile(r"\b(paie|salaires?|payslip|paystub|bulletin de paie|t4\b|relev[eé] 1|cumul|gains? (?:imposables?|ytd)|remises?\b|ytd)\b", re.I)),
    ("releve", re.compile(r"\b(relev[eé]|statement|sommaire de compte|d[eé]bit\b|cr[eé]dit\b|solde|imposable|taxable)\b", re.I)),
    ("facture", re.compile(r"\b(factures?|facturation|invoices?|re[çc]us?|receipts?|sous-total|amount due|montant d[uû])\b", re.I)),
    ("paiement", re.compile(r"\b(paiements?|confirmation (?:de )?paiement|virements?|op[eé]ration effectu[eé]e)\b", re.I)),
    ("contrat", re.compile(r"\b(contrats?|contracts?|ententes?|mandats?|bail|conventions?)\b", re.I)),
    ("assurance", re.compile(r"\b(assurances?|polices?|insurance|policy|primes? d'assurance)\b", re.I)),
    ("hypotheque", re.compile(r"\b(hypoth[eè]ques?|pr[eê]t hypoth[eé]caire|mortgage)\b", re.I)),
    ("certificat", re.compile(r"\b(certificats?|dipl[oô]mes?|attestations?|certificates?)\b", re.I)),
    ("lettre", re.compile(r"\b(lettres?|courriers?|letters?|avis)\b", re.I)),
]

# Marqueurs d'ORGANISME (sinon on tente « personne » puis « divers »).
_ORG_MARKERS = re.compile(
    r"\b(banque|bank|caisse|desjardins|hydro|[eé]nergir|vid[eé]otron|bell|"
    r"rogers|telus|fizz|koodo|gouvernement|revenu|minist[eè]re|ville de|"
    r"municipalit[eé]|assurances?|mutuelle|coop|fondation|universit[eé]|"
    r"c[eé]gep|[eé]cole|clinique|h[oô]pital|notaire|avocats?|"
    r"inc\b|lt[eé]e\b|ltd\b|corp\b|s\.?e\.?n\.?c|google|amazon|aws|microsoft|"
    r"apple|stripe|paypal)\b", re.I)

# Dossiers d'origine → sujet (normalisé). Sinon : forme normalisée du dossier.
_SUJET_RULES = [
    (re.compile(r"\b(maison|logement|immeuble|hypoth|propri[eé]t|travaux|r[eé]novation|municipalit)", re.I), "maison"),
    (re.compile(r"\b(imp[oô]t|fiscal|d[eé]claration)", re.I), "impots"),
    (re.compile(r"\b(paie|salaire|emploi|rh|ressources humaines)", re.I), "emploi"),
    (re.compile(r"\b(sant[eé]|m[eé]dical|clinique|h[oô]pital|assurance)", re.I), "sante"),
    (re.compile(r"\b(voyage|vacances|billet)", re.I), "voyage"),
    (re.compile(r"\b(auto|voiture|v[eé]hicule|saaq)", re.I), "vehicule"),
    (re.compile(r"\b(formation|cours|certification|dipl[oô]me)", re.I), "formation"),
]

_DATE_RE = re.compile(r"(19[89]\d|20\d{2})[-/.]?(0[1-9]|1[0-2])[-/.]?(0[1-9]|[12]\d|3[01])")
_PERSON_RE = re.compile(r"^[A-ZÀ-Ö][a-zà-ÿ'’-]+(?:\s+[A-ZÀ-Ö][a-zà-ÿ'’-]+){1,2}$")

# Mots de TYPE de document (minés du vrai corpus) à retirer d'un candidat
# d'entité : « BNC Sommaire Relevé de compte » → « BNC ». Inclut articles/
# suffixes juridiques pour un nettoyage propre.
_TYPE_WORDS = {
    "facture", "factures", "recu", "recus", "releve", "releves", "paiement",
    "paiements", "sommaire", "sommaires", "compte", "comptes", "courant",
    "cotisation", "avis", "statement", "report", "rapport", "officiel",
    "document", "documents", "confirmation", "depot", "depots", "transaction",
    "transactions", "bordereau", "manuel", "manuels", "declaration",
    "formulaire", "paye", "payes", "bulletin", "note", "notes", "contrat",
    "contrats", "de", "du", "des", "le", "la", "les", "en", "et",
    "inc", "ltee", "ltd", "corp", "enr",
    # Bruit de noms générés par scanner/app (jamais une entité).
    "scanner", "doxie", "scan", "scanned", "img", "image", "images", "photo",
    "screenshot", "capture", "untitled", "numerisation", "numerise", "fichier",
    "page", "pages", "export", "copie", "copy", "final", "version",
}


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def _to_iso_date(raw: str | None) -> str | None:
    """Extraire AAAA-MM-JJ d'une chaîne (nom, métadonnée ISO, ``D:2024…``)."""
    if not raw:
        return None
    m = _DATE_RE.search(raw)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def pick_date(signals: dict) -> tuple[str | None, str]:
    """(date AAAA-MM-JJ, provenance). Priorité : nom > métadonnée > filesystem."""
    dates = signals.get("dates", {}) or {}
    for key, label in (("from_name", "name"), ("metadata", "metadata"),
                       ("filesystem_created", "filesystem")):
        d = _to_iso_date(dates.get(key) if isinstance(dates.get(key), str)
                         else dates.get(key))
        if d:
            return d, label
    return None, "none"


def _name_segments(stem: str) -> list[str]:
    """Segments d'un nom « A - B - C » (ou « A — B »), date retirée, nettoyés."""
    parts = re.split(r"\s+[-—–]\s+", stem)
    out = []
    for p in parts:
        p = p.strip()
        if not p or _DATE_RE.fullmatch(p.replace(" ", "")):
            continue
        # Retirer une date en tête de segment (« 2025-09-29 facture »).
        p = _DATE_RE.sub("", p).strip(" -—–")
        if p:
            out.append(p)
    return out


def guess_category(signals: dict, segments: list[str]) -> str | None:
    hay = " ".join([signals.get("type_hint") or "",
                    " ".join(signals.get("name_keywords") or []),
                    " ".join(segments),
                    " ".join((signals.get("summary") or {}).get("keywords") or [])])
    for cat, rx in _CATEGORY_RULES:
        if rx.search(hay):
            return cat
    return signals.get("type_hint")


def guess_sujet(origin_folder: str | None) -> str | None:
    if not origin_folder:
        return None
    for rx, sujet in _SUJET_RULES:
        if rx.search(origin_folder):
            return sujet
    return None


def _classify_entity_type(name: str) -> str:
    if _ORG_MARKERS.search(name):
        return "organismes"
    if _PERSON_RE.match(name.strip()):
        return "personnes"
    return "divers"


def _strip_type_words(text: str) -> str:
    """Candidat d'entité nettoyé : underscores → espaces, dates/nombres/années
    et mots-de-type (+ bruit scanner) retirés."""
    base = _DATE_RE.sub("", (text or "").replace("_", " "))
    kept = []
    for t in re.split(r"\s+", base.strip()):
        n = _norm(t.strip("'’-_·"))
        if not n or n in _TYPE_WORDS:
            continue
        if re.fullmatch(r"\d{1,4}(-\d{1,4})?", n):   # nombres, années, plages
            continue
        # Mot-scanner collé à un numéro : « scan001 », « img1234 », « doxie2024 ».
        m = re.fullmatch(r"([a-zà-ÿ]+)\d+", n)
        if m and m.group(1) in _TYPE_WORDS:
            continue
        kept.append(t.strip("'’-_·"))
    return re.sub(r"\s+", " ", " ".join(kept)).strip(" -—–_·")


def guess_entity(signals: dict, segments: list[str],
                 known_entities: list[str] | None = None) -> tuple[str | None, str, bool]:
    """(nom d'entité, entity_type, matché_connu).

    Candidat = 1er segment du nom **nettoyé** (mots-de-type, bruit scanner,
    nombres) ; si vide (nom « scanner_2024 », date seule…), on se rabat sur le
    **dossier d'origine** nettoyé — ce qui couvre les ~900 fichiers scanner/doxie
    et les noms non segmentés. ``known_entities`` permet un alignement
    (sous-chaîne normalisée) pour la cohérence inter-documents.
    """
    candidate = None
    if segments:
        candidate = _strip_type_words(segments[0]) or None
    if not candidate and signals.get("origin_folder"):
        lead = re.split(r"[\\/]", signals["origin_folder"])[0]
        candidate = _strip_type_words(lead) or None

    if not candidate:
        return None, "inconnus", False

    if known_entities:
        cnd = _norm(candidate)
        for ent in known_entities:
            ne = _norm(ent)
            if ne and (ne in cnd or cnd in ne):
                return ent, _classify_entity_type(ent), True

    return candidate, _classify_entity_type(candidate), False


def guess_title(stem: str, segments: list[str], entity: str | None) -> str:
    """Titre nettoyé : segments hors entité (et nom d'entité retiré s'il est
    resté collé dans un segment unique), sinon nom sans date."""
    kept = [s for s in segments if s != entity]
    title = " ".join(kept) if kept else _DATE_RE.sub("", stem).strip(" -—–_")
    if entity:
        title = re.sub(re.escape(entity), "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title.replace("_", " ")).strip(" -—–·")
    return title or stem


def classify(signals: dict, known_entities: list[str] | None = None) -> dict:
    """Proposition de rangement heuristique (le « hint ») pour un paquet de signaux."""
    rel = signals.get("rel", "")
    stem = re.sub(r"\.[^.]+$", "", rel.split("/")[-1]) if rel else ""
    segments = _name_segments(stem)

    date, date_src = pick_date(signals)
    entity, entity_type, known = guess_entity(signals, segments, known_entities)
    category = guess_category(signals, segments)
    sujet = guess_sujet(signals.get("origin_folder"))
    title = guess_title(stem, segments, entity)

    # Confiance : haute si on a une date + une entité (connue, ou nom bien
    # segmenté) + une catégorie. Sinon basse (→ Claude tranche / zone d'attente).
    well_named = len(segments) >= 2
    confidence = "high" if (date and entity and category
                            and (known or well_named)) else "low"

    return {
        "rel": rel,
        "date": date,
        "date_source": date_src,
        "entity": entity,
        "entity_type": entity_type,
        "entity_known": known,
        "category": category,
        "sujet": sujet,
        "title": title,
        "confidence": confidence,
    }
