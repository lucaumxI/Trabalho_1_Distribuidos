"""
Controle de Qualidade de Serviço (QoS) por tipo de mídia.

- Texto:  garantia de entrega via retry com ACK
- Vídeo:  taxa adaptativa + drop de frames antigos
- Áudio:  baixa latência, descarta chunks antigos se fila crescer
"""

import queue
import time
import threading

from config import (
    TEXTO_RETRY_MAX, TEXTO_RETRY_TIMEOUT_S,
    VIDEO_ADAPTIVE_QUEUE_HIGH, VIDEO_ADAPTIVE_QUEUE_LOW,
    VIDEO_JPEG_QUALITY, VIDEO_MIN_QUALITY,
    AUDIO_MAX_QUEUE,
)


# ---------------------------------------------------------------------------
# Texto — entrega confiável
# ---------------------------------------------------------------------------
class TextoReliableSender:
    """Armazena mensagens enviadas e reenvia se não receber ACK a tempo."""

    def __init__(self):
        self._pending: dict[int, dict] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def registrar(self, seq: int, payload: str):
        with self._lock:
            self._pending[seq] = {
                "payload": payload,
                "tentativas": 1,
                "enviado_em": time.time(),
            }

    def confirmar(self, seq: int):
        with self._lock:
            self._pending.pop(seq, None)

    def pendentes_para_reenvio(self) -> list[tuple[int, str]]:
        agora = time.time()
        reenviar = []
        remover = []
        with self._lock:
            for seq, info in self._pending.items():
                if agora - info["enviado_em"] > TEXTO_RETRY_TIMEOUT_S:
                    if info["tentativas"] >= TEXTO_RETRY_MAX:
                        remover.append(seq)
                    else:
                        info["tentativas"] += 1
                        info["enviado_em"] = agora
                        reenviar.append((seq, info["payload"]))
            for seq in remover:
                self._pending.pop(seq, None)
        return reenviar


# ---------------------------------------------------------------------------
# Vídeo — taxa adaptativa
# ---------------------------------------------------------------------------
class VideoAdaptiveBuffer:
    """Ajusta qualidade JPEG com base no tamanho da fila de saída."""

    def __init__(self):
        self.quality = VIDEO_JPEG_QUALITY

    def ajustar(self, fila: queue.Queue):
        sz = fila.qsize()
        if sz > VIDEO_ADAPTIVE_QUEUE_HIGH:
            self.quality = max(VIDEO_MIN_QUALITY, self.quality - 5)
        elif sz < VIDEO_ADAPTIVE_QUEUE_LOW:
            self.quality = min(VIDEO_JPEG_QUALITY, self.quality + 5)

    def drop_antigos(self, fila: queue.Queue, max_size: int = 10):
        """Remove frames antigos mantendo no máximo *max_size* na fila."""
        while fila.qsize() > max_size:
            try:
                fila.get_nowait()
            except queue.Empty:
                break


# ---------------------------------------------------------------------------
# Áudio — baixa latência
# ---------------------------------------------------------------------------
def audio_drop_antigos(fila: queue.Queue):
    """Descarta chunks antigos se fila ultrapassar AUDIO_MAX_QUEUE."""
    while fila.qsize() > AUDIO_MAX_QUEUE:
        try:
            fila.get_nowait()
        except queue.Empty:
            break
