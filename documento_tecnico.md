# Documento Técnico — Sistema de Videoconferência Distribuído com ZeroMQ

## 1. Visão Geral

Sistema de videoconferência desktop em **Python 3** que suporta transmissão de
**Vídeo**, **Áudio** e **Texto** em salas de grupo (A–K). A arquitetura é
distribuída, com **N brokers cooperando**, **service discovery** dinâmico,
**tolerância a falhas** via heartbeat/failover e **QoS diferenciado** por tipo
de mídia.

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


| Componente   | Arquivo            | Responsabilidade                                                             |
| ------------ | ------------------ | ---------------------------------------------------------------------------- |
| **Registry** | `registry.py`      | Service discovery; mantém lista de brokers ativos                            |
| **Broker**   | `broker.py`        | Roteamento de mídia (XPUB/XSUB), controle de sessão, heartbeat, inter-broker |
| **Cliente**  | `client.py`        | GUI Tkinter, captura, envio, recepção, failover                              |
| **Captura**  | `media_capture.py` | Threads de webcam (OpenCV) e microfone (PyAudio)                             |
| **QoS**      | `qos.py`           | Retry de texto, buffer adaptativo de vídeo, drop de áudio                    |
| **Config**   | `config.py`        | Constantes compartilhadas (portas, timeouts, parâmetros)                     |


### 2.2 Estrutura de Arquivos

```
projeto/
├── config.py            # Constantes compartilhadas
├── registry.py          # Service Discovery (REQ/REP)
├── broker.py            # Broker distribuído
├── client.py            # Cliente com GUI Tkinter
├── media_capture.py     # Captura de vídeo e áudio
├── qos.py               # QoS por tipo de mídia
├── demo.py              # Script de demonstração
├── test_captura.py      # Testes unitários
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

**Justificativa**: XPUB/XSUB com `zmq.proxy()` cria um intermediário
transparente que:

- Permite fan-out (um-para-muitos) — essencial para salas de grupo
- Propaga subscrições automaticamente do backend para o frontend
- Suporta filtragem por tópico (prefixo `SALA_X` nos frames)
- Permite HWM (High Water Mark) para controle de backpressure

**Portas por broker** (offset relativo à porta-base):


| Canal | XSUB (entrada) | XPUB (saída) |
| ----- | -------------- | ------------ |
| Vídeo | base+0         | base+1       |
| Áudio | base+2         | base+3       |
| Texto | base+4         | base+5       |


**Formato das mensagens multipart**:

```
Frame 0: SALA (tópico para filtragem SUB)
Frame 1: USER_ID (remetente)
Frame 2: DADOS (JPEG / PCM / JSON)
```

### 3.2 REQ/REP — Service Discovery e Controle de Sessão

**Registry** (porta 6000):

```
Cliente/Broker  ──REQ──►  Registry ──REP──►  resposta JSON
```

Ações do registry:

- `register` — broker se registra (chamado periodicamente)
- `discover` — cliente consulta lista de brokers ativos
- `unregister` — broker se desregistra ao encerrar

**Controle de sessão** (porta base+6 de cada broker):

```
Cliente  ──REQ──►  Broker ──REP──►  resposta JSON
```

Ações de controle:

- `join` — entrar numa sala (valida unicidade do ID)
- `leave` — sair de uma sala
- `presenca` — consultar membros de uma sala

### 3.3 PUB/SUB — Heartbeat

```
Broker  ──PUB──►  "HB {broker_id, timestamp}"  ──►  Cliente SUB
                                                       (timeout → failover)
```

Cada broker publica heartbeat a cada 1 segundo na porta base+7.
O cliente assina e monitora: se não receber por 3 segundos, inicia failover.

### 3.4 PUB/SUB — Comunicação Inter-Broker

```
Broker A PUB (base+8)  ──►  Broker B SUB
Broker B PUB (base+8)  ──►  Broker A SUB
```

Cada broker:

1. Assina mensagens locais do seu próprio XPUB (vídeo/áudio/texto)
2. Republica para outros brokers via PUB inter-broker
3. Recebe mensagens de outros brokers via SUB
4. Injeta no XSUB local para distribuição aos seus clientes

**Prevenção de loops**: cada mensagem inter-broker carrega o `broker_id`
de origem. O broker receptor ignora mensagens originadas de si mesmo.

---

## 4. Threads do Sistema

### 4.1 Cliente (5 threads)


| Thread        | Função                | Detalhe                                             |
| ------------- | --------------------- | --------------------------------------------------- |
| **Captura**   | `captura_midia()`     | Sub-threads: webcam (OpenCV) + microfone (PyAudio)  |
| **Envio**     | `_thread_envio()`     | PUB vídeo/áudio/texto para broker                   |
| **Recepção**  | `_thread_recepcao()`  | SUB vídeo/áudio/texto do broker via `zmq.Poller`    |
| **Heartbeat** | `_thread_heartbeat()` | Monitora broker, dispara failover                   |
| **GUI**       | Thread principal      | Tkinter mainloop, `root.after()` para poll de filas |


### 4.2 Broker (7 threads)


| Thread                 | Função                                |
| ---------------------- | ------------------------------------- |
| `_proxy_video`         | zmq.proxy(XSUB, XPUB) para vídeo      |
| `_proxy_audio`         | zmq.proxy(XSUB, XPUB) para áudio      |
| `_proxy_texto`         | zmq.proxy(XSUB, XPUB) para texto      |
| `_thread_controle`     | REP para JOIN/LEAVE/presença          |
| `_thread_heartbeat`    | PUB periódico de heartbeat            |
| `_thread_registry`     | Registro periódico no registry        |
| `_thread_inter_broker` | PUB/SUB para replicação entre brokers |


---

## 5. Estratégia de Tolerância a Falhas

### 5.1 Detecção de Falha

1. **Heartbeat**: broker publica `HB` a cada 1s via PUB dedicado
2. **Timeout**: cliente monitora via SUB; se 3s sem heartbeat → broker morto
3. **Registry**: também remove brokers que não renovam registro

### 5.2 Failover Automático

Ao detectar queda:

1. Cliente seta flag `_reconectar_evt` → threads de envio/recepção pausam
2. Consulta registry para lista de brokers vivos
3. Seleciona novo broker (round-robin)
4. Reconecta todos os sockets (vídeo, áudio, texto, heartbeat)
5. Re-envia `JOIN` para manter sessão na sala
6. Limpa flag → threads retomam operação

### 5.3 Garantias

- **Texto**: mensagens em trânsito durante failover podem ser perdidas, mas
o mecanismo de retry (QoS) reenvia mensagens sem ACK
- **Vídeo/Áudio**: perda de alguns frames/chunks é aceitável (best-effort)
- **Sessão**: preservada via re-JOIN automático no novo broker

---

## 6. Controle de Qualidade de Serviço (QoS)

### 6.1 Texto — Garantia de Entrega


| Aspecto            | Implementação                                      |
| ------------------ | -------------------------------------------------- |
| **Mecanismo**      | Retry com sequência numérica                       |
| **Buffer**         | `TextoReliableSender` armazena mensagens pendentes |
| **Timeout**        | 2 segundos para reenvio                            |
| **Max tentativas** | 3 (depois descarta)                                |
| **Padrão ZMQ**     | XPUB/XSUB (fan-out para sala)                      |


### 6.2 Áudio — Baixa Latência


| Aspecto        | Implementação                    |
| -------------- | -------------------------------- |
| **Prioridade** | Latência mínima, tolerando perda |
| **Buffer**     | Fila limitada a 5 chunks         |
| **Overflow**   | Descarta chunks mais antigos     |
| **HWM broker** | 50 (mais permissivo que vídeo)   |


### 6.3 Vídeo — Taxa Adaptativa


| Aspecto         | Implementação                                        |
| --------------- | ---------------------------------------------------- |
| **Adaptação**   | `VideoAdaptiveBuffer` ajusta qualidade JPEG          |
| **Trigger**     | Fila de saída > 15 → reduz qualidade (mín. 30%)      |
| **Recuperação** | Fila < 5 → restaura qualidade (máx. 70%)             |
| **Drop**        | Frames antigos descartados quando fila excede limite |
| **HWM broker**  | 10 (descarte agressivo no broker)                    |


---

## 7. Service Discovery

### 7.1 Fluxo de Registro

```
1. Registry inicia na porta 6000
2. Broker B1 inicia → envia {"action": "register", "broker_id": "B1", ...}
3. Broker B2 inicia → envia {"action": "register", "broker_id": "B2", ...}
4. Brokers renovam registro a cada 2s
5. Registry remove brokers sem renovação após 6s (2× HEARTBEAT_TIMEOUT_S)
```

### 7.2 Fluxo de Descoberta (Cliente)

```
1. Cliente envia {"action": "discover"} ao registry
2. Registry responde com lista de brokers ativos
3. Cliente seleciona broker por round-robin
4. Cliente conecta nos sockets do broker selecionado
5. Cliente envia JOIN para entrar na sala
```

### 7.3 Seleção de Broker

Implementação: **round-robin** com índice global incrementado a cada chamada.
O índice persiste entre failovers, garantindo distribuição equilibrada.

---

## 8. Identidade e Sessão

### 8.1 Login

- ID simples via argumento CLI ou input interativo
- Unicidade garantida: ao fazer JOIN, broker verifica se ID já existe em
qualquer sala e remove da anterior (transferência de sala)

### 8.2 Presença

- Broker mantém dicionário `{sala: set(user_ids)}`
- Publicação periódica de lista de presença via tópico especial no canal
de texto: `{"tipo": "presenca", "sala": "...", "membros": [...]}`

### 8.3 Salas

- 11 salas disponíveis: SALA_A até SALA_K
- Troca dinâmica via combobox na GUI
- Ao trocar: LEAVE na sala antiga → JOIN na nova → resubscribe dos canais SUB

---

## 9. Como Executar

### 9.1 Instalação

```bash
git clone <repo>
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

Executa automaticamente: registry + 2 brokers + comunicação + queda de broker

- detecção de falha + failover + comunicação pós-failover.

### 9.4 Testes Unitários

```bash
python -m unittest test_captura.py -v
```

---

## 10. Dependências


| Biblioteca    | Versão | Uso                                   |
| ------------- | ------ | ------------------------------------- |
| pyzmq         | latest | Comunicação assíncrona ZeroMQ         |
| opencv-python | latest | Captura e encoding de vídeo (webcam)  |
| PyAudio       | latest | Captura de áudio (microfone)          |
| Pillow        | latest | Conversão de frames JPEG para Tkinter |
| numpy         | latest | Manipulação de arrays (frames OpenCV) |


