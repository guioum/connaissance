"""Helper partagé : écriture optionnelle du payload JSON dans un fichier.

Certains outils (``documents scan``, ``notes scan``, ``summarize prepare``) peuvent
produire des sorties volumineuses (centaines de Ko à plusieurs Mo) qui saturent
le contexte des assistants MCP. Ce helper implémente un pattern commun : si
``output_file`` est fourni, écrire le payload dans ce fichier et retourner un
récapitulatif compact ; sinon retourner le payload inline.

Usage depuis un module ``commands/`` :

    from connaissance.core.output_file import write_or_inline

    def scan(..., output_file=None):
        payload = build_heavy_payload()
        return write_or_inline(
            payload,
            output_file=output_file,
            summary_fn=lambda p: {
                "total": len(p["items"]),
                "by_category": count_by_category(p["items"]),
            },
        )
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


def write_or_inline(
    payload: dict,
    *,
    output_file: str | None,
    summary_fn: Callable[[dict], dict] | None = None,
) -> dict:
    """Si ``output_file`` est fourni, écrire ``payload`` en JSON et retourner
    un récap compact ; sinon retourner ``payload`` tel quel.

    Le récap contient toujours ``output_file`` (chemin résolu) et
    ``total_bytes`` (taille du JSON écrit). Les clés du dict ``summary_fn``
    (si fourni) sont fusionnées dans le récap — utile pour remonter par
    exemple ``{total: N, by_type: {...}}`` sans avoir à lire le fichier.
    """
    if not output_file:
        return payload

    out_path = Path(output_file).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # JSON compact (non-indenté) pour minimiser la taille disque — les
    # consommateurs re-parsent programmatiquement.
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    summary: dict[str, Any] = {
        "output_file": str(out_path),
        "total_bytes": out_path.stat().st_size,
    }
    if summary_fn is not None:
        summary.update(summary_fn(payload))
    return summary
