"""
Serviço de identidade, presença e salas.

Componentes:
  - EstadoPresenca: tabela em memória (thread-safe) com lógica pura.
  - handle_cmd: parser de comandos texto -> resposta + eventos a publicar.
  - parse_list / parse_list_sala: helpers para parsear respostas no cliente.

Protocolo texto (separado por espaço):
  Requisições (REQ -> ROUTER):
    LOGIN <id>
    LOGOUT <id>
    JOIN <id> <sala>
    LEAVE <id> <sala>
    LIST
    LIST_SALA <sala>
    HEARTBEAT <id>

  Respostas: "OK ..." ou "ERR ..."

  Eventos (PUB -> SUB):
    PRESENCE ONLINE <id>
    PRESENCE OFFLINE <id>
    SALA <sala> JOIN <id>
    SALA <sala> LEAVE <id>
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Set, Tuple


class EstadoPresenca:
    """Tabela em memória de usuários online e suas salas."""

    def __init__(self) -> None:
        self._usuarios: Dict[str, Set[str]] = {}
        self._last_seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def login(self, uid: str) -> Tuple[bool, str]:
        uid = uid.strip()
        if not uid:
            return False, "ERR id vazio"
        with self._lock:
            if uid in self._usuarios:
                return False, f"ERR ID '{uid}' ja em uso"
            self._usuarios[uid] = set()
            self._last_seen[uid] = time.time()
        return True, f"OK LOGIN {uid}"

    def logout(self, uid: str) -> Tuple[bool, str, List[str]]:
        with self._lock:
            if uid not in self._usuarios:
                return False, f"ERR ID '{uid}' nao logado", []
            salas = sorted(self._usuarios.pop(uid))
            self._last_seen.pop(uid, None)
        return True, f"OK LOGOUT {uid}", salas

    def heartbeat(self, uid: str) -> Tuple[bool, str]:
        with self._lock:
            if uid not in self._usuarios:
                return False, f"ERR ID '{uid}' nao logado"
            self._last_seen[uid] = time.time()
        return True, "OK PONG"

    def expire_stale(self, timeout: float) -> List[Tuple[str, List[str]]]:
        """Remove usuários sem heartbeat recente. Retorna [(uid, [salas])]."""
        agora = time.time()
        expirados: List[Tuple[str, List[str]]] = []
        with self._lock:
            for uid, ts in list(self._last_seen.items()):
                if agora - ts > timeout:
                    salas = sorted(self._usuarios.pop(uid, set()))
                    self._last_seen.pop(uid, None)
                    expirados.append((uid, salas))
        return expirados

    def join(self, uid: str, sala: str) -> Tuple[bool, str]:
        with self._lock:
            if uid not in self._usuarios:
                return False, f"ERR ID '{uid}' nao logado"
            if sala in self._usuarios[uid]:
                return False, f"ERR ja esta na sala '{sala}'"
            self._usuarios[uid].add(sala)
        return True, f"OK JOIN {uid} {sala}"

    def leave(self, uid: str, sala: str) -> Tuple[bool, str]:
        with self._lock:
            if uid not in self._usuarios:
                return False, f"ERR ID '{uid}' nao logado"
            if sala not in self._usuarios[uid]:
                return False, f"ERR nao esta na sala '{sala}'"
            self._usuarios[uid].discard(sala)
        return True, f"OK LEAVE {uid} {sala}"

    def list_all(self) -> Dict[str, List[str]]:
        with self._lock:
            return {uid: sorted(salas) for uid, salas in self._usuarios.items()}

    def list_sala(self, sala: str) -> List[str]:
        with self._lock:
            return sorted(
                uid for uid, salas in self._usuarios.items() if sala in salas
            )


def handle_cmd(estado: EstadoPresenca, msg: str) -> Tuple[str, List[str]]:
    """Processa uma linha de comando. Retorna (resposta, eventos_para_publicar)."""
    partes = msg.strip().split()
    if not partes:
        return "ERR comando vazio", []
    cmd = partes[0].upper()

    if cmd == "LOGIN" and len(partes) == 2:
        ok, resp = estado.login(partes[1])
        return resp, [f"PRESENCE ONLINE {partes[1]}"] if ok else []

    if cmd == "LOGOUT" and len(partes) == 2:
        ok, resp, salas = estado.logout(partes[1])
        if not ok:
            return resp, []
        eventos = [f"SALA {s} LEAVE {partes[1]}" for s in salas]
        eventos.append(f"PRESENCE OFFLINE {partes[1]}")
        return resp, eventos

    if cmd == "JOIN" and len(partes) == 3:
        ok, resp = estado.join(partes[1], partes[2])
        return resp, [f"SALA {partes[2]} JOIN {partes[1]}"] if ok else []

    if cmd == "LEAVE" and len(partes) == 3:
        ok, resp = estado.leave(partes[1], partes[2])
        return resp, [f"SALA {partes[2]} LEAVE {partes[1]}"] if ok else []

    if cmd == "LIST" and len(partes) == 1:
        d = estado.list_all()
        itens = ";".join(
            f"{uid}:{','.join(salas) if salas else '-'}" for uid, salas in d.items()
        )
        return f"OK LIST {itens}", []

    if cmd == "HEARTBEAT" and len(partes) == 2:
        _, resp = estado.heartbeat(partes[1])
        return resp, []

    if cmd == "LIST_SALA" and len(partes) == 2:
        membros = estado.list_sala(partes[1])
        return f"OK LIST_SALA {partes[1]} {','.join(membros)}", []

    return f"ERR comando invalido: {msg.strip()}", []


def parse_list(resposta: str) -> Dict[str, List[str]]:
    """Parseia resposta de LIST -> {id: [salas]}."""
    if not resposta.startswith("OK LIST"):
        return {}
    corpo = resposta[len("OK LIST"):].strip()
    if not corpo:
        return {}
    out: Dict[str, List[str]] = {}
    for item in corpo.split(";"):
        if ":" not in item:
            continue
        uid, salas = item.split(":", 1)
        out[uid] = [] if salas == "-" else salas.split(",")
    return out


def parse_list_sala(resposta: str) -> List[str]:
    """Parseia resposta de LIST_SALA -> [ids]."""
    if not resposta.startswith("OK LIST_SALA"):
        return []
    corpo = resposta[len("OK LIST_SALA"):].strip()
    partes = corpo.split(" ", 1)
    if len(partes) < 2 or not partes[1]:
        return []
    return [x for x in partes[1].split(",") if x]
