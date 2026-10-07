"""Source-grounded briefing pipeline: hygiene -> sufficiency -> fact selection
-> script composition -> deterministic quality gate. No LLM, no network."""
import hashlib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def briefing_fingerprint() -> str:
    """Hash of the code that decides whether a story can be narrated (this package plus
    the caption limits in storyboard_composer). A stored sufficiency verdict carries the
    fingerprint it was computed under; a different one means the verdict is stale."""
    here = Path(__file__).resolve().parent
    files = sorted(here.glob("*.py")) + [here.parent / "storyboard_composer.py"]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]
