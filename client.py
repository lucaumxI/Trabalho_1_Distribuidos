import zmq
import threading
import queue
import time
import cv2
import pyaudio

global ID

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

def renderizacaoInterface(contexto): # não sei o que vai receber de argumentos, provavelmente essa vai ser a última a ser implementada e o texto creio eu que vai ser capturado aqui
    #implementação
    print("INTERFACE RENDERIZADA")

def pubPacotes(contexto, fila_video, fila_audio, fila_texto, ID, SALA):
    video_pub = contexto.socket(zmq.PUB)
    video_pub.connect("tcp://127.0.0.1:5555")   # todo: RF06

    audio_pub = contexto.socket(zmq.PUB)
    audio_pub.connect("tcp://127.0.0.1:5557")

    texto_dealer = contexto.socket(zmq.DEALER)
    texto_dealer.connect("tcp://127.0.0.1:5559")

    time.sleep(1) 

    print("Cliente PUB Iniciado")

    while True:
        # Tenta pegar um frame de vídeo da fila por 10 milissegundos
        try:
            frame = fila_video.get(timeout=0.01)
            # Se usar OpenCV vai chegar uma matriz numpy, precisa converter para bytes puros antes de usar a função debaixo
            # video_pub.send(frame_convertido_em_bytes)
            pass
        except queue.Empty:
            pass # Fila estava vazia, segue o jogo

        # Tenta pegar um pacote de áudio
        try:
            audio = fila_audio.get(timeout=0.01)
            # se usar o PyAudio, se eu não me engano ja vem em byter puros, aí é só descomentar a linha debaixo que ja da certo
            # mas caso não seja bytes puros vai ter que converter antes de usar
            # audio_pub.send(audio_em_bytes)
            pass
        except queue.Empty:
            pass

        # Tenta pegar um texto digitado
        try:
            texto = fila_texto.get(timeout=0.01)
            # Se for texto, envia como string com a tag do remetente
            texto_dealer.send_string(f"{SALA} {ID} {texto}")
        except queue.Empty:
            pass

def subPacotes(contexto, fila_video, fila_audio, fila_texto, SALA):
    print("Pacotes recebidos")


def main():
    ID = input("Digite seu ID: ")   # Todo: RF01
    SALA = "SALA_A"                 # Todo: RF03

    contexto = zmq.Context()

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
    )
    t_envio = threading.Thread(target=pubPacotes, args=(contexto, fila_video_pub, fila_audio_pub, fila_texto_pub, ID, SALA))
    t_recep = threading.Thread(target=subPacotes, args=(contexto, fila_video_sub, fila_audio_sub, fila_texto_sub, SALA))

    t_captura.start()
    t_envio.start()
    t_recep.start()

    try:
        t_envio.join()
        t_recep.join()
        t_captura.join()
    except KeyboardInterrupt:
        print("\nEncerrando o cliente...")
        parar_evento.set()

if __name__ == "__main__":
    main()
