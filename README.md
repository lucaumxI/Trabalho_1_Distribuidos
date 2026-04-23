# Sistema de Videoconferência Distribuído — ZeroMQ

Sistema de videoconferência desktop em Python 3 com arquitetura distribuída,
múltiplos brokers, service discovery, tolerância a falhas e QoS por tipo de mídia.

## Instalação

```bash
git clone https://github.com/lucaumxI/Trabalho_1_Distribuidos.git
cd Trabalho_1_Distribuidos
git checkout Rafael
python3 -m venv .venv
source .venv/bin/activate        # Linux/Mac
# .venv\Scripts\activate         # Windows
pip install -r requirements.txt
```

## Execução

Em terminais separados:

```bash
# 1. Registry (service discovery)
python registry.py

# 2. Broker 1
python broker.py --broker-id B1 --port-base 5555

# 3. Broker 2 (opcional, para testar distribuição)
python broker.py --broker-id B2 --port-base 5575

# 4. Clientes
python client.py Alice SALA_A
python client.py Bob SALA_A
```

## Demo automatizada

```bash
python demo.py
```

Simula: 2 brokers + comunicação inter-broker + queda de broker + failover.

## Testes

```bash
python -m unittest test_captura.py -v
```

## Arquitetura

Veja `documento_tecnico.md` para documentação completa.

```
Registry (REQ/REP)  ◄──  Brokers se registram
    │
    ├── Broker B1 (XPUB/XSUB × 3 canais + heartbeat + inter-broker)
    │     └── Clientes PUB/SUB
    │
    └── Broker B2 (XPUB/XSUB × 3 canais + heartbeat + inter-broker)
          └── Clientes PUB/SUB
```
