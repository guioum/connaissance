"""Module commands/manifest : patch de manifestes d'organisation.

Permet à Claude d'appliquer des mutations à un manifeste JSON produit
par `organize.plan()` sans réécrire le fichier manuellement. Deux modes :

1. `--patches` : liste de patches JSON `[{id, set, delete?}, ...]`.
2. `--filter FIELD=VAL --set k=v,...` : patch en masse sur toutes les
   entrées qui matchent le prédicat.

Le manifeste peut être un tableau direct OU une enveloppe
`{entrees: [...], total, auto, alias_match, a_confirmer}` — les deux
formats sont acceptés par `organize.apply`.
"""

from __future__ import annotations

import json
from pathlib import Path

from connaissance.core.paths import BASE_PATH


CONNAISSANCE_ROOT = BASE_PATH / "Connaissance"


def _normalize_candidates(raw: str) -> list[str]:
    """Candidats de match pour un ``resume_path`` ou ``id`` fourni.

    Les manifestes stockent typiquement des chemins absolus
    (``/Users/.../Connaissance/Résumés/...``), mais Claude envoie souvent des
    chemins relatifs (``Résumés/...``) ou préfixés (``Connaissance/Résumés/...``).
    On retourne la liste de variantes à essayer dans l'ordre.
    """
    if not raw:
        return []
    raw_str = str(raw).strip()
    out: list[str] = [raw_str]
    # Absolu → ajouter variantes relatives
    if raw_str.startswith("/"):
        try:
            rel = str(Path(raw_str).relative_to(CONNAISSANCE_ROOT))
            out.append(rel)
        except ValueError:
            pass
        try:
            rel_base = str(Path(raw_str).relative_to(BASE_PATH))
            out.append(rel_base)
        except ValueError:
            pass
    else:
        # Relatif → ajouter variantes absolues
        out.append(str(CONNAISSANCE_ROOT / raw_str))
        out.append(str(BASE_PATH / raw_str))
    return out


def _load_manifest(path: Path) -> tuple[dict | None, list[dict]]:
    """Charger un manifeste. Retourne (enveloppe_ou_None, entries)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "entrees" in data:
        entries = data["entrees"] if isinstance(data["entrees"], list) else []
        return data, entries
    if isinstance(data, list):
        return None, data
    return None, []


def _save_manifest(path: Path, envelope: dict | None, entries: list[dict]) -> None:
    """Écrire le manifeste au même format que l'entrée."""
    if envelope is not None:
        envelope["entrees"] = entries
        # Recalculer les compteurs status
        for status in ("auto", "alias_match", "a_confirmer"):
            envelope[status] = sum(1 for e in entries if e.get("status") == status)
        envelope["total"] = len(entries)
        out: object = envelope
    else:
        out = entries
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")


def _match_filter(entry: dict, predicates: dict) -> bool:
    return all(str(entry.get(k)) == str(v) for k, v in predicates.items())


def _parse_kv(spec: str) -> dict:
    """Parser 'k1=v1,k2=v2' en dict."""
    if not spec:
        return {}
    out = {}
    for pair in spec.split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# --- API publique ---


def patch(manifest_path: str,
          patches: list[dict] | None = None,
          filter_expr: str | None = None,
          set_expr: str | dict | None = None,
          delete_filter: str | None = None) -> dict:
    """Appliquer des patches à un manifeste (schema ManifestPatchResult).

    Parameters
    ----------
    manifest_path : str
        Chemin vers le manifeste JSON.
    patches : list[dict] | None
        Liste de patches `[{id, set: {...}, delete: true?}, ...]` où `id`
        est soit le champ `id` soit le `resume_path` de l'entrée à modifier.
    filter_expr : str | None
        Filtre `"status=a_confirmer,source=courriels"` pour patch en masse.
    set_expr : str | dict | None
        Nouvelle valeur à appliquer aux entrées matchées par `filter_expr`.
        Format str `"entity_type=organismes,entity_slug=videotron"` ou dict.
    delete_filter : str | None
        Filtre pour supprimer des entrées en masse.
    """
    path = Path(manifest_path)
    if not path.exists():
        return {
            "manifest_path": str(path),
            "patches": [],
            "updated": 0,
            "error": f"manifeste introuvable : {path}",
        }

    envelope, entries = _load_manifest(path)
    applied_patches: list[dict] = []
    not_found: list[dict] = []
    updated = 0

    def _rebuild_index() -> dict[str, int]:
        """Reconstruire l'index {clé_normalisée → idx d'entrée}.

        Chaque entrée est indexée sur toutes ses clés plausibles
        (``id``, ``resume_path`` — plus leurs variantes absolue/relative via
        ``_normalize_candidates``). Ça permet au patch d'utiliser n'importe
        quelle forme.
        """
        idx_map: dict[str, int] = {}
        for i, entry in enumerate(entries):
            for key_field in ("id", "resume_path"):
                val = entry.get(key_field)
                if not val:
                    continue
                for cand in _normalize_candidates(val):
                    # premier arrive gagne — pas d'écrasement
                    idx_map.setdefault(cand, i)
        return idx_map

    # Patches ciblés par id
    if patches:
        by_id = _rebuild_index()
        for p in patches:
            target = p.get("id") or p.get("resume_path")
            if not target:
                not_found.append({"patch": p, "reason": "id et resume_path absents"})
                continue
            found_idx = None
            for cand in _normalize_candidates(target):
                if cand in by_id:
                    found_idx = by_id[cand]
                    break
            if found_idx is None:
                not_found.append({"target": target, "reason": "aucune entrée ne matche"})
                continue
            if p.get("delete"):
                entries.pop(found_idx)
                by_id = _rebuild_index()
                applied_patches.append({"id": target, "delete": True})
                updated += 1
                continue
            updates = p.get("set") or {}
            for k, v in updates.items():
                entries[found_idx][k] = v
            applied_patches.append({"id": target, "set": updates})
            updated += 1

    # Patch en masse par filtre
    if filter_expr and set_expr is not None:
        predicates = _parse_kv(filter_expr)
        new_values = set_expr if isinstance(set_expr, dict) else _parse_kv(set_expr)
        for entry in entries:
            if _match_filter(entry, predicates):
                for k, v in new_values.items():
                    entry[k] = v
                updated += 1
                applied_patches.append({
                    "filter": predicates, "set": new_values,
                })

    # Suppression en masse par filtre
    if delete_filter:
        predicates = _parse_kv(delete_filter)
        before = len(entries)
        entries[:] = [e for e in entries if not _match_filter(e, predicates)]
        deleted = before - len(entries)
        if deleted:
            applied_patches.append({"filter": predicates, "delete": True, "count": deleted})
            updated += deleted

    _save_manifest(path, envelope, entries)

    result: dict = {
        "manifest_path": str(path),
        "patches": applied_patches,
        "updated": updated,
    }
    if not_found:
        result["not_found"] = not_found
    return result
