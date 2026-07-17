"""Résolution d'entités : slug, nommage, recherche d'aliases.

Fonctions déterministes utilisées par cli/commands/organize.py et les skills.

Usage en module :
    from connaissance.core.resolution import construire_slug, construire_nom_fichier, chercher_alias
"""

import re
import unicodedata
from pathlib import Path

import yaml

from connaissance.core.paths import BASE_PATH


# Caractères gardés dans un slug : chiffres, lettres ASCII et lettres accentuées
# françaises (latin-1 minuscules + œ). Décision projet : on CONSERVE les accents
# dans les noms de fichiers et de dossiers (pas de translittération é→e). Tout le
# reste (espaces, ponctuation) devient un tiret.
_SLUG_DROP = re.compile(r"[^0-9a-zà-ÿœ]+")


def slugify(text: str) -> str:
    """Slug minuscule-tirets en **conservant les accents** (normalisé NFC).

    NFC : macOS écrit les noms en NFD (accents décomposés) ; on canonicalise
    pour que le slug soit une clé stable quelle que soit la source du nom.

    >>> slugify("Banque de développement du Canada")
    'banque-de-développement-du-canada'
    >>> slugify("Hôpital Sainte-Justine")
    'hôpital-sainte-justine'
    """
    s = unicodedata.normalize("NFC", text or "").lower()
    s = _SLUG_DROP.sub("-", s).strip("-")
    return re.sub(r"-{2,}", "-", s)


def construire_slug(name: str) -> str:
    """Slug d'un nom d'entité (minuscules + tirets, accents conservés).

    >>> construire_slug("Marie Lefebvre")
    'marie-lefebvre'
    >>> construire_slug("Banque Nationale")
    'banque-nationale'
    """
    return slugify(name)


def construire_nom_fichier(date: str, title: str) -> str:
    """Construire le nom de fichier après organisation.

    Format : YYYY-MM-DD description-slug (accents conservés).

    >>> construire_nom_fichier("2025-09-01", "Avis de cotisation 2025")
    '2025-09-01 avis-de-cotisation-2025'
    """
    slug = slugify(title)[:50].rstrip("-")
    return f"{date} {slug}"


# Cache par dossier Synthèse : (aliases exacts {alias_lower: "type/slug"},
# patterns domaine [(domain_lower, "type/slug")]). Le CLI est un process
# court : relire et re-parser TOUTES les fiches à chaque appel (une fois par
# document dans organize/classify) était le principal coût des gros lots.
_ALIAS_CACHE: dict[str, tuple[dict[str, str], list[tuple[str, str]]]] = {}


def _load_alias_index(synthese_dir: Path) -> tuple[dict[str, str],
                                                   list[tuple[str, str]]]:
    key = str(synthese_dir)
    cached = _ALIAS_CACHE.get(key)
    if cached is not None:
        return cached
    exact: dict[str, str] = {}
    domains: list[tuple[str, str]] = []
    for type_dir in ("personnes", "organismes"):
        type_path = synthese_dir / type_dir
        if not type_path.exists():
            continue
        for fiche in type_path.rglob("fiche.md"):
            try:
                content = fiche.read_text(encoding="utf-8")
            except OSError:
                continue
            if not content.startswith("---"):
                continue
            try:
                fm_text = content.split("---", 2)[1]
                fm = yaml.safe_load(fm_text)
            except (IndexError, yaml.YAMLError):
                continue
            # frontmatter vide -> None ; `aliases:` vide -> None (pas []).
            if not isinstance(fm, dict):
                continue
            entity = f"{type_dir}/{fiche.parent.name}"
            for alias in (fm.get("aliases") or []):
                alias_str = str(alias)
                if alias_str.startswith("*@"):
                    domains.append((alias_str[2:].lower(), entity))
                else:
                    # Premier arrivé gagne (ordre de parcours stable), comme
                    # le scan historique qui retournait le premier match.
                    exact.setdefault(alias_str.lower(), entity)
    _ALIAS_CACHE[key] = (exact, domains)
    return exact, domains


def invalidate_alias_cache() -> None:
    """Vider le cache d'aliases (après une écriture de fiche dans le process)."""
    _ALIAS_CACHE.clear()


def chercher_alias(identifiant: str, synthese_dir: Path | None = None) -> str | None:
    """Chercher un identifiant dans les aliases des fiches existantes.

    Les fiches sont lues UNE fois par process puis mises en cache (le CLI est
    un process court) — appeler ``invalidate_alias_cache()`` si des fiches
    sont modifiées dans le même process.

    Args:
        identifiant: nom, email ou domaine à chercher
        synthese_dir: chemin vers ~/Connaissance/Synthèse/ (auto-détecté si None)

    Returns:
        "type/slug" si trouvé, None sinon
    """
    if synthese_dir is None:
        synthese_dir = BASE_PATH / "Connaissance" / "Synthèse"

    if not synthese_dir.exists():
        return None

    identifiant_lower = identifiant.lower()
    exact, domains = _load_alias_index(synthese_dir)

    hit = exact.get(identifiant_lower)
    if hit:
        return hit
    for domain, entity in domains:
        # Pattern domaine : *@orange.fr matche facturation@orange.fr
        if identifiant_lower.endswith(f"@{domain}"):
            return entity
    return None


