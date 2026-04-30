"""Captura de vídeo (webcam) e áudio (microfone) em threads dedicadas."""

import sys
import subprocess
import threading
import time
import queue

import cv2

from config import (
    VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, VIDEO_JPEG_QUALITY,
    AUDIO_RATE, AUDIO_CHANNELS, AUDIO_FORMAT, AUDIO_CHUNK,
)

# PortAudio não suporta múltiplas inicializações concorrentes (crash/abort).
# Criamos UMA instância compartilhada de PyAudio, protegida por lock.
_PA_LOCK = threading.Lock()
_PA_INSTANCE = None  # será pyaudio.PyAudio() ou None

def _testar_pyaudio() -> bool:
    """Testa PyAudio num subprocesso — se abort(), só o filho morre."""
    code = (
        "import cv2; "
        "import pyaudio; "
        "p = pyaudio.PyAudio(); "
        "p.terminate()"
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False

_PYAUDIO_OK = _testar_pyaudio()

if _PYAUDIO_OK:
    import pyaudio
else:
    pyaudio = None
    print("[media_capture] PyAudio indisponível; áudio desabilitado.")


def get_pa() -> "pyaudio.PyAudio | None":
    """Retorna a instância compartilhada de PyAudio (lazy init, thread-safe)."""
    global _PA_INSTANCE
    if not _PYAUDIO_OK:
        return None
    with _PA_LOCK:
        if _PA_INSTANCE is None:
            try:
                _PA_INSTANCE = pyaudio.PyAudio()
            except Exception as e:
                print(f"[pyaudio] Falha ao iniciar: {e}")
                return None
    return _PA_INSTANCE


def terminate_pa():
    """Marca PyAudio como encerrado. Não chama pa.terminate() porque
    PortAudio/ALSA pode dar segfault se streams já foram fechadas.
    O OS limpa os recursos quando o processo encerra."""
    global _PA_INSTANCE
    with _PA_LOCK:
        _PA_INSTANCE = None


def _captura_video(fila_video: queue.Queue, parar_evento: threading.Event):
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(0, backend)
    if not cap.isOpened():
        print("[captura_video] Webcam indisponível.")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, VIDEO_FPS)

    intervalo = 1.0 / VIDEO_FPS
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_JPEG_QUALITY]

    try:
        while not parar_evento.is_set():
            inicio = time.time()
            ok, frame = cap.read()
            if not ok:
                time.sleep(intervalo)
                continue

            ok, buf = cv2.imencode(".jpg", frame, encode_params)
            if ok:
                fila_video.put(buf.tobytes())

            dt = time.time() - inicio
            if dt < intervalo:
                time.sleep(intervalo - dt)
    finally:
        cap.release()


def _captura_audio(fila_audio: queue.Queue, parar_evento: threading.Event):
    pa = get_pa()
    if pa is None:
        return

    try:
        stream = pa.open(
            format=AUDIO_FORMAT,
            channels=AUDIO_CHANNELS,
            rate=AUDIO_RATE,
            input=True,
            frames_per_buffer=AUDIO_CHUNK,
        )
    except Exception as e:
        print(f"[captura_audio] Microfone indisponível: {e}")
        return

    try:
        while not parar_evento.is_set():
            try:
                dados = stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                fila_audio.put(dados)
            except Exception as e:
                print(f"[captura_audio] Erro na leitura: {e}")
                break
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass


def captura_midia(fila_video: queue.Queue, fila_audio: queue.Queue,
                  parar_evento: threading.Event):
    t_video = threading.Thread(
        target=_captura_video, args=(fila_video, parar_evento), daemon=True
    )
    t_audio = threading.Thread(
        target=_captura_audio, args=(fila_audio, parar_evento), daemon=True
    )

    t_video.start()
    t_audio.start()

    print("[captura] Captura de vídeo e áudio iniciada.")

    try:
        while not parar_evento.is_set():
            if not t_video.is_alive() and not t_audio.is_alive():
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        parar_evento.set()

    t_video.join(timeout=2)
    t_audio.join(timeout=2)
