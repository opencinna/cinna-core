# ruff: noqa: T201 - a manual CLI script; stdout is its output
"""Manual live smoke for the Gemini Live speech adapter.

NOT a test: it calls the real Gemini API and bills the key's owner. Run it
only with that person's approval, never from CI. It lives outside ``tests/``
so pytest never collects it.

Drives ``GeminiLiveAdapter`` directly (no database, no WebSocket relay) and
prints timings, counts and the outcome. The API key is read from
``GEMINI_API_KEY`` and never printed. Transcript text is printed only with
``--show-text``.

Usage (from ``backend/``, with the venv active):

    GEMINI_API_KEY=... python scripts/voice_transcription_live_smoke.py \\
        [--wav path/to/16k_mono_s16le.wav] [--burst] [--locale en-US] [--show-text]

Without ``--wav`` a few seconds of a generated tone are sent: a connectivity
check only (expect few or no transcripts).
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

if __name__ != "__main__":  # pragma: no cover - guard against accidental import
    raise ImportError("voice_transcription_live_smoke is a manual script, not a module")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.voice_transcription.constants import (  # noqa: E402
    PCM_BYTES_PER_SECOND,
)
from app.services.voice_transcription.providers.base import (  # noqa: E402
    Closed,
    Failed,
    Final,
    Partial,
    Ready,
)
from app.services.voice_transcription.providers.gemini_live import (  # noqa: E402
    SAMPLE_RATE,
    GeminiLiveAdapter,
)

CHUNK_SECONDS = 0.1
CHUNK_BYTES = int(PCM_BYTES_PER_SECOND * CHUNK_SECONDS)
GENERATED_SECONDS = 3.0
OVERALL_TIMEOUT_SECONDS = 120.0


def load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise SystemExit(
                f"{path}: need 16 kHz mono 16-bit PCM WAV, got "
                f"{wav.getframerate()} Hz, {wav.getnchannels()} ch, {wav.getsampwidth() * 8}-bit"
            )
        return wav.readframes(wav.getnframes())


def generated_tone(seconds: float = GENERATED_SECONDS, hz: float = 440.0) -> bytes:
    frames = int(SAMPLE_RATE * seconds)
    return b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * hz * i / SAMPLE_RATE)))
        for i in range(frames)
    )


def ms(start: float | None, end: float | None) -> str:
    if start is None or end is None:
        return "n/a"
    return f"{(end - start) * 1000:.0f} ms"


async def run(args: argparse.Namespace, api_key: str) -> int:
    pcm = load_wav(args.wav) if args.wav else generated_tone()
    print(
        f"audio: {len(pcm) / PCM_BYTES_PER_SECOND:.2f} s "
        f"({'wav' if args.wav else 'generated tone'}), mode: {'burst' if args.burst else 'paced 100 ms'}"
    )

    adapter = GeminiLiveAdapter()
    opened = time.monotonic()
    stream = await adapter.open(api_key=api_key, locale=args.locale, sample_rate=SAMPLE_RATE)

    ready_at: float | None = None
    first_partial_at: float | None = None
    last_final_at: float | None = None
    end_at: float | None = None
    closed_at: float | None = None
    partials = 0
    finals = 0
    outcome = "unknown"

    async def send_audio() -> None:
        nonlocal end_at
        for offset in range(0, len(pcm), CHUNK_BYTES):
            await stream.push(pcm[offset : offset + CHUNK_BYTES])
            if not args.burst:
                await asyncio.sleep(CHUNK_SECONDS)
        end_at = time.monotonic()
        await stream.end_audio()

    sender = asyncio.create_task(send_audio())
    try:
        async with asyncio.timeout(OVERALL_TIMEOUT_SECONDS):
            async for event in stream.events():
                now = time.monotonic()
                if isinstance(event, Ready):
                    ready_at = now
                elif isinstance(event, Partial):
                    partials += 1
                    first_partial_at = first_partial_at or now
                    if args.show_text:
                        print(f"  partial: {event.text}")
                elif isinstance(event, Final):
                    finals += 1
                    last_final_at = now
                    if args.show_text:
                        print(f"  final:   {event.text}")
                elif isinstance(event, Closed):
                    closed_at = now
                    outcome = "closed"
                elif isinstance(event, Failed):
                    closed_at = now
                    outcome = f"failed ({event.code}, cause={event.cause})"
    except TimeoutError:
        outcome = f"timeout after {OVERALL_TIMEOUT_SECONDS:.0f} s"
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        await stream.cancel()

    print(f"ready after open:            {ms(opened, ready_at)}")
    print(f"first partial after open:    {ms(opened, first_partial_at)}")
    print(f"end of audio -> last final:  {ms(end_at, last_final_at)}")
    print(f"end of audio -> closed:      {ms(end_at, closed_at)}")
    print(f"partials: {partials}  finals: {finals}  rotations: {stream.rotations}")
    print(f"outcome: {outcome}")
    return 0 if outcome == "closed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual Gemini Live speech adapter smoke (bills the key).")
    parser.add_argument("--wav", type=Path, help="16 kHz mono s16le WAV; default: a generated tone")
    parser.add_argument("--burst", action="store_true", help="send all audio at once instead of live-paced")
    parser.add_argument("--locale", default="en-US", help="BCP-47 locale (default en-US)")
    parser.add_argument("--show-text", action="store_true", help="print transcript text (off by default)")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY is not set", file=sys.stderr)
        return 2
    return asyncio.run(run(args, api_key))


sys.exit(main())
