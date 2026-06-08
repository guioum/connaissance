"""OCR local (commands/ocr + core/ocr_local). Le moteur Vision est macOS-only ;
on teste ce qui est pur (parsing frontmatter, disponibilité)."""
from connaissance.commands import ocr as O
from connaissance.core import ocr_local


def test_read_frontmatter():
    c = "---\nsource: Documents/x.pdf\nocr_engine: vision-local\nocr_confidence: 0.85\n---\nTexte OCR."
    fm = O._read_frontmatter(c)
    assert fm["ocr_engine"] == "vision-local"
    assert float(fm["ocr_confidence"]) == 0.85
    assert O._read_frontmatter("pas de frontmatter") == {}


def test_available_returns_bool():
    assert isinstance(ocr_local.available(), bool)
