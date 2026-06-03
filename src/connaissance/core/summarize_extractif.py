"""Résumé extractif + mots-clés + entités — zéro dépendance (stdlib).

Pour la Phase B (signaux sans OCR) : condenser un texte déjà obtenu (cache OCR,
texte Office, couche PDF born-digital) en signaux exploitables par le
pré-classement — SANS lib externe, SANS modèle.

On implémente l'**algorithme** plutôt que de tirer une lib (fidèle au pattern
SimHash du projet ; gensim a retiré son module, sumy/nltk imposent un
téléchargement de données, T5/GPT sont hors-sujet pour la phase gratuite) :

- **Luhn** (défaut) : phrases scorées par densité de mots significatifs
  (clusters). Robuste sur des documents courts et structurés (factures,
  lettres, relevés) — le gros de ~/Documents.
- **mots-clés** : mots de contenu les plus fréquents (hors mots-vides FR/EN).
- **entités** : montants, dates, numéros de référence par regex.

Toutes les fonctions sont pures et testables sans environnement.
"""
from __future__ import annotations

import re
from collections import Counter

# Mots-vides FR + EN (volontairement compact : suffisant pour pondérer la
# fréquence sans dépendre d'une ressource linguistique téléchargée).
_STOPWORDS = {
    # français
    "le", "la", "les", "un", "une", "des", "de", "du", "d", "l", "et", "ou",
    "à", "au", "aux", "en", "dans", "sur", "sous", "pour", "par", "avec",
    "sans", "ce", "cet", "cette", "ces", "se", "sa", "son", "ses", "ne",
    "pas", "plus", "que", "qui", "quoi", "dont", "où", "est", "sont", "été",
    "être", "avoir", "a", "ont", "fait", "faire", "comme", "mais", "donc",
    "car", "ni", "nous", "vous", "ils", "elles", "il", "elle", "je", "tu",
    "on", "leur", "leurs", "mon", "ma", "mes", "votre", "vos", "notre",
    "nos", "y", "si", "ainsi", "cela", "ceci", "tout", "tous", "toute",
    "toutes", "très", "entre", "vers", "chez", "selon",
    # anglais
    "the", "a", "an", "of", "to", "and", "or", "in", "on", "at", "for",
    "with", "without", "this", "that", "these", "those", "is", "are", "was",
    "were", "be", "been", "being", "as", "by", "from", "it", "its", "we",
    "you", "they", "he", "she", "i", "not", "no", "but", "so", "if", "then",
    "than", "which", "who", "what", "all", "any", "your", "our", "their",
}

_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][A-Za-zÀ-ÖØ-öø-ÿ0-9'’-]*")
# Frontières de phrase : ponctuation forte OU saut de ligne (les documents
# structurés alignent souvent une info par ligne, sans ponctuation).
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")

# --- Entités (regex légères) -----------------------------------------------
_AMOUNT_RE = re.compile(
    r"(?:(?:\$|€|£)\s?\d[\d  .,]*\d|\d[\d  .,]*\d\s?(?:\$|€|£|CAD|USD|EUR|"
    r"dollars?|euros?))",
)
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|"
    r"\d{1,2}\s+(?:janvier|février|mars|avril|mai|juin|juillet|août|"
    r"septembre|octobre|novembre|décembre|january|february|march|april|"
    r"may|june|july|august|september|october|november|december)\s+\d{4})\b",
    re.IGNORECASE,
)
_REF_RE = re.compile(
    r"\b(?:n[o°]|n[uú]m[eé]ro|r[eé]f[eé]rence|ref|facture|invoice|"
    r"confirmation|dossier|compte|account)\s*[:#.]?\s*([A-Z0-9][A-Z0-9-]{3,})\b",
    re.IGNORECASE,
)


def _norm_word(w: str) -> str:
    return w.lower().strip("'’-")


def tokenize_words(text: str) -> list[str]:
    """Mots (minuscule), apostrophes/tirets de bord retirés."""
    return [w for w in (_norm_word(m.group(0)) for m in _WORD_RE.finditer(text)) if w]


def tokenize_sentences(text: str) -> list[str]:
    """Découper en phrases (ponctuation forte ou saut de ligne)."""
    parts = _SENT_SPLIT_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


def keywords(text: str, top_n: int = 12, *, min_len: int = 3) -> list[str]:
    """Mots de contenu les plus fréquents (hors mots-vides, hors pur-numérique)."""
    counts: Counter = Counter()
    for w in tokenize_words(text):
        if len(w) < min_len or w in _STOPWORDS or w.isdigit():
            continue
        counts[w] += 1
    return [w for w, _ in counts.most_common(top_n)]


def luhn_summary(text: str, max_sentences: int = 3, *,
                 top_significant: int = 12, gap: int = 4) -> list[str]:
    """Résumé extractif de Luhn : phrases à plus forte densité de mots
    significatifs, rendues dans l'ordre d'origine.

    Mots significatifs = ``top_significant`` mots de contenu les plus fréquents.
    Score d'une phrase = max sur ses clusters de (n_signif² / longueur_cluster),
    un cluster étant une fenêtre de mots significatifs séparés d'au plus ``gap``
    mots non significatifs.
    """
    sentences = tokenize_sentences(text)
    if len(sentences) <= max_sentences:
        return sentences
    sig = set(keywords(text, top_n=top_significant))
    if not sig:
        return sentences[:max_sentences]

    scored: list[tuple[float, int, str]] = []
    for idx, sent in enumerate(sentences):
        words = tokenize_words(sent)
        # Indices des mots significatifs dans la phrase
        marks = [i for i, w in enumerate(words) if w in sig]
        if not marks:
            continue
        best = 0.0
        start = 0
        for k in range(1, len(marks) + 1):
            if k == len(marks) or marks[k] - marks[k - 1] > gap:
                cluster = marks[start:k]
                span = cluster[-1] - cluster[0] + 1
                score = (len(cluster) ** 2) / span
                best = max(best, score)
                start = k
        scored.append((best, idx, sent))

    top = sorted(scored, key=lambda t: (-t[0], t[1]))[:max_sentences]
    return [s for _, _, s in sorted(top, key=lambda t: t[1])]


def extract_entities(text: str) -> dict:
    """Montants, dates et numéros de référence (regex). Listes dédupliquées."""
    def _uniq(seq, limit=10):
        seen, out = set(), []
        for x in seq:
            x = re.sub(r"\s+", " ", x).strip()
            key = x.lower()
            if key and key not in seen:
                seen.add(key)
                out.append(x)
            if len(out) >= limit:
                break
        return out

    return {
        "amounts": _uniq(m.group(0) for m in _AMOUNT_RE.finditer(text)),
        "dates": _uniq(m.group(0) for m in _DATE_RE.finditer(text)),
        "refs": _uniq(m.group(1) for m in _REF_RE.finditer(text)),
    }


def summarize(text: str, *, max_sentences: int = 3, top_keywords: int = 12) -> dict:
    """Paquet de résumé extractif complet pour un texte (schema interne).

    Retourne ``{keywords, sentences, entities, chars}``. Vide proprement si le
    texte est absent/trop court.
    """
    text = (text or "").strip()
    if not text:
        return {"keywords": [], "sentences": [], "entities": {},
                "chars": 0}
    return {
        "keywords": keywords(text, top_n=top_keywords),
        "sentences": luhn_summary(text, max_sentences=max_sentences),
        "entities": extract_entities(text),
        "chars": len(text),
    }
