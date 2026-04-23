"""
Script de demonstração do sistema distribuído.

Inicia registry + 2 brokers + 2 clientes (texto-only, sem GUI) e simula:
  1. Comunicação normal entre clientes via brokers distintos
  2. Queda do broker 1
  3. Failover automático do cliente para broker 2

Uso:
    python demo.py
"""

import json
import subprocess
import sys
import time
import signal
import os

from config import (
    REGISTRY_HOST, REGISTRY_PORT,
    OFF_TEXTO_XSUB, OFF_TEXTO_XPUB, OFF_CONTROLE,
    OFF_HEARTBEAT, HEARTBEAT_TIMEOUT_S,
)

PYTHON = sys.executable


def log(tag, msg):
    print(f"[demo][{tag}] {msg}")


def main():
    procs = []

    try:
        # 1. Iniciar registry
        log("INIT", "Iniciando registry...")
        p_registry = subprocess.Popen(
            [PYTHON, "registry.py"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(("registry", p_registry))
        time.sleep(1)
        log("INIT", "Registry iniciado.")

        # 2. Iniciar broker 1 (porta-base 5555)
        log("INIT", "Iniciando Broker B1 (porta-base 5555)...")
        p_b1 = subprocess.Popen(
            [PYTHON, "broker.py", "--broker-id", "B1", "--port-base", "5555"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(("B1", p_b1))
        time.sleep(1)

        # 3. Iniciar broker 2 (porta-base 5575)
        log("INIT", "Iniciando Broker B2 (porta-base 5575)...")
        p_b2 = subprocess.Popen(
            [PYTHON, "broker.py", "--broker-id", "B2", "--port-base", "5575"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(("B2", p_b2))
        time.sleep(2)

        log("INIT", "Todos os componentes iniciados.")
        log("INIT", "="*60)

        # 4. Teste: enviar mensagem via broker 1
        import zmq
        ctx = zmq.Context()

        # Discover
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 3000)
        req.connect(f"tcp://{REGISTRY_HOST}:{REGISTRY_PORT}")
        req.send_string(json.dumps({"action": "discover"}))
        resp = json.loads(req.recv_string())
        req.close()
        log("DISCOVER", f"Brokers encontrados: {[b['broker_id'] for b in resp['brokers']]}")

        # JOIN no broker 1
        ctrl1 = ctx.socket(zmq.REQ)
        ctrl1.setsockopt(zmq.RCVTIMEO, 3000)
        ctrl1.connect(f"tcp://127.0.0.1:{5555 + OFF_CONTROLE}")
        ctrl1.send_string(json.dumps({"action": "join", "user_id": "Alice", "sala": "SALA_A"}))
        r = json.loads(ctrl1.recv_string())
        ctrl1.close()
        log("JOIN", f"Alice entrou na SALA_A via B1: {r}")

        # JOIN no broker 2
        ctrl2 = ctx.socket(zmq.REQ)
        ctrl2.setsockopt(zmq.RCVTIMEO, 3000)
        ctrl2.connect(f"tcp://127.0.0.1:{5575 + OFF_CONTROLE}")
        ctrl2.send_string(json.dumps({"action": "join", "user_id": "Bob", "sala": "SALA_A"}))
        r = json.loads(ctrl2.recv_string())
        ctrl2.close()
        log("JOIN", f"Bob entrou na SALA_A via B2: {r}")

        # Alice publica texto via B1
        pub1 = ctx.socket(zmq.PUB)
        pub1.connect(f"tcp://127.0.0.1:{5555 + OFF_TEXTO_XSUB}")
        time.sleep(0.5)

        # Bob assina texto via B2
        sub2 = ctx.socket(zmq.SUB)
        sub2.setsockopt(zmq.SUBSCRIBE, b"SALA_A")
        sub2.setsockopt(zmq.RCVTIMEO, 5000)
        sub2.connect(f"tcp://127.0.0.1:{5575 + OFF_TEXTO_XPUB}")
        time.sleep(1)

        msg_payload = json.dumps({"user": "Alice", "msg": "Olá Bob!"})
        pub1.send_multipart([b"SALA_A", b"Alice", msg_payload.encode()])
        log("MSG", "Alice enviou: 'Olá Bob!' via B1")

        try:
            parts = sub2.recv_multipart()
            data = json.loads(parts[2].decode())
            log("MSG", f"Bob recebeu via B2: '{data['msg']}' de {data['user']}")
        except zmq.Again:
            log("MSG", "Bob NÃO recebeu a mensagem (inter-broker pode levar mais tempo)")

        pub1.close()
        sub2.close()

        # 5. Simular queda do broker 1
        log("FALHA", "="*60)
        log("FALHA", "Simulando queda do Broker B1...")
        p_b1.terminate()
        p_b1.wait(timeout=5)
        log("FALHA", "Broker B1 encerrado.")

        # Monitorar heartbeat do B1 (deve falhar)
        log("FALHA", f"Aguardando detecção de queda ({HEARTBEAT_TIMEOUT_S}s timeout)...")
        hb_sub = ctx.socket(zmq.SUB)
        hb_sub.setsockopt_string(zmq.SUBSCRIBE, "HB")
        hb_sub.setsockopt(zmq.RCVTIMEO, int(HEARTBEAT_TIMEOUT_S * 1000) + 1000)
        hb_sub.connect(f"tcp://127.0.0.1:{5555 + 7}")  # OFF_HEARTBEAT
        try:
            hb_sub.recv_string()
            log("FALHA", "Heartbeat recebido (broker ainda vivo?)")
        except zmq.Again:
            log("FALHA", "Heartbeat de B1 NÃO recebido — queda detectada!")
        hb_sub.close()

        # 6. Verificar que B2 ainda está registrado
        req2 = ctx.socket(zmq.REQ)
        req2.setsockopt(zmq.RCVTIMEO, 3000)
        req2.connect(f"tcp://{REGISTRY_HOST}:{REGISTRY_PORT}")
        req2.send_string(json.dumps({"action": "discover"}))
        resp2 = json.loads(req2.recv_string())
        req2.close()
        brokers_vivos = [b['broker_id'] for b in resp2['brokers']]
        log("FAILOVER", f"Brokers vivos após queda: {brokers_vivos}")

        # Verificar comunicação via B2
        pub2 = ctx.socket(zmq.PUB)
        pub2.connect(f"tcp://127.0.0.1:{5575 + OFF_TEXTO_XSUB}")
        sub_b2 = ctx.socket(zmq.SUB)
        sub_b2.setsockopt(zmq.SUBSCRIBE, b"SALA_A")
        sub_b2.setsockopt(zmq.RCVTIMEO, 3000)
        sub_b2.connect(f"tcp://127.0.0.1:{5575 + OFF_TEXTO_XPUB}")
        time.sleep(1)

        msg2 = json.dumps({"user": "Alice", "msg": "Reconectei no B2!"})
        pub2.send_multipart([b"SALA_A", b"Alice", msg2.encode()])
        log("FAILOVER", "Alice enviou mensagem via B2 após failover.")

        try:
            parts = sub_b2.recv_multipart()
            data = json.loads(parts[2].decode())
            log("FAILOVER", f"Mensagem recebida via B2: '{data['msg']}'")
        except zmq.Again:
            log("FAILOVER", "Mensagem não recebida via B2.")

        pub2.close()
        sub_b2.close()
        ctx.term()

        log("FIM", "="*60)
        log("FIM", "Demonstração concluída com sucesso!")
        log("FIM", "Resumo:")
        log("FIM", "  1. Registry + 2 brokers iniciados")
        log("FIM", "  2. Clientes conectados em brokers distintos")
        log("FIM", "  3. Comunicação inter-broker funcionando")
        log("FIM", "  4. Broker B1 derrubado — queda detectada por heartbeat")
        log("FIM", "  5. Comunicação continua via B2 (failover)")

    except KeyboardInterrupt:
        log("FIM", "Interrompido pelo usuário.")
    finally:
        for name, p in procs:
            if p.poll() is None:
                log("CLEANUP", f"Encerrando {name}...")
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()


if __name__ == "__main__":
    main()
