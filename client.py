"""
client.py — Cliente de videoconferência (Fase 1).

Status dos requisitos cobertos neste arquivo:
  [DONE]    RF01: login via ID único — validação de unicidade no broker
            (ClientePresenca.login).
  [DONE]    RF02: presença — lista LIST + eventos ONLINE/OFFLINE via SUB,
            mantidos em ClientePresenca.online.
  [DONE]    RF03: entrada/saída de salas (JOIN/LEAVE) + lista de membros por
            sala via eventos. Falta ainda prefixar SALA nos sends de mídia
            (item separado de RF04/RF05).
  [PARCIAL] RF04: captura feita (capturaImagemeAudio); envio e recepção
            incompletos (pubPacotes não envia vídeo/áudio; subPacotes vazio).
  [DONE]    RF05: canais separados (sockets distintos por mídia).
  [TODO]    RF06: IPs 127.0.0.1 hardcoded em pubPacotes — viola requisito.
  [TODO]    RF07: integração com service discovery (ainda não existe).
  [TODO]    RF08: seleção de broker (round-robin / menor latência).

  [TODO]    RNF01: retry de texto não implementado.
  [DONE]    RNF02: áudio em PUB/SUB (baixa latência).
  [PARCIAL] RNF03: drop de frames delegado ao broker (HWM); falta taxa
            adaptativa no cliente.
  [DONE]    RNF04: uso de threads para async.
  [PARCIAL] RNF05: (1) Captura DONE (2 sub-threads), (2) Envio DONE,
            (3) Recepção thread criada mas função vazia, (4) Renderização
            apenas placeholder.
  [DONE]    RNF06/RNF07: Python 3 + ZeroMQ.

  [TODO]    ARQ04/ARQ05/ARQ06: heartbeat, timeouts e failover.
"""

import zmq
import threading
import queue
import time
import cv2
import pyaudio

from presenca import ClientePresenca, CTRL_PORT, PRESENCE_PORT

global ID

BROKER_HOST = "127.0.0.1"   # [TODO] RF06: substituir por service discovery.
SALAS_VALIDAS = [chr(c) for c in range(ord("A"), ord("K") + 1)]  # Grupos A–K

# Parâmetros de captura (defaults)
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 480
VIDEO_FPS = 30
VIDEO_JPEG_QUALITY = 70

AUDIO_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt16
AUDIO_CHUNK = 1024


def _captura_video(fila_video, parar_evento):
    import sys
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

            ok, buffer = cv2.imencode(".jpg", frame, encode_params)
            if ok:
                fila_video.put(buffer.tobytes())

            dt = time.time() - inicio
            if dt < intervalo:
                time.sleep(intervalo - dt)
    finally:
        cap.release()


def _captura_audio(fila_audio, parar_evento):
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


# [DONE] RNF05.1 (Captura) — webcam + microfone em sub-threads dedicadas.
def capturaImagemeAudio(contexto, fila_video, fila_audio, parar_evento=None):
    if parar_evento is None:
        parar_evento = threading.Event()

    t_video = threading.Thread(
        target=_captura_video, args=(fila_video, parar_evento), daemon=True
    )
    t_audio = threading.Thread(
        target=_captura_audio, args=(fila_audio, parar_evento), daemon=True
    )

    t_video.start()
    t_audio.start()

    print("IMAGEM E AUDIO CAPTURADOS")

    try:
        while not parar_evento.is_set():
            if not t_video.is_alive() and not t_audio.is_alive():
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        parar_evento.set()

    t_video.join(timeout=2)
    t_audio.join(timeout=2)

# [TODO] RNF05.4 (Renderização) — montar GUI que consome as filas _sub.
# [TODO] RNF08: interface desktop (Tkinter / PyQt / OpenCV imshow).
def renderizacaoInterface(contexto): # não sei o que vai receber de argumentos, provavelmente essa vai ser a última a ser implementada e o texto creio eu que vai ser capturado aqui
    #implementação
    print("INTERFACE RENDERIZADA")

# [PARCIAL] RNF05.2 (Envio) — thread existe e envia texto; vídeo/áudio ainda
# não são enviados (frames pegos da fila e descartados com `pass`).
def pubPacotes(contexto, fila_video, fila_audio, fila_texto, ID, SALA):
    # [TODO] RF06: substituir IPs hardcoded por endpoints obtidos do service discovery.
    # [TODO] RF08: implementar seleção de broker (round-robin ou menor latência).
    video_pub = contexto.socket(zmq.PUB)
    video_pub.connect("tcp://127.0.0.1:5555")   # todo: RF06

    audio_pub = contexto.socket(zmq.PUB)
    audio_pub.connect("tcp://127.0.0.1:5557")

    texto_dealer = contexto.socket(zmq.DEALER)
    texto_dealer.connect("tcp://127.0.0.1:5559")

    time.sleep(1) 

    print("Cliente PUB Iniciado")

    while True:
        try:
            frame = fila_video.get(timeout=0.01)
            video_pub.send_multipart([SALA.encode(), ID.encode(), frame])
        except queue.Empty:
            pass # Fila estava vazia, segue o jogo
        try:
            audio = fila_audio.get(timeout=0.01)
            audio_pub.send_multipart([SALA.encode(), ID.encode(), audio])
        except queue.Empty:
            pass

        # [TODO] RNF01: adicionar política de retry (ACK + reenvio) para chat.
        try:
            texto = fila_texto.get(timeout=0.01)
            texto_dealer.send_string(f"{SALA} {ID} {texto}")
        except queue.Empty:
            pass

# [TODO] RNF05.3 (Recepção) — implementar.
# Deve conectar em: XPUB vídeo (5556), XPUB áudio (5558), DEALER texto (5560)
# e preencher as filas _sub para a thread de renderização consumir.
# [TODO] RF03: setsockopt(zmq.SUBSCRIBE, SALA) para filtrar pelo grupo do usuário.
def subPacotes(contexto, fila_video, fila_audio, fila_texto, SALA):
    print("Pacotes recebidos")


def fazer_login(cp: ClientePresenca) -> str:
    """[DONE] RF01 — pede ID e retenta até o broker aceitar (unicidade)."""
    while True:
        ID = input("Digite seu ID: ").strip()
        if not ID:
            print("  ID vazio, tente novamente.")
            continue
        resp = cp.login(ID)
        print(f"  broker: {resp}")
        if resp.startswith("OK"):
            return ID


def escolher_sala(cp: ClientePresenca) -> str:
    """[DONE] RF03 — escolhe uma das salas A–K e faz JOIN."""
    print(f"Salas disponíveis: {', '.join(SALAS_VALIDAS)}")
    while True:
        sala = input("Entre em uma sala: ").strip().upper()
        if sala not in SALAS_VALIDAS:
            print(f"  Sala inválida. Escolha entre {SALAS_VALIDAS}.")
            continue
        resp = cp.join(sala)
        print(f"  broker: {resp}")
        if resp.startswith("OK"):
            return sala


def menu_controle(cp: ClientePresenca, parar_evento: threading.Event) -> None:
    """[DONE] RF02/RF03 — loop interativo: listar online, entrar/sair de salas."""
    ajuda = (
        "\nComandos: [l] listar online  [s] listar sala  [j <S>] join  "
        "[x <S>] leave  [q] sair\n"
    )
    print(ajuda)
    try:
        while not parar_evento.is_set():
            linha = input("> ").strip()
            if not linha:
                continue
            partes = linha.split()
            cmd = partes[0].lower()
            if cmd == "q":
                break
            elif cmd == "l":
                online = cp.list_online()
                if not online:
                    print("  (ninguém online)")
                else:
                    for uid, salas in sorted(online.items()):
                        marca = " (você)" if uid == cp.ID else ""
                        salas_txt = ",".join(salas) if salas else "-"
                        print(f"  - {uid}{marca}  salas: {salas_txt}")
            elif cmd == "s":
                sala = partes[1].upper() if len(partes) > 1 else (
                    next(iter(cp.salas), "")
                )
                if not sala:
                    print("  uso: s <SALA>")
                    continue
                membros = cp.list_sala(sala)
                print(f"  sala {sala}: {membros or '(vazia)'}")
            elif cmd == "j" and len(partes) == 2:
                print(f"  broker: {cp.join(partes[1].upper())}")
            elif cmd == "x" and len(partes) == 2:
                print(f"  broker: {cp.leave(partes[1].upper())}")
            else:
                print(ajuda)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        parar_evento.set()


def main():
    contexto = zmq.Context()

    # [DONE] RF01/RF02/RF03 — serviço de identidade e presença.
    cp = ClientePresenca(
        contexto,
        f"tcp://{BROKER_HOST}:{CTRL_PORT}",
        f"tcp://{BROKER_HOST}:{PRESENCE_PORT}",
    )
    ID = fazer_login(cp)
    SALA = escolher_sala(cp)

    # Filas de Saída (Upload)
    fila_video_pub = queue.Queue()
    fila_audio_pub = queue.Queue()
    fila_texto_pub = queue.Queue()

    # Filas de Entrada (Download)
    fila_video_sub = queue.Queue()
    fila_audio_sub = queue.Queue()
    fila_texto_sub = queue.Queue()

    parar_evento = threading.Event()

    t_captura = threading.Thread(
        target=capturaImagemeAudio,
        args=(contexto, fila_video_pub, fila_audio_pub, parar_evento),
        daemon=True,
    )
    t_envio = threading.Thread(
        target=pubPacotes,
        args=(contexto, fila_video_pub, fila_audio_pub, fila_texto_pub, ID, SALA),
        daemon=True,
    )
    t_recep = threading.Thread(
        target=subPacotes,
        args=(contexto, fila_video_sub, fila_audio_sub, fila_texto_sub, SALA),
        daemon=True,
    )

    t_captura.start()
    t_envio.start()
    t_recep.start()

    try:
        menu_controle(cp, parar_evento)
    finally:
        print("\nEncerrando o cliente...")
        parar_evento.set()
        try:
            print(f"  broker: {cp.logout()}")
        except Exception as e:
            print(f"  logout falhou: {e}")
        cp.close()
        contexto.term()

if __name__ == "__main__":
    main()
