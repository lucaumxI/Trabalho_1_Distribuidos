import zmq

def main():
    context = zmq.Context()

    # Cria os sockets de RECEPÇÃO (SUB para mídia, DEALER para texto)
    video_sub = context.socket(zmq.SUB)
    video_sub.connect("tcp://127.0.0.1:5556") # Conecta no XPUB do broker
    video_sub.setsockopt_string(zmq.SUBSCRIBE, "VIDEO_A") # Requisita APENAS pacotes com a tag VIDEO_A

    audio_sub = context.socket(zmq.SUB)
    audio_sub.connect("tcp://127.0.0.1:5558") # Conecta no XPUB do broker
    audio_sub.setsockopt_string(zmq.SUBSCRIBE, "AUDIO_A") # Requisita APENAS pacotes com a tag AUDIO_A

    texto_dealer = context.socket(zmq.DEALER)
    texto_dealer.connect("tcp://127.0.0.1:5560") # Conecta no DEALER do broker

    # Usa o Poller para escutar os 3 sockets ao mesmo tempo sem precisar de threads
    poller = zmq.Poller()
    poller.register(video_sub, zmq.POLLIN)
    poller.register(audio_sub, zmq.POLLIN)
    poller.register(texto_dealer, zmq.POLLIN)

    print("Cliente B (Assinante) iniciado. Aguardando pacotes do Cliente A...\n")

    try:
        while True:
            # O poll() trava o código aqui até que ALGUM dos 3 sockets receba algo
            socks = dict(poller.poll())

            if video_sub in socks:
                print(f"[VÍDEO] {video_sub.recv_string()}")
            
            if audio_sub in socks:
                print(f"[ÁUDIO] {audio_sub.recv_string()}")
                
            if texto_dealer in socks:
                # Lê o pacote completo (etiqueta do router + sua mensagem)
                pacotes = texto_dealer.recv_multipart()
                # Pega só a última parte (que é o texto) e converte de bytes pra string
                mensagem_real = pacotes[-1].decode('utf-8')
                print(f"[TEXTO] {mensagem_real}")

    except KeyboardInterrupt:
        print("\nEncerrando Cliente B...")
    finally:
        context.term()

if __name__ == "__main__":
    main()