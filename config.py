"""Constantes compartilhadas entre cliente, broker e registry."""

# pyaudio.paInt16 == 8; usar constante fixa pra não precisar importar
# pyaudio aqui (o import pode crashar em máquinas sem dispositivo de áudio).
_AUDIO_FORMAT_PAINT16 = 8

# Registry
REGISTRY_HOST = "127.0.0.1"
REGISTRY_PORT = 6000

# Offsets de porta relativos à porta-base do broker (default 5555)
OFF_VIDEO_XSUB    = 0
OFF_VIDEO_XPUB    = 1
OFF_AUDIO_XSUB    = 2
OFF_AUDIO_XPUB    = 3
OFF_TEXTO_XSUB    = 4
OFF_TEXTO_XPUB    = 5
OFF_CONTROLE       = 6   # ROUTER para LOGIN/JOIN/LEAVE/presença
OFF_HEARTBEAT      = 7   # PUB heartbeat broker->cliente
OFF_INTER_PUB      = 8   # PUB inter-broker
OFF_INTER_SUB      = 9   # SUB inter-broker
OFF_PRESENCE_PUB   = 10  # PUB eventos de presença (ONLINE/OFFLINE/SALA)
BROKER_PORT_RANGE  = 11  # total de portas que cada broker ocupa

# Captura de mídia
VIDEO_WIDTH  = 640
VIDEO_HEIGHT = 480
VIDEO_FPS    = 30
VIDEO_JPEG_QUALITY = 70

AUDIO_RATE     = 16000
AUDIO_CHANNELS = 1
AUDIO_FORMAT   = _AUDIO_FORMAT_PAINT16
AUDIO_CHUNK    = 1024

# Heartbeat broker->cliente (failover)
HEARTBEAT_INTERVAL_S = 1.0
HEARTBEAT_TIMEOUT_S  = 3.0

# Heartbeat cliente->broker (expiração de presença)
CLIENT_HB_INTERVAL = 2.0
CLIENT_HB_TIMEOUT  = 8.0

# QoS
VIDEO_HWM = 10
AUDIO_HWM = 50
TEXTO_RETRY_MAX = 3
TEXTO_RETRY_TIMEOUT_S = 2.0
VIDEO_ADAPTIVE_QUEUE_HIGH = 15
VIDEO_ADAPTIVE_QUEUE_LOW  = 5
VIDEO_MIN_QUALITY = 30
AUDIO_MAX_QUEUE = 5

# Salas disponíveis (A-K)
SALAS = [f"SALA_{chr(c)}" for c in range(ord("A"), ord("L"))]
