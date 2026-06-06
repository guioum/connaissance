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


def chercher_alias(identifiant: str, synthese_dir: Path | None = None) -> str | None:
    """Chercher un identifiant dans les aliases des fiches existantes.

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

    for type_dir in ("personnes", "organismes"):
        type_path = synthese_dir / type_dir
        if not type_path.exists():
            continue
        for fiche in type_path.rglob("fiche.md"):
            content = fiche.read_text(encoding="utf-8")
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
            for alias in (fm.get("aliases") or []):
                alias_str = str(alias)
                if alias_str.startswith("*@"):
                    # Pattern domaine : *@orange.fr matche facturation@orange.fr
                    domain = alias_str[2:].lower()
                    if identifiant_lower.endswith(f"@{domain}"):
                        return f"{type_dir}/{fiche.parent.name}"
                elif alias_str.lower() == identifiant_lower:
                    return f"{type_dir}/{fiche.parent.name}"
    return None


