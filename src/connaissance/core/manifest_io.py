"""core/manifest_io : lecture homogène des manifestes plan→apply.

Toutes les commandes mutatrices (organize, classify, optimize, emails cleanup)
écrivent un manifeste JSON que leur `apply` relit. Historiquement chacune avait
sa propre clé de liste (`entrees`/`entries`/`items`) et son propre code de
lecture défensif, ré-implémenté à l'identique. Ce module centralise le contrat :

- ``load_entries(path)`` — loader tolérant qui rend ``(enveloppe|None, entries)``
  quelle que soit la clé de liste ou un tableau nu ;
- ``unwrap(data, *keys)`` — déballe ``{key: X}`` → ``X`` pour les fichiers de
  transit (``{"results": [...]}``, ``{"requests": [...]}``) éventuellement nus ;
- ``unique_dest(path)`` — anti-collision de destination (``nom (2).pdf``).

Zéro dépendance, zéro accès DB : pur I/O JSON + Path.
"""
from __future__ import annotations

import json
from pathlib import Path

# Clés de liste reconnues, par ordre de priorité (compat historique :
# `entries` = classify, `entrees` = organize/manifest, `items` = emails cleanup).
LIST_KEYS = ("entries", "entrees", "items")


def load_entries(path, *, list_keys: tuple[str, ...] = LIST_KEYS
                 ) -> tuple[dict | None, list[dict]]:
    """Charger un manifeste → ``(enveloppe_ou_None, entries)``.

    Accepte : (a) une liste nue → ``(None, liste)`` ; (b) un dict enveloppe
    portant l'une des ``list_keys`` → ``(dict, sa_liste)`` ; (c) tout autre
    dict → ``(dict, [])``. Lève les erreurs JSON habituelles si le fichier
    est illisible/malformé.
    """
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(data, list):
        return None, data
    if isinstance(data, dict):
        for k in list_keys:
            if k in data:
                v = data[k]
                return data, v if isinstance(v, list) else []
        return data, []
    return None, []


def unwrap(data, *keys):
    """Déballer ``{key: X}`` → ``X`` pour la 1re ``key`` présente, sinon ``data``.

    Idiome pour les fichiers de transit qui peuvent être soit ``{"results": [...]}``
    soit directement une liste. Sans ``keys``, renvoie ``data`` inchangé.
    """
    if isinstance(data, dict):
        for k in keys:
            if k in data:
                return data[k]
    return data


def unique_dest(dst) -> Path:
    """Éviter d'écraser : ``nom.pdf`` → ``nom (2).pdf`` si la cible existe."""
    dst = Path(dst)
    if not dst.exists():
        return dst
    stem, suf = dst.stem, dst.suffix
    i = 2
    while True:
        cand = dst.with_name(f"{stem} ({i}){suf}")
        if not cand.exists():
            return cand
        i += 1
