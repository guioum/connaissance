"""OCR local via le framework Vision de macOS (moteur Live Text).

Gratuit, local (Neural Engine), sans téléchargement iCloud (on lit le chemin
fourni — typiquement le miroir SSD). Le helper Swift (`helpers/ocr_vision.swift`)
est compilé à la volée vers un cache et réutilisé.

Sert de **première passe OCR gratuite** ; les transcriptions produites sont
marquées (`ocr_engine: vision-local` + `ocr_confidence`) pour permettre une
**repasse Mistral** ciblée sur les cas à faible confiance.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

_HELPER_SRC = Path(__file__).resolve().parent.parent / "helpers" / "ocr_vision.swift"
_BIN_DIR = Path.home() / "Library" / "Application Support" / "connaissance" / "bin"
_BIN = _BIN_DIR / "ocr_vision"

OCR_ENGINE = "vision-local"


def available() -> bool:
    """Vrai si l'OCR local est utilisable (swiftc présent + source helper)."""
    return shutil.which("swiftc") is not None and _HELPER_SRC.is_file()


def _ensure_binary() -> Path | None:
    """Compiler le helper Swift à la volée (cache), recompiler si la source a
    changé. Retourne le chemin du binaire ou None si indisponible."""
    if not available():
        return None
    try:
        if _BIN.is_file() and _BIN.stat().st_mtime >= _HELPER_SRC.stat().st_mtime:
            return _BIN
        _BIN_DIR.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["swiftc", "-O", str(_HELPER_SRC), "-o", str(_BIN)],
                           capture_output=True, timeout=180)
        return _BIN if r.returncode == 0 and _BIN.is_file() else None
    except (OSError, subprocess.SubprocessError):
        return None


def ocr_file(path, max_pages: int = 50, timeout: int = 180) -> dict | None:
    """OCR un PDF (rendu page→image) ou une image. Retourne
    ``{text, confidence, pages}`` ou None (indisponible / échec / vide).
    ``path`` doit être lisible directement (miroir SSD pour un dataless)."""
    b = _ensure_binary()
    if b is None:
        return None
    try:
        r = subprocess.run([str(b), str(path), str(max_pages)],
                           capture_output=True, timeout=timeout)
        if r.returncode != 0:
            return None
        out = json.loads(r.stdout.decode("utf-8", "replace"))
        return out if (out.get("text") or "").strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
