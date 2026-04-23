"""
Service Discovery — Registry centralizado.

Brokers se registram periodicamente; clientes consultam a lista de brokers
ativos.  Brokers que não renovam registro dentro de HEARTBEAT_TIMEOUT_S são
removidos automaticamente.

Protocolo JSON via REQ/REP na porta REGISTRY_PORT.

Ações aceitas:
  {"action": "register", "broker_id": "...", "host": "...", "port_base": N}
  {"action": "discover"}
  {"action": "unregister", "broker_id": "..."}
"""

import json
import time
import threading
import argparse

import zmq

from config import REGISTRY_HOST, REGISTRY_PORT, HEARTBEAT_TIMEOUT_S


class Registry:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._brokers: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---- limpeza periódica ------------------------------------------------
    def _limpar_expirados(self):
        agora = time.time()
        with self._lock:
            expirados = [
                bid for bid, info in self._brokers.items()
                if agora - info["last_seen"] > HEARTBEAT_TIMEOUT_S * 2
            ]
            for bid in expirados:
                print(f"[registry] Broker expirado removido: {bid}")
                del self._brokers[bid]

    def _loop_limpeza(self):
        while True:
            time.sleep(HEARTBEAT_TIMEOUT_S)
            self._limpar_expirados()

    # ---- handlers ---------------------------------------------------------
    def _handle_register(self, msg: dict) -> dict:
        bid = msg["broker_id"]
        with self._lock:
            self._brokers[bid] = {
                "broker_id": bid,
                "host": msg["host"],
                "port_base": msg["port_base"],
                "last_seen": time.time(),
            }
        print(f"[registry] Broker registrado/renovado: {bid} "
              f"({msg['host']}:{msg['port_base']})")
        return {"status": "ok"}

    def _handle_discover(self) -> dict:
        self._limpar_expirados()
        with self._lock:
            lista = [
                {"broker_id": v["broker_id"],
                 "host": v["host"],
                 "port_base": v["port_base"]}
                for v in self._brokers.values()
            ]
        return {"status": "ok", "brokers": lista}

    def _handle_unregister(self, msg: dict) -> dict:
        bid = msg["broker_id"]
        with self._lock:
            self._brokers.pop(bid, None)
        print(f"[registry] Broker removido: {bid}")
        return {"status": "ok"}

    # ---- loop principal ---------------------------------------------------
    def run(self):
        ctx = zmq.Context()
        sock = ctx.socket(zmq.REP)
        sock.bind(f"tcp://{self.host}:{self.port}")
        print(f"[registry] Escutando em tcp://{self.host}:{self.port}")

        t_limpa = threading.Thread(target=self._loop_limpeza, daemon=True)
        t_limpa.start()

        try:
            while True:
                raw = sock.recv_string()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    sock.send_string(json.dumps({"status": "error",
                                                 "msg": "json inválido"}))
                    continue

                action = msg.get("action")
                if action == "register":
                    resp = self._handle_register(msg)
                elif action == "discover":
                    resp = self._handle_discover()
                elif action == "unregister":
                    resp = self._handle_unregister(msg)
                else:
                    resp = {"status": "error", "msg": f"ação '{action}' desconhecida"}

                sock.send_string(json.dumps(resp))
        except KeyboardInterrupt:
            print("\n[registry] Encerrando...")
        finally:
            sock.close()
            ctx.term()


def main():
    parser = argparse.ArgumentParser(description="Registry (Service Discovery)")
    parser.add_argument("--host", default=REGISTRY_HOST)
    parser.add_argument("--port", type=int, default=REGISTRY_PORT)
    args = parser.parse_args()

    Registry(args.host, args.port).run()


if __name__ == "__main__":
    main()
