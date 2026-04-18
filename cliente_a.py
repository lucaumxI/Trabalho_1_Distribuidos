import zmq
import time

def main():
    context = zmq.Context()

    # Cria os sockets de ENVIO (PUB para mídia, DEALER para texto)
    video_pub = context.socket(zmq.PUB)
    video_pub.connect("tcp://127.0.0.1:5555") # Conecta no XSUB do broker

    audio_pub = context.socket(zmq.PUB)
    audio_pub.connect("tcp://127.0.0.1:5557") # Conecta no XSUB do broker

    texto_dealer = context.socket(zmq.DEALER)
    texto_dealer.connect("tcp://127.0.0.1:5559") # Conecta no ROUTER do broker

    # Dá tempo para o ZeroMQ fechar a conexão TCP nos bastidores
    time.sleep(1)

    print("Cliente A (Publicador) iniciado. Enviando pacotes...")
    contador = 1

    try:
        while True:
            # O prefixo (tag) antes do espaço é o TÓPICO que o SUB vai usar para filtrar
            video_pub.send_string(f"VIDEO_A [FRAME_FALSO_{contador}]")
            audio_pub.send_string(f"AUDIO_A [SOM_FALSO_{contador}]")
            texto_dealer.send_string(f"TEXTO_A Ola, mensagem de texto {contador}")
            
            print(f"-> Pacote {contador} disparado nos 3 canais.")
            time.sleep(2) # Espera 2 segundos e manda de novo
            contador += 1
            
    except KeyboardInterrupt:
        print("\nEncerrando Cliente A...")
    finally:
        context.term()

if __name__ == "__main__":
    main()