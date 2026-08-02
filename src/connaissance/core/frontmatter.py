"""Parsing et écriture unifiés du frontmatter YAML des ``.md``.

Historiquement, une dizaine de modules réimplémentaient le même idiome de
découpe — avec deux conventions divergentes (``find("\\n---", 4)`` vs
``split("---", 2)``) qui ne traitaient pas pareil un ``---`` dans le corps.
Convention canonique ici : le frontmatter se termine au premier ``\\n---``
après l'ouverture (un ``---`` dans une valeur ou dans le corps n'est jamais
un délimiteur — cf. les noms de fichiers organisés ``date---entité---titre``).

Les écritures passent par :func:`connaissance.core.fsio.atomic_write_text`
(le frontmatter est la source de vérité du pipeline, jamais de troncature).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from connaissance.core.fsio import atomic_write_text


def split_frontmatter(content: str) -> tuple[str, str] | None:
    """Découper ``content`` en ``(fm_text, body)``.

    ``body`` conserve tout ce qui suit le ``\\n---`` fermant (y compris son
    éventuel saut de ligne de tête) : ``dump_frontmatter(fm, body)`` reproduit
    alors le fichier à l'identique. Retourne ``None`` si pas de frontmatter.
    """
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 4)
    if end < 0:
        return None
    return content[4:end], content[end + 4:]


def parse_frontmatter(content: str) -> dict | None:
    """Dict du frontmatter.

    ``{}`` si le bloc est présent mais vide ; ``None`` si absent, YAML
    invalide, ou racine non-mapping (liste, scalaire).
    """
    parts = split_frontmatter(content)
    if parts is None:
        return None
    try:
        fm = yaml.safe_load(parts[0])
    except yaml.YAMLError:
        return None
    if fm is None:
        return {}
    return fm if isinstance(fm, dict) else None


def read_frontmatter(path: Path | str) -> dict | None:
    """Frontmatter d'un fichier, ``None`` si illisible ou sans frontmatter."""
    try:
        content = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return parse_frontmatter(content)


def dump_frontmatter(fm: dict, body: str) -> str:
    """Recomposer un document ``---\\n<yaml>\\n---<body>``.

    ``body`` est concaténé tel quel — utiliser le body retourné par
    :func:`split_frontmatter` pour un round-trip fidèle.
    """
    fm_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm_text}\n---{body}"


def write_frontmatter(path: Path | str, fm: dict, body: str) -> None:
    """Écrire (atomiquement) un document recomposé frontmatter + body."""
    atomic_write_text(path, dump_frontmatter(fm, body))


def body_sha256(content: str) -> str:
    """Hash SHA-256 du **corps** d'un ``.md`` (frontmatter exclu).

    La vérité de CONTENU, insensible aux retouches de métadonnées :
    ``register-batch``/``reindex-db`` réécrivent le frontmatter des
    transcriptions sans toucher au texte OCR, ce qui périmait à tort les
    résumés tant que la détection comparait des mtimes (constaté le
    2026-08-02 : 8 075 « périmés » dont ~6 600 de bruit). Le corps est
    ``strip()``-é pour neutraliser les sauts de ligne de bord.
    """
    parts = split_frontmatter(content)
    body = parts[1] if parts is not None else content
    return "sha256:" + hashlib.sha256(body.strip().encode("utf-8")).hexdigest()
