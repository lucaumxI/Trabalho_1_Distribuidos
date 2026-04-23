"""
Cliente de videoconferência com GUI Tkinter.

Threads:
  1. Captura de mídia (vídeo + áudio)    — media_capture.captura_midia
  2. Envio (PUB vídeo/áudio, PUB texto)  — _thread_envio
  3. Recepção (SUB vídeo/áudio/texto)     — _thread_recepcao
  4. Heartbeat / failover                 — _thread_heartbeat
  5. GUI / Renderização                   — thread principal (Tkinter mainloop)
"""

import json
import io
import sys
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

import zmq
from PIL import Image, ImageTk

from config import (
    REGISTRY_HOST, REGISTRY_PORT, SALAS,
    OFF_VIDEO_XSUB, OFF_VIDEO_XPUB,
    OFF_AUDIO_XSUB, OFF_AUDIO_XPUB,
    OFF_TEXTO_XSUB, OFF_TEXTO_XPUB,
    OFF_CONTROLE, OFF_HEARTBEAT,
    HEARTBEAT_TIMEOUT_S,
)
from media_capture import captura_midia
from qos import TextoReliableSender, VideoAdaptiveBuffer, audio_drop_antigos


# ======================================================================
# Descoberta de broker via registry
# ======================================================================
def descobrir_brokers(registry_host: str = REGISTRY_HOST,
                      registry_port: int = REGISTRY_PORT) -> list[dict]:
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 3000)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(f"tcp://{registry_host}:{registry_port}")
    sock.send_string(json.dumps({"action": "discover"}))
    try:
        resp = json.loads(sock.recv_string())
        return resp.get("brokers", [])
    except zmq.Again:
        return []
    finally:
        sock.close()


def selecionar_broker(brokers: list[dict], _idx=[0]) -> dict | None:
    """Round-robin simples."""
    if not brokers:
        return None
    b = brokers[_idx[0] % len(brokers)]
    _idx[0] += 1
    return b


# ======================================================================
# Classe principal do cliente
# ======================================================================
class ClienteApp:
    def __init__(self, user_id: str, sala: str):
        self.user_id = user_id
        self.sala = sala
        self.ctx = zmq.Context()
        self.parar = threading.Event()

        # broker atual
        self.broker_host: str | None = None
        self.broker_port_base: int | None = None
        self._broker_lock = threading.Lock()

        # Filas de saída (upload)
        self.fila_video_pub = queue.Queue()
        self.fila_audio_pub = queue.Queue()
        self.fila_texto_pub = queue.Queue()

        # Filas de entrada (download)
        self.fila_video_sub = queue.Queue()
        self.fila_audio_sub = queue.Queue()
        self.fila_texto_sub = queue.Queue()

        # eventos para reconexão
        self._reconectar_evt = threading.Event()

        # QoS
        self._texto_qos = TextoReliableSender()
        self._video_qos = VideoAdaptiveBuffer()
        self._seen_seqs: set[int] = set()

        # Tkinter
        self.root: tk.Tk | None = None
        self._tk_image = None  # referência para evitar GC

    # ------------------------------------------------------------------
    # Conexão com broker
    # ------------------------------------------------------------------
    def _conectar_broker(self) -> bool:
        brokers = descobrir_brokers()
        broker = selecionar_broker(brokers)
        if broker is None:
            print("[cliente] Nenhum broker disponível.")
            return False
        with self._broker_lock:
            self.broker_host = broker["host"]
            if self.broker_host == "0.0.0.0":
                self.broker_host = "127.0.0.1"
            self.broker_port_base = broker["port_base"]
        print(f"[cliente] Conectado ao broker {broker['broker_id']} "
              f"({self.broker_host}:{self.broker_port_base})")
        return True

    def _bp(self, offset: int) -> str:
        with self._broker_lock:
            return f"tcp://{self.broker_host}:{self.broker_port_base + offset}"

    def _join_sala(self):
        try:
            sock = self.ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, 3000)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(self._bp(OFF_CONTROLE))
            sock.send_string(json.dumps({
                "action": "join",
                "user_id": self.user_id,
                "sala": self.sala,
            }))
            resp = json.loads(sock.recv_string())
            sock.close()
            if resp.get("status") == "ok":
                membros = resp.get("membros", [])
                self.fila_texto_sub.put(
                    f"[sistema] Entrou na {self.sala}. "
                    f"Online: {', '.join(membros)}"
                )
            return resp.get("status") == "ok"
        except (zmq.Again, zmq.ZMQError) as e:
            print(f"[cliente] Falha no JOIN: {e}")
            return False

    def _leave_sala(self):
        try:
            sock = self.ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, 2000)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(self._bp(OFF_CONTROLE))
            sock.send_string(json.dumps({
                "action": "leave",
                "user_id": self.user_id,
                "sala": self.sala,
            }))
            sock.recv_string()
            sock.close()
        except (zmq.Again, zmq.ZMQError):
            pass

    # ------------------------------------------------------------------
    # Thread de envio
    # ------------------------------------------------------------------
    def _thread_envio(self):
        while not self.parar.is_set():
            if self._reconectar_evt.is_set():
                time.sleep(0.2)
                continue

            try:
                video_pub = self.ctx.socket(zmq.PUB)
                video_pub.connect(self._bp(OFF_VIDEO_XSUB))

                audio_pub = self.ctx.socket(zmq.PUB)
                audio_pub.connect(self._bp(OFF_AUDIO_XSUB))

                texto_pub = self.ctx.socket(zmq.PUB)
                texto_pub.connect(self._bp(OFF_TEXTO_XSUB))

                time.sleep(0.5)
                print("[cliente] Thread de envio pronta.")

                while not self.parar.is_set() and not self._reconectar_evt.is_set():
                    # Vídeo
                    try:
                        frame = self.fila_video_pub.get(timeout=0.005)
                        self._video_qos.drop_antigos(self.fila_video_pub)
                        video_pub.send_multipart([
                            self.sala.encode(),
                            self.user_id.encode(),
                            frame,
                        ])
                    except queue.Empty:
                        pass

                    # Áudio
                    try:
                        audio = self.fila_audio_pub.get(timeout=0.005)
                        audio_drop_antigos(self.fila_audio_pub)
                        audio_pub.send_multipart([
                            self.sala.encode(),
                            self.user_id.encode(),
                            audio,
                        ])
                    except queue.Empty:
                        pass

                    # Texto
                    try:
                        texto = self.fila_texto_pub.get(timeout=0.005)
                        seq = self._texto_qos.next_seq()
                        payload = json.dumps({
                            "seq": seq, "user": self.user_id, "msg": texto,
                        })
                        self._texto_qos.registrar(seq, payload)
                        texto_pub.send_multipart([
                            self.sala.encode(),
                            self.user_id.encode(),
                            payload.encode(),
                        ])
                        self._texto_qos.confirmar(seq)
                    except queue.Empty:
                        pass

                    # Reenvio apenas de mensagens que falharam no send
                    for seq, payload in self._texto_qos.pendentes_para_reenvio():
                        try:
                            texto_pub.send_multipart([
                                self.sala.encode(),
                                self.user_id.encode(),
                                payload.encode(),
                            ], zmq.NOBLOCK)
                            self._texto_qos.confirmar(seq)
                        except zmq.Again:
                            pass

            except zmq.ZMQError as e:
                print(f"[cliente] Erro no envio: {e}")
            finally:
                for s in [video_pub, audio_pub, texto_pub]:
                    try:
                        s.close()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # Thread de recepção
    # ------------------------------------------------------------------
    def _thread_recepcao(self):
        while not self.parar.is_set():
            if self._reconectar_evt.is_set():
                time.sleep(0.2)
                continue

            video_sub = None
            audio_sub = None
            texto_sub = None
            try:
                video_sub = self.ctx.socket(zmq.SUB)
                video_sub.setsockopt(zmq.SUBSCRIBE, self.sala.encode())
                video_sub.setsockopt(zmq.RCVHWM, 5)
                video_sub.connect(self._bp(OFF_VIDEO_XPUB))

                audio_sub = self.ctx.socket(zmq.SUB)
                audio_sub.setsockopt(zmq.SUBSCRIBE, self.sala.encode())
                audio_sub.connect(self._bp(OFF_AUDIO_XPUB))

                texto_sub = self.ctx.socket(zmq.SUB)
                texto_sub.setsockopt(zmq.SUBSCRIBE, self.sala.encode())
                texto_sub.connect(self._bp(OFF_TEXTO_XPUB))

                poller = zmq.Poller()
                poller.register(video_sub, zmq.POLLIN)
                poller.register(audio_sub, zmq.POLLIN)
                poller.register(texto_sub, zmq.POLLIN)

                print("[cliente] Thread de recepção pronta.")

                while not self.parar.is_set() and not self._reconectar_evt.is_set():
                    events = dict(poller.poll(100))

                    if video_sub in events:
                        parts = video_sub.recv_multipart(zmq.NOBLOCK)
                        if len(parts) >= 3:
                            remetente = parts[1].decode()
                            if remetente != self.user_id:
                                self.fila_video_sub.put(parts[2])

                    if audio_sub in events:
                        parts = audio_sub.recv_multipart(zmq.NOBLOCK)
                        if len(parts) >= 3:
                            remetente = parts[1].decode()
                            if remetente != self.user_id:
                                self.fila_audio_sub.put(parts[2])

                    if texto_sub in events:
                        parts = texto_sub.recv_multipart(zmq.NOBLOCK)
                        if len(parts) >= 3:
                            remetente = parts[1].decode()
                            raw = parts[2].decode()
                            try:
                                data = json.loads(raw)
                                if data.get("tipo") == "presenca":
                                    membros = data.get("membros", [])
                                    self.fila_texto_sub.put(
                                        f"__PRESENCA__:{json.dumps(membros)}"
                                    )
                                else:
                                    user = data.get("user", remetente)
                                    msg = data.get("msg", raw)
                                    seq = data.get("seq")
                                    if user == self.user_id:
                                        continue
                                    if seq is not None and seq in self._seen_seqs:
                                        continue
                                    if seq is not None:
                                        self._seen_seqs.add(seq)
                                        if len(self._seen_seqs) > 5000:
                                            self._seen_seqs.clear()
                                    self.fila_texto_sub.put(f"{user}: {msg}")
                            except json.JSONDecodeError:
                                if remetente != self.user_id:
                                    self.fila_texto_sub.put(f"{remetente}: {raw}")

            except zmq.ZMQError as e:
                print(f"[cliente] Erro na recepção: {e}")
            finally:
                for s in [video_sub, audio_sub, texto_sub]:
                    if s is not None:
                        try:
                            s.close()
                        except Exception:
                            pass

    # ------------------------------------------------------------------
    # Thread de heartbeat / failover
    # ------------------------------------------------------------------
    def _thread_heartbeat(self):
        while not self.parar.is_set():
            hb_sub = None
            try:
                hb_sub = self.ctx.socket(zmq.SUB)
                hb_sub.setsockopt_string(zmq.SUBSCRIBE, "HB")
                hb_sub.setsockopt(zmq.RCVTIMEO, int(HEARTBEAT_TIMEOUT_S * 1000))
                hb_sub.connect(self._bp(OFF_HEARTBEAT))

                while not self.parar.is_set() and not self._reconectar_evt.is_set():
                    try:
                        hb_sub.recv_string()
                    except zmq.Again:
                        print("[cliente] Heartbeat perdido — iniciando failover...")
                        self._reconectar_evt.set()
                        hb_sub.close()
                        hb_sub = None
                        self._fazer_failover()
                        break
            except zmq.ZMQError:
                pass
            finally:
                if hb_sub is not None:
                    try:
                        hb_sub.close()
                    except Exception:
                        pass

            if self._reconectar_evt.is_set():
                time.sleep(0.5)

    def _fazer_failover(self):
        for tentativa in range(5):
            print(f"[cliente] Failover tentativa {tentativa + 1}...")
            if self._conectar_broker():
                self._join_sala()
                self._reconectar_evt.clear()
                self.fila_texto_sub.put(
                    "[sistema] Reconectado a novo broker com sucesso."
                )
                print("[cliente] Failover bem-sucedido.")
                return
            time.sleep(1)
        self.fila_texto_sub.put("[sistema] Failover falhou. Sem brokers disponíveis.")
        self._reconectar_evt.clear()

    # ------------------------------------------------------------------
    # Trocar de sala
    # ------------------------------------------------------------------
    def trocar_sala(self, nova_sala: str):
        self._leave_sala()
        self.sala = nova_sala
        self._reconectar_evt.set()
        time.sleep(0.3)
        self._join_sala()

        # resubscrever nos canais com novo tópico
        self._reconectar_evt.clear()
        self.fila_texto_sub.put(f"[sistema] Sala alterada para {nova_sala}.")

    # ------------------------------------------------------------------
    # GUI Tkinter
    # ------------------------------------------------------------------
    def _build_gui(self):
        self.root = tk.Tk()
        self.root.title(f"VideoConf — {self.user_id}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.minsize(800, 600)

        # Frame principal
        main_frame = ttk.Frame(self.root, padding=5)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Coluna esquerda — vídeo
        left = ttk.Frame(main_frame)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.video_label = ttk.Label(left, text="Aguardando vídeo...")
        self.video_label.pack(fill=tk.BOTH, expand=True)

        # Coluna direita — chat + controles
        right = ttk.Frame(main_frame, width=300)
        right.pack(side=tk.RIGHT, fill=tk.Y)

        # Seletor de sala
        sala_frame = ttk.LabelFrame(right, text="Sala", padding=5)
        sala_frame.pack(fill=tk.X, pady=(0, 5))

        self._sala_var = tk.StringVar(value=self.sala)
        sala_combo = ttk.Combobox(
            sala_frame, textvariable=self._sala_var,
            values=SALAS, state="readonly", width=15,
        )
        sala_combo.pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(
            sala_frame, text="Entrar", command=self._on_trocar_sala,
        ).pack(side=tk.LEFT)

        # Lista de usuários online
        presenca_frame = ttk.LabelFrame(right, text="Online", padding=5)
        presenca_frame.pack(fill=tk.X, pady=(0, 5))

        self._lista_usuarios = tk.Listbox(presenca_frame, height=6)
        self._lista_usuarios.pack(fill=tk.X)

        # Chat
        chat_frame = ttk.LabelFrame(right, text="Chat", padding=5)
        chat_frame.pack(fill=tk.BOTH, expand=True)

        self._chat_area = scrolledtext.ScrolledText(
            chat_frame, state=tk.DISABLED, wrap=tk.WORD, height=15, width=35,
        )
        self._chat_area.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        entry_frame = ttk.Frame(chat_frame)
        entry_frame.pack(fill=tk.X)

        self._msg_entry = ttk.Entry(entry_frame)
        self._msg_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self._msg_entry.bind("<Return>", lambda e: self._on_enviar_texto())

        ttk.Button(
            entry_frame, text="Enviar", command=self._on_enviar_texto,
        ).pack(side=tk.RIGHT)

        # Status bar
        self._status_var = tk.StringVar(value=f"Conectado à {self.sala}")
        ttk.Label(
            self.root, textvariable=self._status_var, relief=tk.SUNKEN,
            anchor=tk.W, padding=3,
        ).pack(fill=tk.X, side=tk.BOTTOM)

    def _on_trocar_sala(self):
        nova = self._sala_var.get()
        if nova and nova != self.sala:
            threading.Thread(
                target=self.trocar_sala, args=(nova,), daemon=True
            ).start()

    def _on_enviar_texto(self):
        msg = self._msg_entry.get().strip()
        if msg:
            self._msg_entry.delete(0, tk.END)
            self.fila_texto_pub.put(msg)
            self._append_chat(f"{self.user_id}: {msg}")

    def _append_chat(self, texto: str):
        self._chat_area.configure(state=tk.NORMAL)
        self._chat_area.insert(tk.END, texto + "\n")
        self._chat_area.see(tk.END)
        self._chat_area.configure(state=tk.DISABLED)

    def _poll_filas(self):
        """Chamada periodicamente pelo Tkinter para consumir filas de recepção."""
        # Vídeo — mostra último frame
        frame_data = None
        try:
            while True:
                frame_data = self.fila_video_sub.get_nowait()
        except queue.Empty:
            pass

        if frame_data is not None:
            try:
                img = Image.open(io.BytesIO(frame_data))
                img = img.resize((480, 360), Image.LANCZOS)
                self._tk_image = ImageTk.PhotoImage(img)
                self.video_label.configure(image=self._tk_image, text="")
            except Exception:
                pass

        # Texto / presença
        for _ in range(50):
            try:
                msg = self.fila_texto_sub.get_nowait()
                if msg.startswith("__PRESENCA__:"):
                    membros = json.loads(msg[len("__PRESENCA__:"):])
                    self._lista_usuarios.delete(0, tk.END)
                    for m in membros:
                        self._lista_usuarios.insert(tk.END, m)
                else:
                    self._append_chat(msg)
            except queue.Empty:
                break

        # Áudio — placeholder: apenas descarta (reprodução requer pyaudio output)
        try:
            while True:
                self.fila_audio_sub.get_nowait()
        except queue.Empty:
            pass

        if not self.parar.is_set():
            self.root.after(33, self._poll_filas)

    def _on_close(self):
        self.parar.set()
        self._leave_sala()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self):
        if not self._conectar_broker():
            print("[cliente] Não foi possível encontrar um broker. Encerrando.")
            return

        self._join_sala()

        # Threads de fundo
        threads = [
            threading.Thread(
                target=captura_midia,
                args=(self.fila_video_pub, self.fila_audio_pub, self.parar),
                daemon=True,
            ),
            threading.Thread(target=self._thread_envio, daemon=True),
            threading.Thread(target=self._thread_recepcao, daemon=True),
            threading.Thread(target=self._thread_heartbeat, daemon=True),
        ]
        for t in threads:
            t.start()

        # GUI na thread principal
        self._build_gui()
        self.root.after(100, self._poll_filas)
        self.root.mainloop()

        self.parar.set()
        self.ctx.term()


# ======================================================================
# main
# ======================================================================
def main():
    if len(sys.argv) >= 2:
        user_id = sys.argv[1]
    else:
        user_id = input("Digite seu ID: ").strip()
        if not user_id:
            print("ID inválido.")
            return

    sala = "SALA_A"
    if len(sys.argv) >= 3:
        sala = sys.argv[2]

    app = ClienteApp(user_id, sala)
    app.run()


if __name__ == "__main__":
    main()
