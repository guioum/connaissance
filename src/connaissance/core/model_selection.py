"""Heuristique centrale de choix de modèle Claude pour les résumés et synthèses.

Un seul endroit dans tout le pipeline où l'on décide :
« quel modèle envoyer à l'API Anthropic pour ce résumé / cette synthèse ? »

La logique est volontairement déterministe (pas d'appel LLM, pas de config
utilisateur cachée) : la skill qui reçoit la demande explique le choix à
l'utilisateur en langage naturel avant confirmation, et le CLI peut être
forcé via un paramètre ``preference`` (``quality``, ``economy`` ou ``auto``).

La décision dépend de trois axes :

- ``source_type`` : document / courriel / note / fil. Les courriels courts et
  les notes sont bien traités par Haiku ; les documents longs et les fils
  multi-messages bénéficient de Sonnet.
- ``content_length`` : taille du ``user`` à envoyer en caractères. Au-delà
  d'un seuil, Sonnet tient mieux la longueur.
- ``age_days`` : âge du document en jours. Les vieux résumés rétroactifs
  (> 18 mois) ont moins d'intérêt que la mémoire récente ; on les
  bascule volontiers sur Haiku pour limiter le budget d'un gros rattrapage.

Les seuils sont documentés dans le dict ``THRESHOLDS`` ci-dessous et peuvent
être ajustés d'un seul endroit si la calibration réelle (via
``pipeline costs --real``) montre une dérive qualité.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

Preference = Literal["auto", "quality", "economy"]

MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5"

THRESHOLDS = {
    # Un courriel / une note ≤ ce seuil (chars du body) est considéré court.
    "courriel_short_chars": 2000,
    "note_short_chars": 2500,
    # Un document dont le body dépasse ce seuil reste sur Sonnet même en
    # mode économie (Haiku décroche sur les documents longs scannés).
    "document_long_chars": 12000,
    # Au-delà de cet âge, une source est « ancienne » et candidate à Haiku
    # en mode auto — la qualité marginale coûte plus cher que sa valeur.
    "ancien_jours": 540,  # ~18 mois
}


def _parse_date(value) -> Optional[date]:
    """Accepter str (YYYY-MM-DD / ISO) ou datetime.date/datetime. Retourne
    None si la valeur est absente ou inexploitable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[: len(fmt)], fmt).date()
        except ValueError:
            continue
    # Dernier recours : tronquer à YYYY-MM-DD
    if len(s) >= 10:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def _age_days(reference_date) -> Optional[int]:
    d = _parse_date(reference_date)
    if d is None:
        return None
    return max(0, (date.today() - d).days)


def choose_model(source_type: str,
                 content_length: int = 0,
                 reference_date=None,
                 preference: Preference = "auto") -> dict:
    """Choisir un modèle Claude pour une requête donnée.

    Parameters
    ----------
    source_type : str
        L'un de {document, courriel, note, fil, fiche, chronologie, moc, digest}.
    content_length : int
        Nombre de caractères du ``user`` à envoyer. Zéro si inconnu.
    reference_date : str | date | datetime | None
        Date sémantique de la source (frontmatter ``date`` ou ``created``).
        Utilisée pour détecter les lots rétroactifs.
    preference : "auto" | "quality" | "economy"
        - ``auto`` : choix par défaut selon l'heuristique.
        - ``quality`` : force Sonnet sauf pour les cas triviaux.
        - ``economy`` : force Haiku sauf pour les cas où il décroche.

    Returns
    -------
    dict avec ``model``, ``reason`` (phrase courte utile à afficher à
    l'utilisateur) et ``tier`` ("sonnet" | "haiku"). Le caller injecte
    ``model`` dans la request envoyée au MCP claude-api.
    """
    st = (source_type or "").lower()
    age = _age_days(reference_date)
    ancien = age is not None and age > THRESHOLDS["ancien_jours"]

    # Mode quality : Sonnet partout, sauf notes très courtes où ça ne change rien.
    if preference == "quality":
        if st == "note" and content_length <= THRESHOLDS["note_short_chars"]:
            return {"model": MODEL_HAIKU, "tier": "haiku",
                    "reason": "note très courte — Haiku suffit même en mode qualité"}
        return {"model": MODEL_SONNET, "tier": "sonnet",
                "reason": "mode qualité forcé"}

    # Mode economy : Haiku partout, sauf documents longs et fils où il décroche.
    if preference == "economy":
        if st == "document" and content_length > THRESHOLDS["document_long_chars"]:
            return {"model": MODEL_SONNET, "tier": "sonnet",
                    "reason": "document long — Haiku décroche, on garde Sonnet"}
        if st == "fil":
            return {"model": MODEL_SONNET, "tier": "sonnet",
                    "reason": "fil multi-messages — Haiku décroche, on garde Sonnet"}
        return {"model": MODEL_HAIKU, "tier": "haiku",
                "reason": "mode économie forcé"}

    # Mode auto — les cas simples d'abord.
    if st == "note":
        if content_length <= THRESHOLDS["note_short_chars"]:
            return {"model": MODEL_HAIKU, "tier": "haiku",
                    "reason": "note courte"}
        if ancien:
            return {"model": MODEL_HAIKU, "tier": "haiku",
                    "reason": "note ancienne"}
        return {"model": MODEL_SONNET, "tier": "sonnet",
                "reason": "note longue récente"}

    if st == "courriel":
        if content_length <= THRESHOLDS["courriel_short_chars"]:
            return {"model": MODEL_HAIKU, "tier": "haiku",
                    "reason": "courriel court"}
        if ancien:
            return {"model": MODEL_HAIKU, "tier": "haiku",
                    "reason": "courriel ancien"}
        return {"model": MODEL_SONNET, "tier": "sonnet",
                "reason": "courriel long récent"}

    if st == "fil":
        # Les fils sont rarement triviaux — garder Sonnet sauf lot ancien massif.
        if ancien:
            return {"model": MODEL_HAIKU, "tier": "haiku",
                    "reason": "fil ancien"}
        return {"model": MODEL_SONNET, "tier": "sonnet", "reason": "fil"}

    if st == "document":
        if content_length > THRESHOLDS["document_long_chars"]:
            return {"model": MODEL_SONNET, "tier": "sonnet",
                    "reason": "document long"}
        if ancien:
            return {"model": MODEL_HAIKU, "tier": "haiku",
                    "reason": "document ancien"}
        return {"model": MODEL_SONNET, "tier": "sonnet",
                "reason": "document récent"}

    # Synthèses : Sonnet par défaut (rédaction narrative). Digest et MOC
    # peuvent passer sur Haiku car la trame est très structurée.
    if st in ("digest", "moc"):
        return {"model": MODEL_HAIKU, "tier": "haiku",
                "reason": f"{st} — trame structurée"}
    if st in ("fiche", "chronologie"):
        return {"model": MODEL_SONNET, "tier": "sonnet",
                "reason": f"{st} — rédaction narrative"}

    # Fallback prudent.
    return {"model": MODEL_SONNET, "tier": "sonnet",
            "reason": f"source_type inconnu ({source_type}) — Sonnet par défaut"}


def summarize_batch(choices: list[dict]) -> dict:
    """Agréger les choix d'un lot pour afficher un tradeoff à l'utilisateur.

    Renvoie ``{total, sonnet: {n, share}, haiku: {n, share}, reasons: {...}}``
    où ``share`` est une proportion dans [0, 1]. Utilisé par la skill resumer
    pour formuler une phrase du genre :

        « 42 résumés à faire : 8 en Sonnet (documents récents), 34 en
          Haiku (courriels courts et notes anciennes). »
    """
    if not choices:
        return {"total": 0}
    n_sonnet = sum(1 for c in choices if c.get("tier") == "sonnet")
    n_haiku = sum(1 for c in choices if c.get("tier") == "haiku")
    total = len(choices)
    reasons: dict[str, int] = {}
    for c in choices:
        reasons[c.get("reason", "?")] = reasons.get(c.get("reason", "?"), 0) + 1
    return {
        "total": total,
        "sonnet": {"n": n_sonnet, "share": round(n_sonnet / total, 3)},
        "haiku": {"n": n_haiku, "share": round(n_haiku / total, 3)},
        "reasons": reasons,
    }
