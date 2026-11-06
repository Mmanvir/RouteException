"""Text to speech for approved customer updates (gTTS; needs internet). Never called before approval."""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import get_settings

log = logging.getLogger("routeexception.tts")


def synthesize(text: str, out_path: Path) -> Path | None:
    s = get_settings()
    if not s.tts_enabled:
        return None
    try:
        from gtts import gTTS

        out_path.parent.mkdir(parents=True, exist_ok=True)
        gTTS(text=text, lang=s.tts_lang).save(str(out_path))
        return out_path
    except Exception as e:  # network errors, missing package, etc. Audio is optional.
        log.warning("TTS failed (%s); message will be text-only", e)
        return None
