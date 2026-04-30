# Documento Técnico — Sistema de Videoconferência Distribuído com ZeroMQ

## 1. Visão Geral

Sistema de videoconferência desktop em **Python 3** que suporta transmissão de
**Vídeo**, **Áudio** e **Texto** em salas de grupo (A–K). A arquitetura é
distribuída, com **N brokers cooperando**, **service discovery** dinâmico,
**tolerância a falhas** via heartbeat/failover, **controle de presença** robusto
com eventos em tempo real e **QoS diferenciado** por tipo de mídia.

---

## 2. Arquitetura do Sistema

```
┌──────────────────────────────────────────────────────┐
│                   Registry (REQ/REP)                 │
│                   porta 6000                         │
└──────────┬─────────────────────────────┬─────────────┘
           │ register / discover         │
     ┌─────▼──────┐              ┌──────▼─────┐
     │  Broker B1  │◄────────────►│  Broker B2  │
     │ porta 5555+ │  inter-broker│ porta 5575+ │
     └─────┬──────┘  PUB/SUB     └──────┬─────┘
           │                             │
    ┌──────▼──────┐               ┌──────▼──────┐
    │  Cliente A   │               │  Cliente B   │
    │  (Tkinter)   │               │  (Tkinter)   │
    └─────────────┘               └──────────────┘
```

### 2.1 Componentes

| Componente    | Arquivo            | Responsabilidade                                                       |
| ------------- | ------------------ | ---------------------------------------------------------------------- |
| **Registry**  | `registry.py`      | Service discovery; mantém lista de brokers ativos                      |
| **Broker**    | `broker.py`        | Roteamento de mídia (XPUB/XSUB), presença (ROUTER/PUB), heartbeat, inter-broker |
| **Presença**  | `presenca.py`      | Identidade, presença e salas — lógica pura (EstadoPresenca + handle_cmd) |
| **Cliente**   | `client.py`        | GUI Tkinter, captura, envio, recepção, failover, playback de áudio     |
| **Captura**   | `media_capture.py` | Threads de webcam (OpenCV) e microfone (PyAudio)                       |
| **QoS**       | `qos.py`           | Retry de texto, buffer adaptativo de vídeo, drop de áudio              |
| **Config**    | `config.py`        | Constantes compartilhadas (portas, timeouts, parâmetros)               |

### 2.2 Estrutura de Arquivos

```
projeto/
├── config.py            # Constantes compartilhadas
├── registry.py          # Service Discovery (REQ/REP)
├── broker.py            # Broker distribuído
├── presenca.py          # Identidade, presença e salas
├── client.py            # Cliente com GUI Tkinter
├── media_capture.py     # Captura de vídeo e áudio
├── qos.py               # QoS por tipo de mídia
├── demo.py              # Script de demonstração
├── test_captura.py      # Testes de captura
├── test_presenca.py     # Testes de presença e salas
├── requirements.txt     # Dependências
└── documento_tecnico.md # Este documento
```

---

## 3. Padrões ZeroMQ Utilizados

### 3.1 XPUB/XSUB — Canais de Mídia (Vídeo, Áudio, Texto)

```
Clientes PUB  ──►  [XSUB  Broker  XPUB]  ──►  Clientes SUB
(enviam)            (proxy)                     (recebem)
```

O padrão XPUB/XSUB com `zmq.proxy()` cria um intermediário transparente que
permite fan-out (um-para-muitos) com filtragem por tópico (prefixo `SALA_X`) e
controle de backpressure via HWM (High Water Mark).

**Portas por broker** (offset relativo à porta-base):

| Canal       | XSUB (entrada) | XPUB (saída) |
| ----------- | -------------- | ------------ |
| Vídeo       | base+0         | base+1       |
| Áudio       | base+2         | base+3       |
| Texto       | base+4         | base+5       |

**Formato das mensagens multipart**:

```
Frame 0: SALA (tópico para filtragem SUB)
Frame 1: USER_ID (remetente)
Frame 2: DADOS (JPEG / PCM / JSON)
```

### 3.2 ROUTER/PUB — Controle de Presença e Sessão

```
Cliente REQ  ──►  Broker ROUTER  (comandos síncronos)
                  Broker PUB     ──►  Cliente SUB (eventos assíncronos)
```

O broker usa ROUTER (porta base+6) para receber comandos de presença e PUB
(porta base+10) para publicar eventos em tempo real.

**Protocolo texto** (separado por espaço):

| Comando               | Resposta              | Evento publicado             |
| --------------------- | --------------------- | ---------------------------- |
| `LOGIN <id>`          | `OK LOGIN <id>`       | `PRESENCE ONLINE <id>`       |
| `LOGOUT <id>`         | `OK LOGOUT <id>`      | `PRESENCE OFFLINE <id>`      |
| `JOIN <id> <sala>`    | `OK JOIN <id> <sala>` | `SALA <sala> JOIN <id>`      |
| `LEAVE <id> <sala>`   | `OK LEAVE <id> <sala>`| `SALA <sala> LEAVE <id>`     |
| `LIST`                | `OK LIST <dados>`     | —                            |
| `LIST_SALA <sala>`    | `OK LIST_SALA ...`    | —                            |
| `HEARTBEAT <id>`      | `OK PONG`             | —                            |

### 3.3 REQ/REP — Service Discovery

```
Cliente/Broker  ──REQ──►  Registry ──REP──►  resposta JSON
```

Ações: `register`, `discover`, `unregister`.

### 3.4 PUB/SUB — Heartbeat (Broker → Cliente)

```
Broker PUB (base+7)  ──►  "HB {broker_id, timestamp}"  ──►  Cliente SUB
                                                              (timeout → failover)
```

Cada broker publica heartbeat a cada 1s. O cliente monitora: se 3s sem
heartbeat, inicia failover automático.

### 3.5 PUB/SUB — Comunicação Inter-Broker

```
Broker A PUB (base+8)  ──►  Broker B SUB
Broker B PUB (base+8)  ──►  Broker A SUB
```

Cada broker assina mensagens locais do seu XPUB, republica para outros brokers,
e injeta mensagens recebidas de outros brokers no XSUB local. O campo
`broker_id` em cada mensagem previne loops.

---

## 4. Threads do Sistema

### 4.1 Cliente (8 threads)

| Thread               | Função                                                  |
| -------------------- | ------------------------------------------------------- |
| **Captura**          | `captura_midia()` — sub-threads: webcam + microfone     |
| **Envio**            | `_thread_envio()` — PUB vídeo/áudio/texto               |
| **Recepção**         | `_thread_recepcao()` — SUB vídeo/áudio/texto            |
| **HB Broker**        | `_thread_heartbeat()` — monitora broker, failover       |
| **HB Cliente**       | `_thread_client_heartbeat()` — mantém presença no broker|
| **Presença SUB**     | `_thread_presenca_sub()` — eventos ONLINE/OFFLINE/SALA  |
| **Áudio Playback**   | `_thread_audio_playback()` — reproduz áudio via PyAudio |
| **GUI**              | Thread principal — Tkinter mainloop                     |

### 4.2 Broker (7 threads)

| Thread                 | Função                                        |
| ---------------------- | --------------------------------------------- |
| `_proxy_video`         | zmq.proxy(XSUB, XPUB) para vídeo             |
| `_proxy_audio`         | zmq.proxy(XSUB, XPUB) para áudio             |
| `_proxy_texto`         | zmq.proxy(XSUB, XPUB) para texto             |
| `_thread_controle`     | ROUTER/PUB — presença, salas, heartbeat       |
| `_thread_heartbeat`    | PUB periódico de heartbeat                    |
| `_thread_registry`     | Registro periódico no registry                |
| `_thread_inter_broker` | PUB/SUB para replicação entre brokers         |

---

## 5. Estratégia de Tolerância a Falhas

### 5.1 Heartbeat Bidirecional

O sistema usa heartbeat em duas direções:

1. **Broker → Cliente** (PUB/SUB): broker publica `HB` a cada 1s na porta
   base+7. Se o cliente não recebe por 3s, considera o broker morto e inicia
   failover.

2. **Cliente → Broker** (REQ): cliente envia `HEARTBEAT <id>` a cada 2s via
   socket de controle. Se o broker não recebe por 8s, expira o usuário da
   tabela de presença e publica evento OFFLINE.

### 5.2 Failover Automático

Ao detectar queda do broker:

1. Cliente seta flag `_reconectar_evt` → threads de envio/recepção pausam
2. Consulta registry para lista de brokers vivos
3. Seleciona novo broker (round-robin)
4. Recria socket de controle e faz LOGIN + JOIN no novo broker
5. Limpa flag → threads retomam operação

### 5.3 Garantias

- **Texto**: retry protege contra falhas de envio (socket error, buffer cheio).
  Como PUB/SUB é best-effort, mensagens em trânsito durante failover podem
  ser perdidas.
- **Vídeo/Áudio**: perda de frames/chunks é aceitável (best-effort).
- **Sessão**: preservada via re-LOGIN + re-JOIN automático no novo broker.

---

## 6. Controle de Qualidade de Serviço (QoS)

### 6.1 Texto — Retry em Falha de Envio

| Aspecto            | Implementação                                      |
| ------------------ | -------------------------------------------------- |
| **Mecanismo**      | Retry com sequência numérica + timeout             |
| **Buffer**         | `TextoReliableSender` armazena mensagens pendentes |
| **Timeout**        | 2 segundos para reenvio                            |
| **Max tentativas** | 3 (depois descarta)                                |
| **Dedup**          | Receptor ignora mensagens com `seq` já visto       |

### 6.2 Áudio — Baixa Latência

| Aspecto        | Implementação                             |
| -------------- | ----------------------------------------- |
| **Playback**   | PyAudio output stream em thread dedicada  |
| **Buffer**     | Fila limitada a 5 chunks                  |
| **Overflow**   | Descarta chunks antigos (pula pra frente) |
| **HWM broker** | 50                                        |

### 6.3 Vídeo — Taxa Adaptativa

| Aspecto         | Implementação                                        |
| --------------- | ---------------------------------------------------- |
| **Adaptação**   | `VideoAdaptiveBuffer` ajusta qualidade JPEG          |
| **Trigger**     | Fila de saída > 15 → reduz qualidade (mín. 30%)     |
| **Recuperação** | Fila < 5 → restaura qualidade (máx. 70%)            |
| **Re-encode**   | Thread de envio re-codifica JPEG na qualidade atual  |
| **Drop**        | Frames antigos descartados quando fila excede limite |
| **HWM broker**  | 10                                                   |

---

## 7. Service Discovery

### 7.1 Fluxo de Registro

1. Registry inicia na porta 6000
2. Broker B1 inicia → envia `register` com broker_id, host e port_base
3. Broker B2 inicia → envia `register`
4. Brokers renovam registro a cada 2s
5. Registry remove brokers sem renovação após 6s

### 7.2 Fluxo de Descoberta (Cliente)

1. Cliente envia `discover` ao registry
2. Registry responde com lista de brokers ativos
3. Cliente seleciona broker por round-robin
4. Cliente conecta nos sockets do broker selecionado
5. Cliente faz LOGIN + JOIN para entrar na sala

---

## 8. Identidade e Sessão

### 8.1 Login

- ID simples via argumento CLI ou input interativo
- Unicidade validada no broker via `EstadoPresenca`: LOGIN retorna ERR se
  ID já está em uso

### 8.2 Presença

- Broker mantém `EstadoPresenca` com tabela `{user_id: set(salas)}`
- Eventos em tempo real via PUB: PRESENCE ONLINE/OFFLINE, SALA JOIN/LEAVE
- Cliente mantém lista de online via SUB assíncrono
- Expiração automática de clientes inativos (heartbeat timeout 8s)

### 8.3 Salas

- 11 salas disponíveis: SALA_A até SALA_K
- Troca dinâmica via combobox na GUI
- Ao trocar: LEAVE na sala antiga → JOIN na nova → resubscribe dos canais SUB

---

## 9. Como Executar

### 9.1 Instalação

```bash
git clone https://github.com/lucaumxI/Trabalho_1_Distribuidos.git
cd Trabalho_1_Distribuidos
git checkout Rafael
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 9.2 Execução Manual

Em terminais separados:

```bash
# Terminal 1 — Registry
python registry.py

# Terminal 2 — Broker 1
python broker.py --broker-id B1 --port-base 5555

# Terminal 3 — Broker 2
python broker.py --broker-id B2 --port-base 5575

# Terminal 4 — Cliente Alice
python client.py Alice SALA_A

# Terminal 5 — Cliente Bob
python client.py Bob SALA_A
```

### 9.3 Demonstração Automatizada

```bash
python demo.py
```

Executa: registry + 2 brokers + comunicação inter-broker + queda de broker +
detecção de falha + failover.

### 9.4 Testes

```bash
python -m unittest test_captura.py test_presenca.py -v
```

---

## 10. Dependências

| Biblioteca    | Versão   | Uso                                   |
| ------------- | -------- | ------------------------------------- |
| pyzmq         | 27.1.0   | Comunicação assíncrona ZeroMQ         |
| opencv-python | 4.13.0   | Captura e encoding de vídeo (webcam)  |
| PyAudio       | 0.2.14   | Captura e playback de áudio           |
| Pillow        | 11.2.1   | Conversão de frames JPEG para Tkinter |
| numpy         | 2.4.4    | Manipulação de arrays (frames OpenCV) |
