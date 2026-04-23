"""
Captura de vídeo (webcam) e áudio (microfone) em threads dedicadas.
"""

import sys
import threading
import time
import queue

import cv2
import pyaudio

from config import (
    VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, VIDEO_JPEG_QUALITY,
    AUDIO_RATE, AUDIO_CHANNELS, AUDIO_FORMAT, AUDIO_CHUNK,
)


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
    pa = pyaudio.PyAudio()
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
        pa.terminate()
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
        stream.stop_stream()
        stream.close()
        pa.terminate()


def captura_midia(fila_video: queue.Queue, fila_audio: queue.Queue,
                  parar_evento: threading.Event):
    """Inicia sub-threads de captura de vídeo e áudio e aguarda término."""
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
