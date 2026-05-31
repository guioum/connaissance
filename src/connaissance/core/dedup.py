"""Détection de quasi-doublons par SimHash texte (pur Python, sans dépendance).

SimHash 64 bits sur shingles de mots. Validé en calibration sur les
transcriptions OCR : sépare les vrais doublons (même document transcrit ou
classé plusieurs fois → distance de Hamming ~0) des documents distincts mais
templatés (reçus fiscaux annuels, fiches de paie, relevés mensuels) qui ne
collident qu'au-delà du point d'opération.

Le perceptual hash d'image (pHash sur la page 1) avait été écarté : sur un
corpus de documents templatés il fusionne des documents distincts dès le seuil
0, car la page 1 encode le gabarit, pas l'identité. Le texte n'a pas ce défaut.

Point d'opération recommandé : Hamming <= 3 / 64.
"""
import hashlib
import re

_FRONTMATTER = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_NONWORD = re.compile(r"[^a-z0-9]+")

SIMHASH_BITS = 64
DEFAULT_SHINGLE = 4
DEFAULT_THRESHOLD = 3


def _tokens(text: str) -> list[str]:
    """Retire le frontmatter YAML (métadonnées), minuscule, mots alphanum."""
    text = _FRONTMATTER.sub("", text, count=1)
    return _NONWORD.sub(" ", text.lower()).split()


def simhash_text(text: str, k: int = DEFAULT_SHINGLE) -> int | None:
    """SimHash 64 bits du texte sur shingles de ``k`` mots consécutifs.

    Retourne ``None`` si le texte est vide (rien à hasher).
    """
    words = _tokens(text)
    if not words:
        return None
    if len(words) < k:
        shingles = [" ".join(words)]
    else:
        shingles = [" ".join(words[i:i + k]) for i in range(len(words) - k + 1)]
    v = [0] * SIMHASH_BITS
    for sh in shingles:
        hv = int.from_bytes(
            hashlib.blake2b(sh.encode(), digest_size=8).digest(), "big")
        for i in range(SIMHASH_BITS):
            v[i] += 1 if (hv >> i) & 1 else -1
    out = 0
    for i in range(SIMHASH_BITS):
        if v[i] > 0:
            out |= (1 << i)
    return out


def hamming(a: int, b: int) -> int:
    """Distance de Hamming entre deux SimHash 64 bits."""
    return bin(a ^ b).count("1")


def cluster_by_hamming(values: list[int], threshold: int) -> list[list[int]]:
    """Union-find : groupes d'indices à distance <= ``threshold``.

    O(n²) — adapté à des centaines/milliers d'items (transcriptions). Pour un
    corpus beaucoup plus grand, remplacer par un BK-tree ou un LSH par bandes.
    Ne retourne que les groupes de taille >= 2.
    """
    n = len(values)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        vi = values[i]
        for j in range(i + 1, n):
            if hamming(vi, values[j]) <= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def to_hex(simhash_int: int) -> str:
    """SimHash 64 bits -> 16 caractères hex (format de stockage)."""
    return f"{simhash_int:016x}"


def from_hex(s: str) -> int:
    return int(s, 16)
