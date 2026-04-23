"""
Broker de videoconferência distribuído.

Funcionalidades:
  - 3 canais XPUB/XSUB (vídeo, áudio, texto) com proxy
  - Canal de controle REP (JOIN/LEAVE/presença)
  - Heartbeat PUB periódico
  - Registro dinâmico no registry (service discovery)
  - Comunicação inter-broker PUB/SUB (replicação de mensagens entre brokers)
"""

import json
import time
import threading
import argparse

import zmq

from config import (
    REGISTRY_HOST, REGISTRY_PORT,
    OFF_VIDEO_XSUB, OFF_VIDEO_XPUB,
    OFF_AUDIO_XSUB, OFF_AUDIO_XPUB,
    OFF_TEXTO_XSUB, OFF_TEXTO_XPUB,
    OFF_CONTROLE, OFF_HEARTBEAT,
    OFF_INTER_PUB, OFF_INTER_SUB,
    HEARTBEAT_INTERVAL_S, HEARTBEAT_TIMEOUT_S,
    VIDEO_HWM, AUDIO_HWM,
)


class Broker:
    def __init__(self, broker_id: str, host: str, port_base: int,
                 registry_host: str, registry_port: int):
        self.broker_id = broker_id
        self.host = host
        self.port_base = port_base
        self.registry_host = registry_host
        self.registry_port = registry_port
        self.ctx = zmq.Context()

        self._salas: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self._parar = threading.Event()

    def _p(self, offset: int) -> int:
        return self.port_base + offset

    # ------------------------------------------------------------------
    # Registry — registro periódico
    # ------------------------------------------------------------------
    def _thread_registry(self):
        sock = self.ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 3000)
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(f"tcp://{self.registry_host}:{self.registry_port}")

        while not self._parar.is_set():
            try:
                msg = json.dumps({
                    "action": "register",
                    "broker_id": self.broker_id,
                    "host": self.host,
                    "port_base": self.port_base,
                })
                sock.send_string(msg)
                sock.recv_string()
            except zmq.Again:
                print(f"[broker {self.broker_id}] Registry não respondeu — tentando novamente")
            except zmq.ZMQError:
                break
            self._parar.wait(HEARTBEAT_INTERVAL_S * 2)

        sock.close()

    # ------------------------------------------------------------------
    # Heartbeat PUB
    # ------------------------------------------------------------------
    def _thread_heartbeat(self):
        sock = self.ctx.socket(zmq.PUB)
        sock.bind(f"tcp://*:{self._p(OFF_HEARTBEAT)}")
        print(f"[broker {self.broker_id}] Heartbeat PUB na porta {self._p(OFF_HEARTBEAT)}")

        while not self._parar.is_set():
            payload = json.dumps({
                "broker_id": self.broker_id,
                "ts": time.time(),
            })
            sock.send_string(f"HB {payload}")
            self._parar.wait(HEARTBEAT_INTERVAL_S)

        sock.close()

    # ------------------------------------------------------------------
    # Canal de controle REP — JOIN / LEAVE / presença
    # ------------------------------------------------------------------
    def _thread_controle(self):
        sock = self.ctx.socket(zmq.REP)
        sock.setsockopt(zmq.RCVTIMEO, 1000)
        sock.bind(f"tcp://*:{self._p(OFF_CONTROLE)}")
        print(f"[broker {self.broker_id}] Controle REP na porta {self._p(OFF_CONTROLE)}")

        while not self._parar.is_set():
            try:
                raw = sock.recv_string()
            except zmq.Again:
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                sock.send_string(json.dumps({"status": "error"}))
                continue

            action = msg.get("action")
            user_id = msg.get("user_id", "")
            sala = msg.get("sala", "")

            if action == "join":
                resp = self._handle_join(user_id, sala)
            elif action == "leave":
                resp = self._handle_leave(user_id, sala)
            elif action == "presenca":
                resp = self._handle_presenca(sala)
            else:
                resp = {"status": "error", "msg": "ação desconhecida"}

            sock.send_string(json.dumps(resp))

        sock.close()

    def _handle_join(self, user_id: str, sala: str) -> dict:
        with self._lock:
            if sala not in self._salas:
                self._salas[sala] = set()
            todos = set()
            for membros in self._salas.values():
                todos |= membros
            if user_id in todos:
                for s, membros in self._salas.items():
                    if user_id in membros:
                        membros.discard(user_id)
                        break
            self._salas[sala].add(user_id)
            membros_lista = sorted(self._salas[sala])
        print(f"[broker {self.broker_id}] JOIN {user_id} -> {sala} "
              f"(membros: {membros_lista})")
        return {"status": "ok", "membros": membros_lista}

    def _handle_leave(self, user_id: str, sala: str) -> dict:
        with self._lock:
            if sala in self._salas:
                self._salas[sala].discard(user_id)
        print(f"[broker {self.broker_id}] LEAVE {user_id} <- {sala}")
        return {"status": "ok"}

    def _handle_presenca(self, sala: str) -> dict:
        with self._lock:
            membros = sorted(self._salas.get(sala, set()))
        return {"status": "ok", "membros": membros}

    # ------------------------------------------------------------------
    # Proxy de presença PUB — publica periodicamente quem está em cada sala
    # ------------------------------------------------------------------
    def _thread_presenca_pub(self):
        sock = self.ctx.socket(zmq.PUB)
        porta_texto_xpub = self._p(OFF_TEXTO_XPUB)
        time.sleep(1)

        while not self._parar.is_set():
            with self._lock:
                for sala, membros in self._salas.items():
                    info = json.dumps({
                        "tipo": "presenca",
                        "sala": sala,
                        "membros": sorted(membros),
                        "broker_id": self.broker_id,
                    })
                    try:
                        sock.send_multipart([
                            sala.encode(),
                            b"__SISTEMA__",
                            info.encode(),
                        ])
                    except zmq.ZMQError:
                        pass
            self._parar.wait(2.0)

        sock.close()

    # ------------------------------------------------------------------
    # Canais de mídia XPUB/XSUB
    # ------------------------------------------------------------------
    def _proxy_video(self):
        frontend = self.ctx.socket(zmq.XSUB)
        frontend.setsockopt(zmq.RCVHWM, VIDEO_HWM)
        frontend.bind(f"tcp://*:{self._p(OFF_VIDEO_XSUB)}")

        backend = self.ctx.socket(zmq.XPUB)
        backend.setsockopt(zmq.SNDHWM, VIDEO_HWM)
        backend.bind(f"tcp://*:{self._p(OFF_VIDEO_XPUB)}")

        print(f"[broker {self.broker_id}] Vídeo XPUB/XSUB "
              f"{self._p(OFF_VIDEO_XSUB)}/{self._p(OFF_VIDEO_XPUB)}")
        zmq.proxy(frontend, backend)

    def _proxy_audio(self):
        frontend = self.ctx.socket(zmq.XSUB)
        frontend.setsockopt(zmq.RCVHWM, AUDIO_HWM)
        frontend.bind(f"tcp://*:{self._p(OFF_AUDIO_XSUB)}")

        backend = self.ctx.socket(zmq.XPUB)
        backend.setsockopt(zmq.SNDHWM, AUDIO_HWM)
        backend.bind(f"tcp://*:{self._p(OFF_AUDIO_XPUB)}")

        print(f"[broker {self.broker_id}] Áudio XPUB/XSUB "
              f"{self._p(OFF_AUDIO_XSUB)}/{self._p(OFF_AUDIO_XPUB)}")
        zmq.proxy(frontend, backend)

    def _proxy_texto(self):
        frontend = self.ctx.socket(zmq.XSUB)
        frontend.bind(f"tcp://*:{self._p(OFF_TEXTO_XSUB)}")

        backend = self.ctx.socket(zmq.XPUB)
        backend.bind(f"tcp://*:{self._p(OFF_TEXTO_XPUB)}")

        print(f"[broker {self.broker_id}] Texto XPUB/XSUB "
              f"{self._p(OFF_TEXTO_XSUB)}/{self._p(OFF_TEXTO_XPUB)}")
        zmq.proxy(frontend, backend)

    # ------------------------------------------------------------------
    # Inter-broker PUB/SUB
    # ------------------------------------------------------------------
    def _thread_inter_broker(self):
        """
        Assina mensagens de outros brokers via SUB e republica localmente.
        Também publica mensagens locais para outros brokers via PUB.
        """
        pub = self.ctx.socket(zmq.PUB)
        pub.bind(f"tcp://*:{self._p(OFF_INTER_PUB)}")

        sub = self.ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.RCVTIMEO, 1000)
        sub.setsockopt_string(zmq.SUBSCRIBE, "")

        local_xsub_video = self.ctx.socket(zmq.SUB)
        local_xsub_video.setsockopt_string(zmq.SUBSCRIBE, "")
        local_xsub_video.setsockopt(zmq.RCVTIMEO, 100)
        local_xsub_video.connect(f"tcp://127.0.0.1:{self._p(OFF_VIDEO_XPUB)}")

        local_xsub_audio = self.ctx.socket(zmq.SUB)
        local_xsub_audio.setsockopt_string(zmq.SUBSCRIBE, "")
        local_xsub_audio.setsockopt(zmq.RCVTIMEO, 100)
        local_xsub_audio.connect(f"tcp://127.0.0.1:{self._p(OFF_AUDIO_XPUB)}")

        local_xsub_texto = self.ctx.socket(zmq.SUB)
        local_xsub_texto.setsockopt_string(zmq.SUBSCRIBE, "")
        local_xsub_texto.setsockopt(zmq.RCVTIMEO, 100)
        local_xsub_texto.connect(f"tcp://127.0.0.1:{self._p(OFF_TEXTO_XPUB)}")

        local_pub_video = self.ctx.socket(zmq.PUB)
        local_pub_video.connect(f"tcp://127.0.0.1:{self._p(OFF_VIDEO_XSUB)}")

        local_pub_audio = self.ctx.socket(zmq.PUB)
        local_pub_audio.connect(f"tcp://127.0.0.1:{self._p(OFF_AUDIO_XSUB)}")

        local_pub_texto = self.ctx.socket(zmq.PUB)
        local_pub_texto.connect(f"tcp://127.0.0.1:{self._p(OFF_TEXTO_XSUB)}")

        connected_brokers: set[str] = set()
        print(f"[broker {self.broker_id}] Inter-broker PUB porta "
              f"{self._p(OFF_INTER_PUB)}")

        time.sleep(0.5)

        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        poller.register(local_xsub_video, zmq.POLLIN)
        poller.register(local_xsub_audio, zmq.POLLIN)
        poller.register(local_xsub_texto, zmq.POLLIN)

        while not self._parar.is_set():
            # Descobrir novos brokers e conectar SUB
            self._atualizar_peers_inter_broker(sub, connected_brokers)

            try:
                events = dict(poller.poll(500))
            except zmq.ZMQError:
                break

            # Mensagens vindas de OUTROS brokers -> republicar localmente
            if sub in events:
                try:
                    frames = sub.recv_multipart(zmq.NOBLOCK)
                    if len(frames) >= 4:
                        canal = frames[0]    # b"video", b"audio", b"texto"
                        origin = frames[1]   # broker_id de origem
                        if origin.decode() != self.broker_id:
                            dados = frames[2:]
                            if canal == b"video":
                                local_pub_video.send_multipart(dados)
                            elif canal == b"audio":
                                local_pub_audio.send_multipart(dados)
                            elif canal == b"texto":
                                local_pub_texto.send_multipart(dados)
                except zmq.Again:
                    pass

            # Mensagens locais -> publicar para outros brokers
            for sock_local, canal_tag in [
                (local_xsub_video, b"video"),
                (local_xsub_audio, b"audio"),
                (local_xsub_texto, b"texto"),
            ]:
                if sock_local in events:
                    try:
                        frames = sock_local.recv_multipart(zmq.NOBLOCK)
                        pub.send_multipart([canal_tag, self.broker_id.encode()] + frames)
                    except zmq.Again:
                        pass

        for s in [pub, sub, local_xsub_video, local_xsub_audio,
                  local_xsub_texto, local_pub_video, local_pub_audio,
                  local_pub_texto]:
            s.close()

    def _atualizar_peers_inter_broker(self, sub_sock, connected: set[str]):
        """Consulta registry e conecta SUB em brokers novos."""
        try:
            req = self.ctx.socket(zmq.REQ)
            req.setsockopt(zmq.RCVTIMEO, 1000)
            req.setsockopt(zmq.LINGER, 0)
            req.connect(f"tcp://{self.registry_host}:{self.registry_port}")
            req.send_string(json.dumps({"action": "discover"}))
            resp = json.loads(req.recv_string())
            req.close()

            for b in resp.get("brokers", []):
                bid = b["broker_id"]
                if bid != self.broker_id and bid not in connected:
                    addr = f"tcp://{b['host']}:{b['port_base'] + OFF_INTER_PUB}"
                    sub_sock.connect(addr)
                    connected.add(bid)
                    print(f"[broker {self.broker_id}] Inter-broker SUB "
                          f"conectado a {bid} em {addr}")
        except (zmq.Again, zmq.ZMQError):
            pass

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self):
        threads = [
            threading.Thread(target=self._proxy_video, daemon=True),
            threading.Thread(target=self._proxy_audio, daemon=True),
            threading.Thread(target=self._proxy_texto, daemon=True),
            threading.Thread(target=self._thread_controle, daemon=True),
            threading.Thread(target=self._thread_heartbeat, daemon=True),
            threading.Thread(target=self._thread_registry, daemon=True),
            threading.Thread(target=self._thread_inter_broker, daemon=True),
        ]

        for t in threads:
            t.start()

        print(f"[broker {self.broker_id}] Todos os canais iniciados "
              f"(porta-base {self.port_base})")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n[broker {self.broker_id}] Encerrando...")
            self._parar.set()

        try:
            req = self.ctx.socket(zmq.REQ)
            req.setsockopt(zmq.RCVTIMEO, 1000)
            req.setsockopt(zmq.LINGER, 0)
            req.connect(f"tcp://{self.registry_host}:{self.registry_port}")
            req.send_string(json.dumps({
                "action": "unregister",
                "broker_id": self.broker_id,
            }))
            req.recv_string()
            req.close()
        except Exception:
            pass

        self.ctx.term()


def main():
    parser = argparse.ArgumentParser(description="Broker de videoconferência")
    parser.add_argument("--broker-id", default="B1")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port-base", type=int, default=5555)
    parser.add_argument("--registry-host", default=REGISTRY_HOST)
    parser.add_argument("--registry-port", type=int, default=REGISTRY_PORT)
    args = parser.parse_args()

    Broker(
        broker_id=args.broker_id,
        host=args.host,
        port_base=args.port_base,
        registry_host=args.registry_host,
        registry_port=args.registry_port,
    ).run()


if __name__ == "__main__":
    main()
