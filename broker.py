import zmq
import threading

def roteador_video(context):    # Thread para roteador de vídeo
    # Cria um socket do tipo Extended Sub, ele irá receber os pacotes de todos os clientes e atua de froma bidirecional repassando flags de controle
    # para o PUB dos clientes
    frontend = context.socket(zmq.XSUB) 
    frontend.setsockopt(zmq.RCVHWM, 10) # Drop de frames. Limita a fila de entrada a 10 pacotes jogando fora o excesso
    frontend.bind("tcp://127.0.0.1:5555")       # Seta a porta 5555 para o XSUB de vídeo, os clientes irão enviar os pacotes para essa porta

    # Cria um socket do tipo XPUB que irá publicar os pacotes recebidos pelo XSUB e junto com ele faz o controle de qual nó está nesse momento pedindo acesso a determinado pacote.
    backend = context.socket(zmq.XPUB)  
    backend.setsockopt(zmq.SNDHWM, 10)  # Limita a fila de saida para 10 pacotes, esse número é arbitrário será preciso fazer testes, o do XSUB tambpém
    backend.bind("tcp://*:5556")        # Seta a porta 5556 para o XPUB de vídeo, os clientes irão conectar nessa porta para receber os pacotes
    
    print("Canal de VÍDEO (XPUB/XSUB) nas portas 5555 e 5556")
    zmq.proxy(frontend, backend)        # Essa função faz com que tudo recebido pelo frontend seja enviado ao backend e as inscrições do backend voltem para o frontend

    # Ideia desse roteador de vídeo é receber os pacotes de todos os clientes no frontend e repassar esses pacotes para o backend enviar para os clientes. Foi usado XPUB e XSUB
    # porque com eles é possível ter um controle de quem nesse momento está requisitando os dados evitando que um client A fique enviando seus pacotes para o broker sem ter alguem
    # requisitando esses pacotes

def roteador_audio(context):    # Thread para roteador de audio, bem semelhante a de vídeo porém sem o drop de frames
    frontend = context.socket(zmq.XSUB)
    frontend.bind("tcp://*:5557")

    backend = context.socket(zmq.XPUB)
    backend.bind("tcp://*:5558")
    
    print("Canal de ÁUDIO (XPUB/XSUB) nas portas 5557 e 5558")
    zmq.proxy(frontend, backend)

def roteador_texto(context):    # Roteador de texto
    # ROUTER/DEALER garantem que mensagens de texto não sejam descartadas
    # caso um cliente pisque na rede.
    frontend = context.socket(zmq.ROUTER)   # Cria um socket do tipo ROUTER
    frontend.bind("tcp://*:5559")           # Atribui a porta 5559 a ele

    backend = context.socket(zmq.DEALER)    # Cria o socket do tipo DEALER
    backend.bind("tcp://*:5560")            # atribui a porta
    
    print("Canal de TEXTO (ROUTER/DEALER) nas portas 5559 e 5560")
    zmq.proxy(frontend, backend)

    # Diferente do padrão PUB/SUB o ROUTER/DEALER não faz o descarte silencioso de pacotes, ele atua como um roteador assincrono
    # garantindo a entrega dos pacotes (mesmo que atrasados)
    # O ROUTER lê automaticamente o ID de cada cliente que se conecta a ele, permitindo rastrear de onde veio a mensagem.
    # O DEALER é o distribuidor que repassa essas mensagens.
    # A função proxy entre os dois cria uma fila em memória segura. Se a conexão de um cliente oscilar 
    # durante o envio/recebimento de um chat, o pacote de texto não é jogado no vácuo; ele fica retido 
    # na fila do ZeroMQ até ser entregue, não havendo perda de informação.

def main():
    context = zmq.Context() # Inicia o contexto do ZQM

    # Cria as threads do broker para rodar os 3 roteadores em paralelo
    t_video = threading.Thread(target=roteador_video, args=(context,))
    t_audio = threading.Thread(target=roteador_audio, args=(context,))
    t_texto = threading.Thread(target=roteador_texto, args=(context,))

    # Inicia as threads
    t_video.start()
    t_audio.start()
    t_texto.start()

    try:
        # Mantém a thread principal viva aguardando as outras
        t_video.join()
        t_audio.join()
        t_texto.join()
    except KeyboardInterrupt:
        print("\nDesligando broker central...")
    finally:
        context.term()

if __name__ == "__main__":
    main()