"""The speech worker: raw 16 kHz mono PCM on stdin, one JSON object per line on stdout.

Runs as a separate process inside BarClaw's own speech environment, so Vosk doesn't need to be
installed for the system Python and a slow recognizer can never stall the UI. Vosk is CPU-only.
Standard library imports only, apart from Vosk itself.
"""
from __future__ import annotations

import json
import os
import sys


def emit(**payload) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(model_path: str) -> int:
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    try:
        import vosk
    except ImportError:
        emit(error="vosk-missing")
        return 2
    vosk.SetLogLevel(-1)
    try:
        model = vosk.Model(model_path)
    except Exception as failure:  # Vosk raises a bare Exception for a bad model directory.
        emit(error=f"model: {failure}")
        return 3
    recognizer = vosk.KaldiRecognizer(model, 16000)
    emit(ready=True)
    stream = sys.stdin.buffer
    committed = ""
    while True:
        chunk = stream.read(3200)
        if not chunk:
            break
        if recognizer.AcceptWaveform(chunk):
            text = json.loads(recognizer.Result()).get("text", "")
            committed = (committed + " " + text).strip() if text else committed
            emit(partial=committed)
        else:
            partial = json.loads(recognizer.PartialResult()).get("partial", "")
            emit(partial=(committed + " " + partial).strip())
    text = json.loads(recognizer.FinalResult()).get("text", "")
    emit(final=(committed + " " + text).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
