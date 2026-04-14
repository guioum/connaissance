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


def construire_slug(name: str) -> str:
    """Construire un slug depuis un nom d'entité.

    Règles :
    - Tout en minuscules
    - Accents supprimés (é→e, ç→c, etc.)
    - Espaces et caractères spéciaux remplacés par des tirets
    - Pas de tirets en début/fin ni de tirets doubles

    >>> construire_slug("Marie Lefebvre")
    'marie-lefebvre'
    >>> construire_slug("Banque Nationale")
    'banque-nationale'
    >>> construire_slug("Orange")
    'orange'
    """
    slug = unicodedata.normalize("NFD", name.lower())
    slug = slug.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    # Supprimer les tirets doubles
    slug = re.sub(r"-{2,}", "-", slug)
    return slug


def construire_nom_fichier(date: str, title: str) -> str:
    """Construire le nom de fichier après organisation.

    Format : YYYY-MM-DD description-slug

    >>> construire_nom_fichier("2025-09-01", "Avis de cotisation 2025")
    '2025-09-01 avis-de-cotisation-2025'
    >>> construire_nom_fichier("2024-01-15", "Facture janvier Orange")
    '2024-01-15 facture-janvier-orange'
    """
    slug = unicodedata.normalize("NFD", title.lower())
    slug = slug.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug[:50].rstrip("-")
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
            for alias in fm.get("aliases", []):
                alias_str = str(alias)
                if alias_str.startswith("*@"):
                    # Pattern domaine : *@orange.fr matche facturation@orange.fr
                    domain = alias_str[2:].lower()
                    if identifiant_lower.endswith(f"@{domain}"):
                        return f"{type_dir}/{fiche.parent.name}"
                elif alias_str.lower() == identifiant_lower:
                    return f"{type_dir}/{fiche.parent.name}"
    return None


