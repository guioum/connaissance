"""Détection de quasi-doublons d'ENTITÉS (registre) — pur, sans dépendance.

Le classement par entité se fragmente quand le même organisme/personne apparaît
sous des slugs légèrement différents : ``ville-de-montreal`` vs ``ville-montreal``,
``monteillet-conseil`` vs ``monteillet-conseil-inc``, ``banque-nationale`` vs son
acronyme ``bnc``. Ce module propose des **paires candidates à la fusion** par
signaux purement lexicaux (jamais d'auto-fusion : un humain tranche).

Signaux (sur les slugs ``a-b-c`` tokenisés sur ``-``), au sein d'un même type :
- **containment** : les tokens de l'un sont inclus dans l'autre (suffixe ``inc``…)
- **token Jaccard** élevé (≥ 0.5) : fort recouvrement de tokens
- **edit distance** faible (≤ 2) : fautes de frappe / variantes
- **acronyme** : les initiales des tokens de l'un == le slug de l'autre
"""
from __future__ import annotations

# Tokens vides de sens pour la comparaison (formes juridiques, articles).
_STOP = {"inc", "ltee", "ltd", "llc", "co", "cie", "de", "du", "des", "la",
         "le", "les", "et", "the", "of"}


def _tokens(slug: str) -> list[str]:
    return [t for t in slug.split("-") if t]


def _content_tokens(slug: str) -> set[str]:
    """Tokens signifiants (hors mots vides)."""
    return {t for t in _tokens(slug) if t not in _STOP} or set(_tokens(slug))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein (itératif, O(len(a)·len(b)))."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _acronym(slug: str) -> str:
    """Initiales des tokens signifiants : ``banque-nationale`` → ``bn``."""
    return "".join(t[0] for t in _tokens(slug) if t and t not in _STOP)


def _is_year(tok: str) -> bool:
    return len(tok) == 4 and tok.isdigit() and tok[:2] in ("19", "20")


def _differ_only_by_year(slug_a: str, slug_b: str) -> bool:
    """Vrai si les slugs ne diffèrent QUE par une année (variantes annuelles
    distinctes : ``impots-2023`` vs ``impots-2024``, ``objectifs-2025`` vs
    ``objectifs-2026``) — à NE PAS fusionner."""
    ya = [t for t in _tokens(slug_a) if _is_year(t)]
    yb = [t for t in _tokens(slug_b) if _is_year(t)]
    if not ya and not yb:
        return False
    rest_a = [t for t in _tokens(slug_a) if not _is_year(t)]
    rest_b = [t for t in _tokens(slug_b) if not _is_year(t)]
    return rest_a == rest_b and ya != yb


def pair_signal(slug_a: str, slug_b: str) -> dict | None:
    """Signal de fusion entre deux slugs, ou ``None`` si rien de probant.

    Retourne ``{score: float, reasons: [str]}``. Score ∈ [0,1], heuristique.
    """
    if slug_a == slug_b:
        return None
    # Variantes annuelles (impots-2023/2024, objectifs-2025/2026) = entités
    # distinctes par conception, jamais à fusionner.
    if _differ_only_by_year(slug_a, slug_b):
        return None
    ta, tb = _content_tokens(slug_a), _content_tokens(slug_b)
    reasons: list[str] = []
    score = 0.0

    if ta and tb and (ta <= tb or tb <= ta):
        reasons.append("containment")
        score = max(score, 0.9)

    jac = _jaccard(ta, tb)
    if jac >= 0.5:
        reasons.append(f"jaccard={jac:.2f}")
        score = max(score, jac)

    ed = _edit_distance(slug_a, slug_b)
    if ed <= 2 and max(len(slug_a), len(slug_b)) >= 4:
        reasons.append(f"edit={ed}")
        score = max(score, 1.0 - ed / max(len(slug_a), len(slug_b)))

    # Acronyme : un slug == initiales de l'autre (tolère 1 lettre en plus, ex.
    # 'bnc' vs acronyme 'bn' de banque-nationale).
    aa, ab = _acronym(slug_a), _acronym(slug_b)
    sa, sb = slug_a.replace("-", ""), slug_b.replace("-", "")
    for acr, other in ((aa, sb), (ab, sa)):
        if len(acr) >= 2 and (other == acr or
                              (other.startswith(acr) and len(other) - len(acr) <= 1)):
            reasons.append("acronym")
            score = max(score, 0.7)
            break

    if not reasons:
        return None
    return {"score": round(score, 3), "reasons": reasons}


def find_candidates(entities: list[dict]) -> list[dict]:
    """Paires candidates à la fusion parmi ``entities`` (mêmes type).

    ``entities`` : ``[{entity_type, entity_slug, name?, count?}]``. Retourne
    ``[{type, a, b, score, reasons}]`` trié par score décroissant. O(n²) par
    type — adapté à un registre de centaines d'entités.
    """
    by_type: dict[str, list[dict]] = {}
    for e in entities:
        by_type.setdefault(e["entity_type"], []).append(e)

    out: list[dict] = []
    for etype, items in by_type.items():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                sig = pair_signal(a["entity_slug"], b["entity_slug"])
                if sig is None:
                    continue
                out.append({
                    "type": etype,
                    "a": {"slug": a["entity_slug"], "name": a.get("name"),
                          "count": a.get("count")},
                    "b": {"slug": b["entity_slug"], "name": b.get("name"),
                          "count": b.get("count")},
                    "score": sig["score"],
                    "reasons": sig["reasons"],
                })
    out.sort(key=lambda p: -p["score"])
    return out
