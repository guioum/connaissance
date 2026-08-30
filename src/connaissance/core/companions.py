"""core/companions : les fichiers qui doivent suivre un `.md` quand il bouge.

Une transcription n'est pas seule dans son dossier. Elle a jusqu'à deux
familles de **compagnons**, tous désignés par des chemins RELATIFS à son
dossier — donc tous cassés dès que le `.md` déménage sans eux :

- ``<stem>_annotations.json`` — la description des images extraites par
  ``--extract-images``, posée à côté du `.md` ;
- ``Attachments/<fichier>`` — les images elles-mêmes, référencées à la fois
  par le corps du `.md` (``(./Attachments/x.jpg)``) et par le champ ``path``
  de chaque entrée du JSON.

Historiquement chaque chemin de déplacement redécouvrait cette règle, ou ne
la découvrait pas : ``organize`` emmenait les compagnons,
``relocate_document`` — utilisé par ``classify`` et ``entities`` — ne les
connaissait pas. Résultat mesuré le 2026-08-30 : **1 210 JSON orphelins et
5 896 images non référencées**, dont 848 laissés par les runs ``classify``.
D'où ce module : une seule définition de « ce qui suit », que les deux
chemins appellent.

Le partage est réel — deux `.md` d'un même dossier peuvent citer la même
image. :func:`companion_moves` le signale par ``shared`` pour que l'appelant
copie au lieu de déplacer, plutôt que d'arracher un fichier encore utilisé.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ANNOTATIONS_SUFFIX = "_annotations.json"
ATTACHMENTS_DIR = "Attachments"

# Une référence d'attachement en markdown : `(./Attachments/x.jpg)`,
# `(Attachments/x.jpg)`, `(/Attachments/x.jpg)`. Borné à la ligne : un lien
# dont la cible a été tronquée par une parenthèse n'a pas de « ) » fermante à
# sa place, et un motif non borné déborderait sur tout le reste du fichier.
_ATT_REF_PATTERN = re.compile(r"\(\.?/?Attachments/([^)\n]*)\)")

# Le LIBELLÉ d'une image écrite par ce dépôt est le nom du fichier lui-même
# (`![x.jpg](./Attachments/x.jpg)`). Quand une parenthèse non échappée a
# tronqué la cible, le libellé, lui, est intact — il reste la seule trace du
# nom complet dans le fichier. On s'en sert pour relire les liens écrits AVANT
# `encode_link`, sans avoir à deviner.
_ATT_LABEL_PATTERN = re.compile(r"!\[([^\]\n]+)\]\(\.?/?Attachments/")


def encode_link(nom: str) -> str:
    """Nom de fichier rendu sûr dans la cible d'un lien markdown.

    Une parenthèse non échappée ferme le lien : `](./Attachments/Guide(1).jpg)`
    se lit comme la cible `./Attachments/Guide(1` suivie de texte. L'image ne
    s'affiche nulle part, et aucun outil ne signale rien — 154 liens de la base
    étaient dans ce cas, tous sur des documents dont le NOM porte une
    parenthèse (« Manual(1).pdf », « Guide (Temporary Workers).pdf »).

    On encode les seules parenthèses, en pourcent : `%28`/`%29` est compris de
    tous les lecteurs markdown et laisse le motif de relecture inchangé. La
    forme CommonMark à chevrons (`](<...>)`) marcherait aussi, mais imposerait
    de réécrire chaque lecteur de liens du dépôt.
    """
    return nom.replace("(", "%28").replace(")", "%29")


def decode_link(cible: str) -> str:
    """Nom de fichier réel derrière une cible de lien. Inverse d'``encode_link``.

    Doit rester tolérant : la base contient des liens écrits AVANT
    l'encodage, non encodés — et un nom peut légitimement contenir « % ».
    """
    return cible.replace("%28", "(").replace("%29", ")")


def annotations_path(md_path: Path) -> Path:
    """Chemin du JSON d'annotations compagnon d'un `.md` (existant ou non)."""
    return md_path.with_name(md_path.stem + ANNOTATIONS_SUFFIX)


def _from_markdown(md_path: Path) -> set[str]:
    try:
        txt = md_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    cibles = {decode_link(c) for c in _ATT_REF_PATTERN.findall(txt) if c}
    # Un libellé qui n'est PAS déjà couvert par une cible complète signale une
    # cible tronquée : on retient le libellé, qui porte le nom entier, et on
    # écarte le fragment — il ne désigne aucun fichier et ferait compter une
    # référence « cassée » là où le nom est parfaitement connu.
    tronquees: set[str] = set()
    for libelle in _ATT_LABEL_PATTERN.findall(txt):
        if libelle in cibles:
            continue
        prefixes = {c for c in cibles if c and libelle.startswith(c)}
        if prefixes:
            cibles.add(libelle)
            tronquees |= prefixes
    return cibles - tronquees


def _from_annotations(json_path: Path) -> set[str]:
    """Noms de fichiers cités par le champ ``path`` d'un JSON d'annotations.

    Le JSON est écrit par un modèle : il peut être tronqué ou mal formé. Un
    JSON illisible ne doit pas empêcher le `.md` de bouger — on renvoie
    l'ensemble vide et le fichier suivra quand même en tant que compagnon.
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    out: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        p = str(entry.get("path") or "")
        if ATTACHMENTS_DIR + "/" in p:
            out.add(p.rsplit(ATTACHMENTS_DIR + "/", 1)[1])
    return out


def referenced_attachments(md_path: Path) -> set[str]:
    """Noms de fichiers d'``Attachments/`` que ce `.md` fait vivre.

    Union du corps markdown ET du JSON d'annotations : une image peut être
    citée par l'un sans l'autre — le JSON décrit des images que le corps
    n'affiche pas toujours, et un corps réécrit peut citer une image que le
    JSON ne décrit plus. Prendre l'union évite d'abandonner un fichier
    encore désigné par une des deux faces.
    """
    return _from_markdown(md_path) | _from_annotations(annotations_path(md_path))


def companion_moves(md_src: Path, md_dst: Path) -> list[tuple[Path, Path, bool]]:
    """Les déplacements à faire suivre à un `.md` qui va de ``src`` à ``dst``.

    Retourne des triplets ``(source, destination, shared)`` :

    - le JSON d'annotations, renommé sur le nouveau stem — sans quoi il ne
      serait plus jamais retrouvé, l'appariement se faisant par le NOM ;
    - chaque image d'``Attachments/`` référencée, ``shared=True`` quand un
      autre `.md` resté dans le dossier source la cite encore (l'appelant
      copie alors au lieu de déplacer).

    Ne touche pas au disque et n'exige pas que ``md_src`` existe encore : le
    `.md` peut déjà avoir été déplacé quand on appelle (c'est le cas dans
    ``relocate_document``, où le graphe bouge avant ses compagnons). Les
    références sont donc lues à la destination si la source a disparu.
    """
    lecture = md_src if md_src.exists() else md_dst
    moves: list[tuple[Path, Path, bool]] = []

    ann_src = annotations_path(md_src)
    if ann_src.exists():
        moves.append((ann_src, annotations_path(md_dst), False))

    noms = referenced_attachments(lecture)
    if not noms:
        return moves

    att_src = md_src.parent / ATTACHMENTS_DIR
    att_dst = md_dst.parent / ATTACHMENTS_DIR
    if not att_src.is_dir() or att_src == att_dst:
        return moves

    # Qui d'autre, resté dans le dossier source, cite encore ces images ?
    encore_cites: set[str] = set()
    for autre in att_src.parent.glob("*.md"):
        if autre in (md_src, md_dst):
            continue
        encore_cites |= referenced_attachments(autre)

    for nom in sorted(noms):
        f_src = att_src / nom
        if f_src.exists() and not (att_dst / nom).exists():
            moves.append((f_src, att_dst / nom, nom in encore_cites))
    return moves
