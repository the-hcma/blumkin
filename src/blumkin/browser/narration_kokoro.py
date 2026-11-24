"""The Kokoro speech engine for narration (#816): offline, natural, set up on request.

Kokoro is installed by `blumkin browser narration setup` (see `narration_setup.py`), into a
private folder under the config directory, and used from there: its packages are put on
`sys.path` only while they are imported, and its model is loaded once per `record stop` and
reused for every cue. Text goes in, a WAV file comes out, and nothing touches the network.
"""

from __future__ import annotations

import contextlib
import os
import wave
from pathlib import Path
from typing import Any

from blumkin.browser.narration import NarrationError
from blumkin.browser.narration_setup import (
    MODEL_NAME,
    VOICES_NAME,
    is_installed,
    narration_dir,
    site_dir,
    site_on_path,
)

#: What a voice's first letter means to the phonemizer (Kokoro names its voices `af_bella`:
#: an accent letter, a gender letter, an underscore, a name).
_LANGUAGES = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "cmn",
}
_NAMES_SHOWN = 8


class KokoroEngine:
    """Kokoro (82M parameters) through `kokoro-onnx`, with `af_bella` as its default voice."""

    name = "kokoro"
    default_voice = "af_bella"

    def __init__(self, config_dir: Path, *, before_marker: bool = False) -> None:
        # `before_marker` is for the setup's own test word, spoken before the marker is written.
        self._config_dir = config_dir
        self._before_marker = before_marker
        self._model: Any = None

    def is_installed(self) -> bool:
        if self._before_marker:
            root = narration_dir(self._config_dir)
            return (
                (root / MODEL_NAME).is_file()
                and (root / VOICES_NAME).is_file()
                and (site_dir(self._config_dir) / "kokoro_onnx").is_dir()
            )
        return is_installed(self._config_dir)

    def ensure_ready(self) -> None:
        if not self.is_installed():
            raise NarrationError(
                "Expected the Kokoro narration engine to be set up, got none; run "
                "`blumkin browser narration setup` once (it downloads about 500 MB after you "
                "confirm), or use --engine say"
            )

    def voices(self) -> list[str]:
        """The voices the installed voices file holds."""
        self.ensure_ready()
        path = narration_dir(self._config_dir) / VOICES_NAME
        try:
            with site_on_path(self._config_dir):
                import numpy  # pyright: ignore[reportMissingImports] - set up on request

                with numpy.load(path, allow_pickle=False) as data:
                    return sorted(str(name) for name in data.files)
        except (ImportError, OSError, ValueError) as exc:
            raise NarrationError(f"Expected to read the Kokoro voices, got {exc}") from exc

    def check_voice(self, voice: str) -> None:
        names = self.voices()
        if voice not in names:
            shown = ", ".join(names[:_NAMES_SHOWN])
            more = f" and {len(names) - _NAMES_SHOWN} more" if len(names) > _NAMES_SHOWN else ""
            raise NarrationError(f"Expected a Kokoro voice, got {voice!r}; it has {shown}{more}")

    def synthesize(self, text: str, *, voice: str, speed: float, out: Path) -> None:
        self.ensure_ready()
        model = self._load()
        language = _LANGUAGES.get(voice[:1], "en-us")
        try:
            samples, rate = model.create(text, voice=voice, speed=speed, lang=language)
            with site_on_path(self._config_dir):
                import numpy  # pyright: ignore[reportMissingImports] - set up on request

                pcm = (numpy.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        except NarrationError:
            raise
        except Exception as exc:  # noqa: BLE001 - whatever the model raises, the text is not in it
            raise NarrationError(
                f"Expected Kokoro to speak a cue, got {type(exc).__name__}"
            ) from exc
        descriptor = os.open(out, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle, wave.open(handle, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(rate))
            wav.writeframes(pcm)

    def _load(self) -> Any:
        """The model, loaded the first time a cue is spoken and kept for the rest."""
        if self._model is None:
            root = narration_dir(self._config_dir)
            try:
                with site_on_path(self._config_dir):
                    from kokoro_onnx import Kokoro  # pyright: ignore[reportMissingImports]

                    self._model = Kokoro(str(root / MODEL_NAME), str(root / VOICES_NAME))
            except Exception as exc:  # noqa: BLE001 - an import or a load can fail in many ways
                with contextlib.suppress(Exception):
                    self._model = None
                raise NarrationError(
                    f"Expected the Kokoro model to load, got {type(exc).__name__}: {str(exc)[:120]}"
                ) from exc
        return self._model
