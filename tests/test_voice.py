"""Tests for app/services/voice.py: gTTS, faster-whisper and Supabase Storage, all mocked."""

from __future__ import annotations

import io
import json
import sys
import threading
import types
from typing import Any

import httpx
import numpy as np
import numpy.typing as npt
import pytest

from app.core.config import Settings
from app.core.exceptions import AudioStorageError, SpeechError
from app.services.voice import (
    GttsSynthesizer,
    SupabaseAudioStore,
    WhisperTranscriber,
    advisory_audio_path,
    decode_audio,
)

SETTINGS = Settings(
    _env_file=None,
    supabase_url="https://abc.supabase.co",
    supabase_service_role_key="service-key",
    database_url="postgresql://u:p@localhost:5432/db",
)


# --- gTTS ----------------------------------------------------------------------------


class FakeGttsError(Exception):
    pass


def install_fake_gtts(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class FakeGTTS:
        def __init__(self, text: str, **kwargs: Any) -> None:
            calls.append({"text": text, **kwargs})

        def write_to_fp(self, fp: io.BytesIO) -> None:
            if fail:
                raise FakeGttsError("429 Too Many Requests")
            fp.write(b"ID3fake-mp3")

    module = types.ModuleType("gtts")
    module.gTTS = FakeGTTS  # type: ignore[attr-defined]
    module.gTTSError = FakeGttsError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gtts", module)
    return calls


def test_gtts_speaks_english_with_indian_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_fake_gtts(monkeypatch)
    audio = GttsSynthesizer().synthesize("Your glucose is 126.", "en")
    assert audio == b"ID3fake-mp3"
    assert calls == [{"text": "Your glucose is 126.", "lang": "en", "tld": "co.in"}]


@pytest.mark.parametrize("language", ["hi", "ta"])
def test_tts_is_english_only(monkeypatch: pytest.MonkeyPatch, language: str) -> None:
    calls = install_fake_gtts(monkeypatch)
    with pytest.raises(SpeechError, match="English-only"):
        GttsSynthesizer().synthesize("text", language)  # type: ignore[arg-type]
    assert calls == []


def test_gtts_failure_becomes_speech_error(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_gtts(monkeypatch, fail=True)
    with pytest.raises(SpeechError, match="gTTS failed"):
        GttsSynthesizer().synthesize("text", "en")
    with pytest.raises(SpeechError, match="nothing to speak"):
        GttsSynthesizer().synthesize("   ", "en")


# --- faster-whisper ------------------------------------------------------------------


class Segment:
    def __init__(self, text: str) -> None:
        self.text = text


SAMPLES = np.zeros(16_000, dtype=np.float32)


def fake_decoder(audio: bytes) -> npt.NDArray[np.float32]:
    assert audio in {b"webm-bytes", b"x"}
    return SAMPLES


class FakeWhisper:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def transcribe(
        self, audio: npt.NDArray[np.float32], *, language: str
    ) -> tuple[list[Segment], object]:
        self.calls.append(language)
        assert audio is SAMPLES  # the decoded samples, not the raw upload
        return [Segment(" my sugar is "), Segment("one forty ")], object()


def test_whisper_loads_lazily_once_and_joins_segments() -> None:
    loads: list[int] = []
    model = FakeWhisper()

    def loader() -> FakeWhisper:
        loads.append(1)
        return model

    transcriber = WhisperTranscriber(loader, fake_decoder)
    assert loads == []  # nothing loaded at construction
    assert transcriber.transcribe(b"webm-bytes", "en") == "my sugar is one forty"
    assert transcriber.transcribe(b"webm-bytes", "en") == "my sugar is one forty"
    assert loads == [1]
    assert model.calls == ["en", "en"]


def test_whisper_concurrent_first_calls_load_once() -> None:
    loads: list[int] = []
    gate = threading.Event()

    def slow_loader() -> FakeWhisper:
        loads.append(1)
        gate.wait(0.2)
        return FakeWhisper()

    transcriber = WhisperTranscriber(slow_loader, fake_decoder)
    threads = [
        threading.Thread(target=transcriber.transcribe, args=(b"webm-bytes", "en"))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert loads == [1]


def test_whisper_rejects_bad_input_and_wraps_decode_errors() -> None:
    class Broken:
        def transcribe(
            self, audio: npt.NDArray[np.float32], *, language: str
        ) -> tuple[list[Segment], object]:
            raise RuntimeError("Invalid data found when processing input")

    with pytest.raises(SpeechError, match="English-only"):
        WhisperTranscriber(FakeWhisper, fake_decoder).transcribe(b"x", "ta")
    with pytest.raises(SpeechError, match="no audio"):
        WhisperTranscriber(FakeWhisper, fake_decoder).transcribe(b"", "en")
    with pytest.raises(SpeechError, match="transcription failed"):
        WhisperTranscriber(Broken, fake_decoder).transcribe(b"x", "en")


def _webm_opus_tone(seconds: float = 1.0) -> bytes:
    """A 440 Hz tone encoded as webm/opus, like Chrome's MediaRecorder output."""
    import av  # noqa: PLC0415

    buf = io.BytesIO()
    with av.open(buf, mode="w", format="webm") as out:
        stream = out.add_stream("libopus", rate=48_000)
        t = np.arange(int(48_000 * seconds)) / 48_000
        pcm = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
        frame.sample_rate = 48_000
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return buf.getvalue()


def test_decode_audio_turns_media_recorder_webm_into_16k_samples() -> None:
    samples = decode_audio(_webm_opus_tone(1.0))
    assert samples.dtype == np.float32
    # ~1 s at 16 kHz (opus adds a little padding).
    assert 15_000 <= samples.size <= 17_500
    assert 0.05 < float(np.abs(samples).max()) <= 1.0


def test_decode_audio_rejects_garbage() -> None:
    with pytest.raises(SpeechError, match="could not decode"):
        decode_audio(b"definitely not audio")


def test_default_loader_uses_small_int8_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import voice  # noqa: PLC0415

    seen: dict[str, Any] = {}

    class WhisperModel:
        def __init__(self, size: str, **kwargs: Any) -> None:
            seen.update(size=size, **kwargs)

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    voice.load_whisper_model()
    assert seen == {
        "size": "small",
        "device": "cpu",
        "compute_type": "int8",
        "use_auth_token": False,  # public model: never send a (possibly stale) saved token
    }


# --- Supabase Storage ----------------------------------------------------------------


def store_with(handler: Any) -> tuple[SupabaseAudioStore, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result: httpx.Response = handler(request)
        return result

    client = httpx.Client(transport=httpx.MockTransport(record))
    return SupabaseAudioStore(SETTINGS, http_client=client), requests


def test_upload_goes_to_private_bucket_with_service_key() -> None:
    store, requests = store_with(lambda _r: httpx.Response(200, json={"Key": "x"}))
    store.upload("u1/a1.mp3", b"ID3")
    (request,) = requests
    assert str(request.url) == "https://abc.supabase.co/storage/v1/object/advisory-audio/u1/a1.mp3"
    assert request.headers["content-type"] == "audio/mpeg"
    assert request.headers["x-upsert"] == "true"
    assert request.headers["authorization"] == "Bearer service-key"
    assert request.content == b"ID3"


def test_signed_url_is_absolute_and_short_lived() -> None:
    store, requests = store_with(
        lambda _r: httpx.Response(
            200, json={"signedURL": "/object/sign/advisory-audio/u1/a1.mp3?token=t"}
        )
    )
    url = store.signed_url("u1/a1.mp3", 300)
    assert url == "https://abc.supabase.co/storage/v1/object/sign/advisory-audio/u1/a1.mp3?token=t"
    assert json.loads(requests[0].content) == {"expiresIn": 300}


def test_signed_urls_signs_many_in_one_request_and_skips_missing_objects() -> None:
    # Shape of Storage's batch sign answer (POST /object/sign/{bucket}, as storage-js
    # createSignedUrls uses), checked against the live project on 2026-11-16.
    store, requests = store_with(
        lambda _r: httpx.Response(
            200,
            json=[
                {
                    "path": "chat/u1/t1.mp3",
                    "signedURL": "/object/sign/b/chat/u1/t1.mp3?token=a",
                    "error": None,
                },
                {
                    "path": "chat/u1/gone.mp3",
                    "signedURL": None,
                    "error": "Either the object does not exist or you do not have access to it",
                },
            ],
        )
    )
    urls = store.signed_urls(["chat/u1/t1.mp3", "chat/u1/gone.mp3"], 300)
    assert urls == {
        "chat/u1/t1.mp3": "https://abc.supabase.co/storage/v1/object/sign/b/chat/u1/t1.mp3?token=a"
    }
    (request,) = requests
    assert request.url.path == "/storage/v1/object/sign/advisory-audio"
    assert json.loads(request.content) == {
        "expiresIn": 300,
        "paths": ["chat/u1/t1.mp3", "chat/u1/gone.mp3"],
    }


def test_signed_urls_with_no_paths_makes_no_request() -> None:
    store, requests = store_with(lambda _r: httpx.Response(500))
    assert store.signed_urls([], 300) == {}
    assert requests == []


@pytest.mark.parametrize(
    "response", [httpx.Response(500), httpx.Response(200, json={"not": "a list"})]
)
def test_signed_urls_errors_are_audio_storage_errors(response: httpx.Response) -> None:
    store, _ = store_with(lambda _r: response)
    with pytest.raises(AudioStorageError):
        store.signed_urls(["chat/u1/t1.mp3"], 300)


@pytest.mark.parametrize(
    "response",
    [httpx.Response(400, json={"error": "not found"}), httpx.Response(200, json={"oops": 1})],
)
def test_storage_errors_are_audio_storage_errors(response: httpx.Response) -> None:
    store, _ = store_with(lambda _r: response)
    with pytest.raises(AudioStorageError):
        store.signed_url("u1/a1.mp3", 300)
    if response.status_code != 200:
        with pytest.raises(AudioStorageError, match="HTTP 400"):
            store.upload("u1/a1.mp3", b"ID3")


def test_network_errors_are_audio_storage_errors() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    store, _ = store_with(boom)
    with pytest.raises(AudioStorageError, match="ConnectError"):
        store.upload("p", b"x")
    with pytest.raises(AudioStorageError, match="ConnectError"):
        store.signed_url("p", 60)


def test_audio_path_is_one_folder_per_patient() -> None:
    assert advisory_audio_path("user-1", "adv-2") == "user-1/adv-2.mp3"
