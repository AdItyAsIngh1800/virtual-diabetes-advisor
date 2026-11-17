"""Voice I/O: text-to-speech for advisories (R3) and speech-to-text for chat (R4).

Three small interfaces, each with one real implementation and fakes in tests:

* :class:`SpeechSynthesizer` - text -> mp3 bytes. :class:`GttsSynthesizer` uses gTTS
  (Google Translate's TTS endpoint). mp3 because every browser plays it.
* :class:`SpeechTranscriber` - recorded audio -> text. :class:`WhisperTranscriber`
  uses faster-whisper ``small`` with int8 weights on CPU, loaded lazily, once per
  process (the first call downloads ~460 MB and takes a few seconds). Audio is decoded
  here (:func:`decode_audio`, PyAV) and handed to the model as samples: faster-whisper
  1.2's own decoder passes an argument PyAV 15+ no longer accepts.
* :class:`AudioStore` - where advisory mp3s live. :class:`SupabaseAudioStore` uploads
  to a **private** Supabase Storage bucket and hands out short-lived signed URLs, so
  only the patient the API gives a link to can play their advisory.

English only for now: Tamil and Hindi need reviewed clinical translations of the
advisory templates first (see ``app.services.advisory``).
"""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Iterable, Sequence
from typing import Final, Protocol

import httpx
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.core.config import Settings
from app.core.exceptions import AudioStorageError, AudioTooLongError, SpeechError
from app.db.models import Language

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES: Final[frozenset[Language]] = frozenset({"en"})
# gTTS "tld" picks the regional Google voice; co.in is Indian English, which the demo
# cohort (India, IST) understands best. Product choice, not a clinical one.
_GTTS_TLD: Final = "co.in"
# faster-whisper model choice from the web-push-voice-advisory skill: "small" is the
# smallest size that handles accented English well; int8 halves memory and runs on
# any laptop CPU.
WHISPER_MODEL_SIZE: Final = "small"
_WHISPER_DEVICE: Final = "cpu"
_WHISPER_COMPUTE_TYPE: Final = "int8"

MP3_CONTENT_TYPE: Final = "audio/mpeg"
_HTTP_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
_HTTP_OK: Final = frozenset({200, 201})


def _require_supported(language: Language) -> None:
    """Refuse languages without reviewed voice support.

    Args:
        language: Requested language.

    Raises:
        SpeechError: For anything but English.
    """
    if language not in SUPPORTED_LANGUAGES:
        raise SpeechError(f"voice is English-only for now, got {language!r}")


# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------


class SpeechSynthesizer(Protocol):
    """Turns advisory text into playable audio."""

    def synthesize(self, text: str, language: Language) -> bytes:
        """Speak ``text``.

        Args:
            text: What to say (already in ``language``).
            language: Language of the text.

        Returns:
            mp3 bytes.

        Raises:
            SpeechError: If the language is unsupported or synthesis fails.
        """
        ...


class GttsSynthesizer:
    """:class:`SpeechSynthesizer` backed by gTTS (needs internet access)."""

    def synthesize(self, text: str, language: Language) -> bytes:
        """Speak ``text`` with gTTS.

        Args:
            text: What to say.
            language: Must be ``"en"``.

        Returns:
            mp3 bytes.

        Raises:
            SpeechError: If the language is unsupported, the text is empty, or the
                gTTS request fails.
        """
        _require_supported(language)
        if not text.strip():
            raise SpeechError("nothing to speak")
        from gtts import gTTS, gTTSError  # noqa: PLC0415 - heavy import, only when speaking

        buffer = io.BytesIO()
        try:
            gTTS(text, lang=language, tld=_GTTS_TLD).write_to_fp(buffer)
        except (gTTSError, AssertionError, ValueError) as exc:
            raise SpeechError(f"gTTS failed ({type(exc).__name__})") from exc
        audio = buffer.getvalue()
        if not audio:
            raise SpeechError("gTTS returned no audio")
        return audio


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------


class SpeechTranscriber(Protocol):
    """Turns a recorded voice message into text."""

    def transcribe(self, audio: bytes, language: Language) -> str:
        """Transcribe ``audio``.

        Args:
            audio: Encoded audio (webm/ogg from MediaRecorder, mp3, wav ...).
            language: Spoken language.

        Returns:
            The transcript, stripped (empty for silence).

        Raises:
            SpeechError: If the language is unsupported or decoding fails.
        """
        ...


class _WhisperSegment(Protocol):
    """The one field of a faster-whisper segment this module reads."""

    text: str


class _WhisperModel(Protocol):
    """The part of ``faster_whisper.WhisperModel`` this module calls."""

    def transcribe(
        self, audio: npt.NDArray[np.float32], *, language: str
    ) -> tuple[Iterable[_WhisperSegment], object]:
        """Transcribe audio (see faster-whisper).

        Args:
            audio: Mono float32 samples at :data:`WHISPER_SAMPLE_RATE`.
            language: ISO language code.

        Returns:
            Segments and transcription info.
        """
        ...


# Whisper models are trained on 16 kHz mono audio; faster-whisper expects exactly that.
WHISPER_SAMPLE_RATE: Final = 16_000
# s16 PCM full scale, to map samples into [-1, 1) as Whisper expects.
_S16_FULL_SCALE: Final = 32768.0


def decode_audio(audio: bytes) -> npt.NDArray[np.float32]:
    """Decode a recording (webm/ogg opus, mp4/aac, mp3, wav) to 16 kHz mono samples.

    Same output as ``faster_whisper.decode_audio``, done with the installed PyAV.

    Args:
        audio: Encoded audio bytes.

    Returns:
        float32 samples in [-1, 1).

    Raises:
        SpeechError: If the bytes are not decodable audio or hold no sound.
    """
    import av  # noqa: PLC0415 - only needed when someone speaks

    resampler = av.AudioResampler(format="s16", layout="mono", rate=WHISPER_SAMPLE_RATE)
    # s16 frames (PyAV types to_ndarray() as a union over sample formats).
    chunks: list[npt.NDArray[np.generic]] = []
    try:
        with av.open(io.BytesIO(audio), mode="r") as container:
            for frame in container.decode(audio=0):
                chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
            chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    except (av.FFmpegError, ValueError, IndexError, OSError) as exc:
        raise SpeechError(f"could not decode the recording ({type(exc).__name__})") from exc
    if not chunks:
        raise SpeechError("the recording holds no audio")
    return (np.concatenate(chunks).astype(np.float32) / _S16_FULL_SCALE).astype(np.float32)


class AudioDecoder(Protocol):
    """Turns encoded audio into Whisper input samples (tests pass a fake)."""

    def __call__(self, audio: bytes) -> npt.NDArray[np.float32]:
        """Decode.

        Args:
            audio: Encoded audio.

        Returns:
            16 kHz mono float32 samples.
        """
        ...


class WhisperModelLoader(Protocol):
    """Creates the faster-whisper model (tests pass a fake)."""

    def __call__(self) -> _WhisperModel:
        """Load the model.

        Returns:
            A ready model.
        """
        ...


def load_whisper_model() -> _WhisperModel:
    """Load faster-whisper ``small`` (int8, CPU).

    Returns:
        The model.
    """
    from faster_whisper import WhisperModel  # noqa: PLC0415 - ~1 s import, load lazily

    model: _WhisperModel = WhisperModel(
        WHISPER_MODEL_SIZE,
        device=_WHISPER_DEVICE,
        compute_type=_WHISPER_COMPUTE_TYPE,
        # The model is public. Never send a saved Hugging Face token: an expired one
        # makes the Hub answer 401 and the download (or cache check) fails.
        use_auth_token=False,
    )
    return model


class WhisperTranscriber:
    """:class:`SpeechTranscriber` backed by faster-whisper, loaded once on first use.

    Thread-safe: the API serves requests from a thread pool, and two first requests
    arriving together must not load two copies of the model.

    Args:
        loader: Builds the model; defaults to :func:`load_whisper_model`.
        decoder: Decodes uploads to samples; defaults to :func:`decode_audio`.
        max_seconds: Longest accepted recording, checked after decoding.
    """

    def __init__(
        self,
        loader: WhisperModelLoader = load_whisper_model,
        decoder: AudioDecoder = decode_audio,
        max_seconds: float = 60.0,
    ) -> None:
        self._loader = loader
        self._decoder = decoder
        self._max_seconds = max_seconds
        self._model: _WhisperModel | None = None
        self._lock = threading.Lock()

    def _get_model(self) -> _WhisperModel:
        """Return the model, loading it on the first call only.

        Returns:
            The shared model.
        """
        if self._model is None:
            with self._lock:
                if self._model is None:
                    logger.info("loading faster-whisper %s (int8, cpu)", WHISPER_MODEL_SIZE)
                    self._model = self._loader()
        return self._model

    def transcribe(self, audio: bytes, language: Language) -> str:
        """Transcribe ``audio`` with faster-whisper.

        Args:
            audio: Encoded audio.
            language: Must be ``"en"``.

        Returns:
            The transcript, stripped.

        Raises:
            AudioTooLongError: If the decoded audio is longer than ``max_seconds``.
            SpeechError: If the language is unsupported, the audio is empty, or
                decoding fails.
        """
        _require_supported(language)
        if not audio:
            raise SpeechError("no audio to transcribe")
        samples = self._decoder(audio)
        if len(samples) > self._max_seconds * WHISPER_SAMPLE_RATE:
            raise AudioTooLongError(f"recording is longer than {self._max_seconds:g} s")
        model = self._get_model()
        try:
            segments, _ = model.transcribe(samples, language=language)
            # Segments are a lazy generator: decoding errors surface while iterating.
            return " ".join(segment.text.strip() for segment in segments).strip()
        except (RuntimeError, ValueError, OSError) as exc:
            raise SpeechError(f"transcription failed ({type(exc).__name__})") from exc


# ---------------------------------------------------------------------------
# Audio storage
# ---------------------------------------------------------------------------


class AudioStore(Protocol):
    """Private storage for advisory audio."""

    def upload(self, path: str, audio: bytes) -> None:
        """Store (or replace) an mp3.

        Args:
            path: Object path inside the bucket.
            audio: mp3 bytes.

        Raises:
            AudioStorageError: If the upload fails.
        """
        ...

    def signed_url(self, path: str, expires_in_s: int) -> str:
        """Return a URL that plays the object for ``expires_in_s`` seconds.

        Args:
            path: Object path inside the bucket.
            expires_in_s: Lifetime of the URL.

        Returns:
            An absolute https URL.

        Raises:
            AudioStorageError: If signing fails (e.g. the object is missing).
        """
        ...

    def signed_urls(self, paths: Sequence[str], expires_in_s: int) -> dict[str, str]:
        """Sign several objects in one call.

        Args:
            paths: Object paths inside the bucket.
            expires_in_s: Lifetime of the URLs.

        Returns:
            ``path -> URL`` for every path that exists; missing objects are left out.

        Raises:
            AudioStorageError: If the request itself fails.
        """
        ...


def advisory_audio_path(user_id: object, advisory_id: object) -> str:
    """Object path of an advisory's mp3: one folder per patient.

    Args:
        user_id: Patient id.
        advisory_id: Advisory id.

    Returns:
        ``"<user_id>/<advisory_id>.mp3"``.
    """
    return f"{user_id}/{advisory_id}.mp3"


class _SignedUrlOut(BaseModel):
    """Body of Storage's ``POST /object/sign/...`` response."""

    signedURL: str  # noqa: N815 - Supabase's field name


class _BatchSignedUrlOut(BaseModel):
    """One item of Storage's batch ``POST /object/sign/{bucket}`` response."""

    path: str | None = None
    signedURL: str | None = None  # noqa: N815 - Supabase's field name
    error: str | None = None


_BatchSignedUrls: Final = TypeAdapter(list[_BatchSignedUrlOut])


class SupabaseAudioStore:
    """:class:`AudioStore` on Supabase Storage, through its REST API with the service key.

    The bucket is private (migration 0007) and has no ``storage.objects`` policies,
    so nothing but this backend can read it; patients only ever get signed URLs.

    Args:
        settings: Supplies the project URL, service-role key and bucket name.
        http_client: Optional client (tests pass one with a mock transport).
    """

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self._base = f"{str(settings.supabase_url).rstrip('/')}/storage/v1"
        self._bucket = settings.advisory_audio_bucket
        key = settings.supabase_service_role_key.get_secret_value()
        self._headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self._client = http_client or httpx.Client(timeout=_HTTP_TIMEOUT)

    def upload(self, path: str, audio: bytes) -> None:
        """Upload an mp3, replacing any earlier file at ``path``.

        Args:
            path: Object path inside the bucket.
            audio: mp3 bytes.

        Raises:
            AudioStorageError: On a network error or a non-2xx answer.
        """
        url = f"{self._base}/object/{self._bucket}/{path}"
        headers = {**self._headers, "Content-Type": MP3_CONTENT_TYPE, "x-upsert": "true"}
        try:
            response = self._client.post(url, content=audio, headers=headers)
        except httpx.HTTPError as exc:
            raise AudioStorageError(f"storage upload failed ({type(exc).__name__})") from exc
        if response.status_code not in _HTTP_OK:
            raise AudioStorageError(f"storage upload answered HTTP {response.status_code}")

    def signed_url(self, path: str, expires_in_s: int) -> str:
        """Create a signed URL for ``path``.

        Args:
            path: Object path inside the bucket.
            expires_in_s: Lifetime of the URL in seconds.

        Returns:
            The absolute signed URL.

        Raises:
            AudioStorageError: On a network error, a non-2xx answer or an unexpected body.
        """
        url = f"{self._base}/object/sign/{self._bucket}/{path}"
        try:
            response = self._client.post(
                url, json={"expiresIn": expires_in_s}, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise AudioStorageError(f"storage sign failed ({type(exc).__name__})") from exc
        if response.status_code not in _HTTP_OK:
            raise AudioStorageError(f"storage sign answered HTTP {response.status_code}")
        try:
            signed = _SignedUrlOut.model_validate_json(response.content).signedURL
        except ValidationError as exc:
            raise AudioStorageError("storage sign returned an unexpected body") from exc
        # Storage answers with a path relative to /storage/v1 ("/object/sign/...").
        return f"{self._base}{signed}"

    def signed_urls(self, paths: Sequence[str], expires_in_s: int) -> dict[str, str]:
        """Sign several objects with one request (Storage's batch sign endpoint).

        Args:
            paths: Object paths inside the bucket.
            expires_in_s: Lifetime of the URLs in seconds.

        Returns:
            ``path -> URL``; objects Storage reports as missing are left out.

        Raises:
            AudioStorageError: On a network error, a non-2xx answer or an unexpected body.
        """
        if not paths:
            return {}
        url = f"{self._base}/object/sign/{self._bucket}"
        try:
            response = self._client.post(
                url, json={"expiresIn": expires_in_s, "paths": list(paths)}, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise AudioStorageError(f"storage batch sign failed ({type(exc).__name__})") from exc
        if response.status_code not in _HTTP_OK:
            raise AudioStorageError(f"storage batch sign answered HTTP {response.status_code}")
        try:
            items = _BatchSignedUrls.validate_json(response.content)
        except ValidationError as exc:
            raise AudioStorageError("storage batch sign returned an unexpected body") from exc
        return {
            item.path: f"{self._base}{item.signedURL}"
            for item in items
            if item.path is not None and item.signedURL and item.error is None
        }
