"""
Cliente de videoconferência com GUI Tkinter.

Threads:
  1. Captura de mídia (vídeo + áudio)
  2. Envio (PUB vídeo/áudio/texto)
  3. Recepção (SUB vídeo/áudio/texto)
  4. Heartbeat / failover (monitora broker)
  5. Heartbeat de presença (cliente -> broker)
  6. Presença SUB (eventos ONLINE/OFFLINE/SALA)
  7. Playback de áudio (PyAudio output)
  8. GUI / Renderização (thread principal - Tkinter)
"""

import json
import io
import sys
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext

import cv2
import numpy as np
import zmq
from PIL import Image, ImageTk

from config import (
    REGISTRY_HOST, REGISTRY_PORT, SALAS,
    OFF_VIDEO_XSUB, OFF_VIDEO_XPUB,
    OFF_AUDIO_XSUB, OFF_AUDIO_XPUB,
    OFF_TEXTO_XSUB, OFF_TEXTO_XPUB,
    OFF_CONTROLE, OFF_HEARTBEAT, OFF_PRESENCE_PUB,
    HEARTBEAT_TIMEOUT_S, CLIENT_HB_INTERVAL,
    AUDIO_RATE, AUDIO_CHANNELS, AUDIO_FORMAT, AUDIO_CHUNK,
    AUDIO_MAX_QUEUE, VIDEO_JPEG_QUALITY,
)
from media_capture import captura_midia, get_pa, terminate_pa, _PYAUDIO_OK
from qos import TextoReliableSender, VideoAdaptiveBuffer, audio_drop_antigos


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


class ClienteApp:
    def __init__(self, user_id: str, sala: str):
        self.user_id = user_id
        self.sala = sala
        self.ctx = zmq.Context()
        self.parar = threading.Event()

        self.broker_host: str | None = None
        self.broker_port_base: int | None = None
        self._broker_lock = threading.Lock()

        # Filas de saída
        self.fila_video_pub = queue.Queue()
        self.fila_audio_pub = queue.Queue()
        self.fila_texto_pub = queue.Queue()

        # Filas de entrada
        self.fila_video_sub = queue.Queue()
        self.fila_audio_sub = queue.Queue()
        self.fila_texto_sub = queue.Queue()

        self._reconectar_evt = threading.Event()

        # QoS
        self._texto_qos = TextoReliableSender()
        self._video_qos = VideoAdaptiveBuffer()
        self._seen_seqs: set[int] = set()

        # Presença
        self._online_users: set[str] = set()
        self._sala_membros: dict[str, set[str]] = {}
        self._presenca_lock = threading.Lock()

        # Socket REQ para controle (com lock para thread-safety)
        self._ctrl_sock: zmq.Socket | None = None
        self._ctrl_lock = threading.Lock()

        # Tkinter
        self.root: tk.Tk | None = None
        self._tk_image = None

    # --- Conexão com broker ---
    def _conectar_broker(self) -> bool:
        brokers = descobrir_brokers()
        broker = selecionar_broker(brokers)
        if broker is None:
            print("[cliente] Nenhum broker disponível.")
            return False
        with self._broker_lock:
            self.broker_host = broker["host"]
            self.broker_port_base = broker["port_base"]
        print(f"[cliente] Conectado ao broker {broker['broker_id']} "
              f"({self.broker_host}:{self.broker_port_base})")
        return True

    def _bp(self, offset: int) -> str:
        with self._broker_lock:
            return f"tcp://{self.broker_host}:{self.broker_port_base + offset}"

    def _novo_ctrl_sock(self):
        """Cria/recria o socket REQ de controle."""
        with self._ctrl_lock:
            if self._ctrl_sock is not None:
                self._ctrl_sock.close(linger=0)
            self._ctrl_sock = self.ctx.socket(zmq.REQ)
            self._ctrl_sock.setsockopt(zmq.RCVTIMEO, 3000)
            self._ctrl_sock.setsockopt(zmq.SNDTIMEO, 3000)
            self._ctrl_sock.setsockopt(zmq.LINGER, 0)
            self._ctrl_sock.connect(self._bp(OFF_CONTROLE))

    def _ctrl_cmd(self, msg: str) -> str:
        """Envia comando texto ao broker e retorna resposta. Thread-safe."""
        with self._ctrl_lock:
            try:
                self._ctrl_sock.send_string(msg)
                return self._ctrl_sock.recv_string()
            except zmq.Again:
                # REQ fica travado após timeout; precisa recriar
                self._ctrl_sock.close(linger=0)
                self._ctrl_sock = self.ctx.socket(zmq.REQ)
                self._ctrl_sock.setsockopt(zmq.RCVTIMEO, 3000)
                self._ctrl_sock.setsockopt(zmq.SNDTIMEO, 3000)
                self._ctrl_sock.setsockopt(zmq.LINGER, 0)
                self._ctrl_sock.connect(self._bp(OFF_CONTROLE))
                return "ERR timeout"
            except zmq.ZMQError as e:
                return f"ERR zmq: {e}"

    def _login(self) -> bool:
        resp = self._ctrl_cmd(f"LOGIN {self.user_id}")
        if resp.startswith("OK"):
            return True
        print(f"[cliente] Login falhou: {resp}")
        return False

    def _logout(self):
        try:
            self._ctrl_cmd(f"LOGOUT {self.user_id}")
        except Exception:
            pass

    def _join_sala(self) -> bool:
        resp = self._ctrl_cmd(f"JOIN {self.user_id} {self.sala}")
        if resp.startswith("OK"):
            self.fila_texto_sub.put(f"[sistema] Entrou na {self.sala}.")
            return True
        print(f"[cliente] Falha no JOIN: {resp}")
        return False

    def _leave_sala(self):
        try:
            self._ctrl_cmd(f"LEAVE {self.user_id} {self.sala}")
        except Exception:
            pass

    # --- Thread de envio ---
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
                    # Vídeo — com taxa adaptativa
                    try:
                        frame = self.fila_video_pub.get(timeout=0.005)
                        self._video_qos.ajustar(self.fila_video_pub)
                        self._video_qos.drop_antigos(self.fila_video_pub)

                        # Re-encode com qualidade adaptativa se necessário
                        if self._video_qos.quality < VIDEO_JPEG_QUALITY:
                            arr = np.frombuffer(frame, dtype=np.uint8)
                            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            if img is not None:
                                ok, buf = cv2.imencode(
                                    ".jpg", img,
                                    [int(cv2.IMWRITE_JPEG_QUALITY), self._video_qos.quality]
                                )
                                if ok:
                                    frame = buf.tobytes()

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

                    # Texto — com retry em caso de falha de envio
                    try:
                        texto = self.fila_texto_pub.get(timeout=0.005)
                        seq = self._texto_qos.next_seq()
                        payload = json.dumps({
                            "seq": seq, "user": self.user_id, "msg": texto,
                        })
                        self._texto_qos.registrar(seq, payload)
                        try:
                            texto_pub.send_multipart([
                                self.sala.encode(),
                                self.user_id.encode(),
                                payload.encode(),
                            ], zmq.NOBLOCK)
                            # Só confirma se o send não deu erro
                            self._texto_qos.confirmar(seq)
                        except zmq.Again:
                            pass  # fica pendente para reenvio
                    except queue.Empty:
                        pass

                    # Reenvio de mensagens que falharam
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

    # --- Thread de recepção ---
    def _thread_recepcao(self):
        while not self.parar.is_set():
            if self._reconectar_evt.is_set():
                time.sleep(0.2)
                continue

            video_sub = audio_sub = texto_sub = None
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
                                user = data.get("user", remetente)
                                msg = data.get("msg", raw)
                                seq = data.get("seq")
                                if user == self.user_id:
                                    continue
                                # Dedup por sequência
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

    # --- Thread de heartbeat broker->cliente (failover) ---
    def _thread_heartbeat(self):
        while not self.parar.is_set():
            hb_sub = None
            try:
                hb_sub = self.ctx.socket(zmq.SUB)
                hb_sub.setsockopt_string(zmq.SUBSCRIBE, "HB")
                hb_sub.setsockopt(zmq.RCVTIMEO, int(HEARTBEAT_TIMEOUT_S * 1000))
                hb_sub.connect(self._bp(OFF_HEARTBEAT))

                # Warmup: SUB leva tempo pra começar a receber (slow joiner).
                # Espera até 2x o timeout antes de considerar falha real.
                misses = 0
                max_misses = 3

                while not self.parar.is_set() and not self._reconectar_evt.is_set():
                    try:
                        hb_sub.recv_string()
                        misses = 0  # reset ao receber
                    except zmq.Again:
                        misses += 1
                        if misses >= max_misses:
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
                self._novo_ctrl_sock()
                self._login()
                self._join_sala()
                self._reconectar_evt.clear()
                self.fila_texto_sub.put("[sistema] Reconectado a novo broker.")
                print("[cliente] Failover OK.")
                return
            time.sleep(1)
        self.fila_texto_sub.put("[sistema] Failover falhou.")
        self._reconectar_evt.clear()

    # --- Thread de heartbeat cliente->broker (presença) ---
    def _thread_client_heartbeat(self):
        # Socket REQ próprio desta thread (ZMQ sockets não são thread-safe)
        hb_req = None

        while not self.parar.is_set():
            if self._reconectar_evt.is_set():
                if hb_req is not None:
                    hb_req.close(linger=0)
                    hb_req = None
                time.sleep(0.5)
                continue

            if hb_req is None:
                hb_req = self.ctx.socket(zmq.REQ)
                hb_req.setsockopt(zmq.RCVTIMEO, 2000)
                hb_req.setsockopt(zmq.SNDTIMEO, 2000)
                hb_req.setsockopt(zmq.LINGER, 0)
                hb_req.connect(self._bp(OFF_CONTROLE))

            try:
                hb_req.send_string(f"HEARTBEAT {self.user_id}")
                hb_req.recv_string()
            except zmq.Again:
                # Timeout — recriar socket (REQ trava em estado inválido)
                hb_req.close(linger=0)
                hb_req = None
            except zmq.ZMQError:
                hb_req.close(linger=0)
                hb_req = None

            t = 0.0
            while t < CLIENT_HB_INTERVAL and not self.parar.is_set():
                time.sleep(0.1)
                t += 0.1

        if hb_req is not None:
            hb_req.close(linger=0)

    # --- Thread de presença SUB (eventos ONLINE/OFFLINE/SALA) ---
    def _thread_presenca_sub(self):
        while not self.parar.is_set():
            if self._reconectar_evt.is_set():
                time.sleep(0.2)
                continue

            sub = None
            try:
                sub = self.ctx.socket(zmq.SUB)
                sub.setsockopt(zmq.LINGER, 0)
                sub.setsockopt_string(zmq.SUBSCRIBE, "")
                sub.connect(self._bp(OFF_PRESENCE_PUB))

                poller = zmq.Poller()
                poller.register(sub, zmq.POLLIN)

                while not self.parar.is_set() and not self._reconectar_evt.is_set():
                    socks = dict(poller.poll(200))
                    if sub in socks:
                        try:
                            msg = sub.recv_string(zmq.NOBLOCK)
                            self._aplicar_evento_presenca(msg)
                        except zmq.Again:
                            continue
            except zmq.ZMQError:
                pass
            finally:
                if sub is not None:
                    try:
                        sub.close(linger=0)
                    except Exception:
                        pass

    def _aplicar_evento_presenca(self, msg: str):
        partes = msg.split()
        with self._presenca_lock:
            if len(partes) >= 3 and partes[0] == "PRESENCE":
                if partes[1] == "ONLINE":
                    self._online_users.add(partes[2])
                elif partes[1] == "OFFLINE":
                    self._online_users.discard(partes[2])
                    for membros in self._sala_membros.values():
                        membros.discard(partes[2])
                # Atualiza GUI via fila
                self.fila_texto_sub.put("__PRESENCA_UPDATE__")

            elif len(partes) >= 4 and partes[0] == "SALA":
                sala, acao, uid = partes[1], partes[2], partes[3]
                membros = self._sala_membros.setdefault(sala, set())
                if acao == "JOIN":
                    membros.add(uid)
                    self._online_users.add(uid)
                elif acao == "LEAVE":
                    membros.discard(uid)
                self.fila_texto_sub.put("__PRESENCA_UPDATE__")

    # --- Thread de playback de áudio ---
    def _thread_audio_playback(self):
        pa = get_pa()
        if pa is None:
            # Drena a fila pra não acumular memória
            while not self.parar.is_set():
                try:
                    self.fila_audio_sub.get(timeout=0.2)
                except queue.Empty:
                    pass
            return

        try:
            stream = pa.open(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                output=True,
                frames_per_buffer=AUDIO_CHUNK,
            )
        except Exception as e:
            print(f"[audio] Saída de áudio indisponível: {e}")
            return

        try:
            while not self.parar.is_set():
                try:
                    dados = self.fila_audio_sub.get(timeout=0.05)
                except queue.Empty:
                    continue

                # Drop de chunks antigos pra manter baixa latência
                while self.fila_audio_sub.qsize() > AUDIO_MAX_QUEUE:
                    try:
                        dados = self.fila_audio_sub.get_nowait()
                    except queue.Empty:
                        break

                try:
                    stream.write(dados)
                except Exception as e:
                    print(f"[audio] Erro no playback: {e}")
                    break
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass

    # --- Trocar de sala ---
    def trocar_sala(self, nova_sala: str):
        self._leave_sala()
        self.sala = nova_sala
        self._reconectar_evt.set()
        time.sleep(0.3)
        self._join_sala()
        self._reconectar_evt.clear()
        self.fila_texto_sub.put(f"[sistema] Sala alterada para {nova_sala}.")

    # --- GUI Tkinter ---
    def _build_gui(self):
        self.root = tk.Tk()
        self.root.title(f"VideoConf — {self.user_id}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.minsize(800, 600)

        main_frame = ttk.Frame(self.root, padding=5)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Vídeo à esquerda
        left = ttk.Frame(main_frame)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.video_label = ttk.Label(left, text="Aguardando vídeo...")
        self.video_label.pack(fill=tk.BOTH, expand=True)

        # Chat + controles à direita
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

        # Usuários online
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

        # Barra de status
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
        """Chamada periodicamente pelo Tkinter para consumir filas."""
        # Vídeo — mostra último frame recebido
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

        # Texto e eventos de presença
        for _ in range(50):
            try:
                msg = self.fila_texto_sub.get_nowait()
                if msg == "__PRESENCA_UPDATE__":
                    self._atualizar_lista_usuarios()
                else:
                    self._append_chat(msg)
            except queue.Empty:
                break

        if not self.parar.is_set():
            self.root.after(33, self._poll_filas)

    def _atualizar_lista_usuarios(self):
        """Atualiza a listbox de usuários online com os membros da sala atual."""
        with self._presenca_lock:
            membros = sorted(self._sala_membros.get(self.sala, set()))
        self._lista_usuarios.delete(0, tk.END)
        for m in membros:
            self._lista_usuarios.insert(tk.END, m)

    def _on_close(self):
        self.parar.set()
        self._leave_sala()
        self._logout()
        self.root.destroy()

    # --- Run ---
    def run(self):
        if not self._conectar_broker():
            print("[cliente] Não foi possível encontrar um broker.")
            return

        self._novo_ctrl_sock()

        if not self._login():
            print("[cliente] Login falhou. Verifique se o ID já está em uso.")
            return

        self._join_sala()

        threads = [
            threading.Thread(
                target=captura_midia,
                args=(self.fila_video_pub, self.fila_audio_pub, self.parar),
                daemon=True,
            ),
            threading.Thread(target=self._thread_envio, daemon=True),
            threading.Thread(target=self._thread_recepcao, daemon=True),
            threading.Thread(target=self._thread_heartbeat, daemon=True),
            threading.Thread(target=self._thread_client_heartbeat, daemon=True),
            threading.Thread(target=self._thread_presenca_sub, daemon=True),
            threading.Thread(target=self._thread_audio_playback, daemon=True),
        ]
        for t in threads:
            t.start()

        self._build_gui()
        self.root.after(100, self._poll_filas)
        self.root.mainloop()

        self.parar.set()
        terminate_pa()
        with self._ctrl_lock:
            if self._ctrl_sock is not None:
                self._ctrl_sock.close(linger=0)
        self.ctx.term()


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
