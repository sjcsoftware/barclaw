"""Push-to-talk speech capture: hold to listen, release to transcribe.

Audio comes from whichever recorder the system has (PipeWire, PulseAudio, or ALSA). Recognition
runs locally and only on the CPU: Vosk streams words as you speak, or a user-configured command
transcribes a WAV file after release (for whisper.cpp and the like).
"""
from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, Callable, Optional

from gi.repository import GLib

from ..support import ExpectedError, Observable, Task, data_dir, in_thread, log, observed, package_dir, sleep, spawn

SAMPLE_RATE = 16000
MODELS = {
    "en-us": ("English (US)", "vosk-model-small-en-us-0.15"),
    "ko": ("Korean", "vosk-model-small-ko-0.22"),
    "ja": ("Japanese", "vosk-model-small-ja-0.22"),
    "cn": ("Chinese", "vosk-model-small-cn-0.22"),
    "de": ("German", "vosk-model-small-de-0.15"),
    "fr": ("French", "vosk-model-small-fr-0.22"),
    "es": ("Spanish", "vosk-model-small-es-0.42"),
    "pt": ("Portuguese", "vosk-model-small-pt-0.3"),
    "ru": ("Russian", "vosk-model-small-ru-0.22"),
    "it": ("Italian", "vosk-model-small-it-0.22"),
    "hi": ("Hindi", "vosk-model-small-hi-0.22"),
}
MODEL_BASE_URL = "https://alphacephei.com/vosk/models"


def recorder_command() -> Optional[list]:
    if os.environ.get("BARCLAW_VOICE_RECORDER"):  # Test hook: anything that writes raw PCM to stdout.
        return shlex.split(os.environ["BARCLAW_VOICE_RECORDER"])
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16", "--media-role", "Communication", "-"]
    if shutil.which("parec"):
        return ["parec", "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1", "--raw", "--latency-msec=50"]
    if shutil.which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"]
    return None


def speech_dir() -> Path:
    return data_dir() / "speech"


def speech_python() -> Path:
    return speech_dir() / "venv" / "bin" / "python"


def model_path(language: str) -> Optional[Path]:
    entry = MODELS.get(language)
    return speech_dir() / "models" / entry[1] if entry else None


def is_engine_installed() -> bool:
    return speech_python().exists()


def is_model_installed(language: str) -> bool:
    path = model_path(language)
    return path is not None and (path / "conf").is_dir() or (path is not None and (path / "am").is_dir())


def rms_level(pcm: bytes) -> float:
    """Maps roughly -50 dB…0 dB to 0…1."""
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    rms = math.sqrt(sum(s * s for s in samples) / count) / 32768
    db = 20 * math.log10(max(rms, 0.00001))
    return max(0.0, min(1.0, (db + 50) / 50))


class VoiceController(Observable):
    # idle | listening | finishing | failed
    phase: str = observed("idle")
    message: str = observed("")
    transcript: str = observed("")
    level: float = observed(0.0)

    def __init__(self, settings: Any) -> None:
        super().__init__()
        self.settings = settings
        self.on_finished: Optional[Callable[[str], None]] = None
        self.on_failed: Optional[Callable[[], None]] = None
        self._recorder: Optional[subprocess.Popen] = None
        self._worker: Optional[subprocess.Popen] = None
        self._watches: list = []
        self._pcm = bytearray()
        self._batch = False
        self._finish_timeout: Optional[Task] = None
        self._failure_reset: Optional[Task] = None
        self._generation = 0
        self._worker_buffer = b""

    @property
    def is_active(self) -> bool:
        return self.phase in ("listening", "finishing")

    def begin(self) -> None:
        if self.is_active:
            return
        if self._failure_reset is not None:
            self._failure_reset.cancel()
        self.transcript = ""
        self.level = 0.0
        self.message = ""
        self.phase = "listening"
        self._pcm = bytearray()
        self._generation += 1
        recorder = recorder_command()
        if recorder is None:
            self._fail("No audio recorder found. Install pipewire, pulseaudio-utils, or alsa-utils.")
            return
        engine = self.settings.get("speech_engine")
        self._batch = engine == "command"
        if engine == "command":
            if not self.settings.get("speech_command").strip():
                self._fail("Set a transcription command in Settings › Voice.")
                return
        elif not self._start_worker():
            return
        try:
            self._recorder = subprocess.Popen(recorder, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as error:
            self._fail(f"Couldn't start the microphone: {error}")
            return
        self._watch(self._recorder.stdout, self._audio_readable)

    def _start_worker(self) -> bool:
        language = self.settings.get("speech_language")
        path = model_path(language)
        if not is_engine_installed() or path is None or not is_model_installed(language):
            self._fail("Install speech recognition in Settings › Voice first.")
            return False
        script = str(package_dir() / "voice" / "worker.py")
        env = dict(os.environ, CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS="2")
        try:
            self._worker = subprocess.Popen(
                [str(speech_python()), "-I", script, str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            )
        except OSError as error:
            self._fail(f"Couldn't start speech recognition: {error}")
            return False
        self._worker_buffer = b""
        os.set_blocking(self._worker.stdin.fileno(), True)
        self._watch(self._worker.stdout, self._worker_readable)
        return True

    def _watch(self, stream: Any, callback: Callable) -> None:
        fd = stream.fileno()
        os.set_blocking(fd, False)
        generation = self._generation
        source = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, fd, GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                                       lambda _fd, condition: callback(stream, condition, generation))
        self._watches.append(source)

    def _audio_readable(self, stream: Any, _condition: Any, generation: int) -> bool:
        if generation != self._generation:
            return GLib.SOURCE_REMOVE
        try:
            chunk = stream.read(8192)
        except OSError:
            chunk = b""
        if chunk is None:
            return GLib.SOURCE_CONTINUE
        if not chunk:
            if self.phase == "listening":
                stderr = b""
                if self._recorder is not None and self._recorder.stderr is not None:
                    try:
                        stderr = self._recorder.stderr.read() or b""
                    except OSError:
                        pass
                detail = stderr.decode(errors="replace").strip().splitlines()
                log.error("Recorder stopped: %s", detail[-1] if detail else "no output")
                self._fail("The microphone stopped unexpectedly.")
            return GLib.SOURCE_REMOVE
        self.level = rms_level(chunk[-1600:])
        if self._batch:
            self._pcm.extend(chunk)
        elif self._worker is not None and self._worker.stdin is not None:
            try:
                self._worker.stdin.write(chunk)
                self._worker.stdin.flush()
            except (BrokenPipeError, ValueError):
                self._fail("Speech recognition stopped unexpectedly.")
                return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _worker_readable(self, stream: Any, _condition: Any, generation: int) -> bool:
        if generation != self._generation:
            return GLib.SOURCE_REMOVE
        try:
            chunk = stream.read(65536)
        except OSError:
            chunk = b""
        if chunk is None:
            return GLib.SOURCE_CONTINUE
        if not chunk:
            if self.phase == "finishing":
                self._deliver()
            elif self.phase == "listening":
                self._fail("Speech recognition stopped unexpectedly.")
            return GLib.SOURCE_REMOVE
        self._worker_buffer += chunk
        *lines, self._worker_buffer = self._worker_buffer.split(b"\n")
        for line in lines:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "error" in message:
                log.error("Speech worker: %s", message["error"])
                self._fail("Speech recognition isn't set up correctly. Reinstall it in Settings › Voice.")
                return GLib.SOURCE_REMOVE
            if message.get("partial"):
                self.transcript = message["partial"]
            if "final" in message:
                if message["final"]:
                    self.transcript = message["final"]
                if self.phase == "finishing":
                    self._deliver()
                return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def end(self) -> None:
        if self.phase != "listening":
            return
        self.phase = "finishing"
        self._stop_recorder()
        if self._batch:
            spawn(self._transcribe_batch())
            return
        if self._worker is not None and self._worker.stdin is not None:
            try:
                self._worker.stdin.close()
            except OSError:
                pass

        def timeout():
            yield sleep(4)
            self._deliver()

        self._finish_timeout = spawn(timeout())

    def _transcribe_batch(self):
        generation = self._generation
        pcm = bytes(self._pcm)
        command = self.settings.get("speech_command")

        def run() -> str:
            with tempfile.TemporaryDirectory(prefix="barclaw-voice-") as directory:
                path = Path(directory) / "speech.wav"
                with wave.open(str(path), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(SAMPLE_RATE)
                    output.writeframes(pcm)
                argv = [part.replace("{file}", str(path)) for part in shlex.split(command)]
                if not any(str(path) in part for part in argv):
                    argv.append(str(path))
                done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
                if done.returncode != 0:
                    raise ExpectedError(done.stderr.strip()[-200:] or f"exit status {done.returncode}")
                return done.stdout.strip()

        try:
            text = yield in_thread(run)
        except (ExpectedError, OSError, subprocess.SubprocessError, ValueError) as error:
            if generation == self._generation:
                self._fail(f"Transcription failed: {error}")
            return
        if generation == self._generation:
            self.transcript = text
            self._deliver()

    def cancel(self) -> None:
        self._generation += 1
        self._stop_recorder()
        self._stop_worker()
        self.phase = "idle"

    def _deliver(self) -> None:
        if self.phase != "finishing":
            return
        if self._finish_timeout is not None:
            self._finish_timeout.cancel()
            self._finish_timeout = None
        self._generation += 1
        self._stop_worker()
        text = self.transcript.strip()
        if not text:
            self._fail("Didn't catch that — hold the shortcut and try again.", reset_after=2.5)
            return
        self.phase = "idle"
        if self.on_finished:
            self.on_finished(text)

    def _fail(self, message: str, reset_after: float = 4) -> None:
        self._generation += 1
        self._stop_recorder()
        self._stop_worker()
        self.message = message
        self.phase = "failed"
        self.level = 0.0
        if self.on_failed:
            self.on_failed()
        if self._failure_reset is not None:
            self._failure_reset.cancel()

        def reset():
            yield sleep(reset_after)
            if self.phase == "failed":
                self.phase = "idle"

        self._failure_reset = spawn(reset())

    def _stop_recorder(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            recorder.terminate()
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, recorder.pid, lambda *_: recorder.poll())
        self.level = 0.0

    def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            if worker.stdin is not None:
                try:
                    worker.stdin.close()
                except OSError:
                    pass
            worker.terminate()
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, worker.pid, lambda *_: worker.poll())


class SpeechInstaller(Observable):
    """Creates BarClaw's private Python environment for Vosk and downloads a small language model."""

    state: str = observed("idle")  # idle | working | failed
    detail: str = observed("")

    def install(self, language: str) -> None:
        if self.state == "working":
            return
        self.state, self.detail = "working", "Setting up…"
        spawn(self._install(language))

    def _install(self, language: str):
        try:
            if not is_engine_installed():
                self.detail = "Installing the speech engine…"
                yield in_thread(self._make_environment)
            if not is_model_installed(language):
                self.detail = f"Downloading the {MODELS[language][0]} model (about 50 MB)…"
                yield in_thread(self._download_model, language)
            self.state, self.detail = "idle", "Installed"
        except (OSError, subprocess.SubprocessError, ValueError, ExpectedError) as error:
            log.error("Speech install failed: %s", error)
            self.state, self.detail = "failed", str(error)

    @staticmethod
    def _make_environment() -> None:
        target = speech_dir() / "venv"
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", "--clear", str(target)], check=True, capture_output=True, timeout=180)
        subprocess.run([str(target / "bin" / "pip"), "install", "--quiet", "--disable-pip-version-check", "vosk==0.3.45"], check=True, capture_output=True, timeout=300)

    @staticmethod
    def _download_model(language: str) -> None:
        import urllib.request
        import zipfile

        name = MODELS[language][1]
        models = speech_dir() / "models"
        models.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=models, suffix=".zip") as archive:
            with urllib.request.urlopen(f"{MODEL_BASE_URL}/{name}.zip", timeout=60) as response:
                shutil.copyfileobj(response, archive)
            archive.flush()
            with zipfile.ZipFile(archive.name) as bundle:
                for member in bundle.namelist():
                    # A model archive holds one top-level directory; anything else is not a model.
                    if not member.startswith(name + "/") or ".." in Path(member).parts:
                        raise ExpectedError("The model archive had unexpected contents.")
                bundle.extractall(models)
