"""Écritures de fichiers atomiques.

``Path.write_text`` tronque puis écrit : un crash (kill, disque plein, coupure)
au milieu laisse un fichier vide ou partiel. Pour les ``.md`` (frontmatter =
source de vérité du pipeline, cf. CLAUDE.md) et les YAML de config, c'est une
perte réelle. Ici : écriture dans un fichier temporaire du même dossier puis
``os.replace`` (atomique sur un même filesystem — POSIX comme APFS).
"""
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, text: str,
                      encoding: str = "utf-8") -> None:
    """Écrire ``text`` dans ``path`` de façon atomique (tmp + ``os.replace``).

    Le fichier temporaire est créé dans le dossier cible (même filesystem,
    sinon ``os.replace`` n'est plus atomique). Les métadonnées (mode) d'un
    fichier existant sont préservées.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=path.parent)
    try:
        try:
            mode = path.stat().st_mode
        except OSError:
            mode = None
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
