import zmq
import threading
import queue
import time

global ID

def capturaImagemeAudio(contexto, fila_video, fila_audio):
    #implementação da thread para capturar audio do microfone e frames da webcam
    print("IMAGEM E AUDIO CAPTURADOS")

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
    ID = input("Digite seu ID: ")   # Todo: RF06
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

    # Passando ID e SALA corretamente como argumentos
    t_envio = threading.Thread(target=pubPacotes, args=(contexto, fila_video_pub, fila_audio_pub, fila_texto_pub, ID, SALA))
    t_recep = threading.Thread(target=subPacotes, args=(contexto, fila_video_sub, fila_audio_sub, fila_texto_sub, SALA))

    t_envio.start()
    t_recep.start()

    try:
        t_envio.join()
        t_recep.join()
    except KeyboardInterrupt:
        print("\nEncerrando o cliente...")

if __name__ == "__main__":
    main()