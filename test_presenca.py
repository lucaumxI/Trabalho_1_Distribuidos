"""
Testes de identidade, presença e salas.

Rodar:
    python -m unittest test_presenca.py -v
"""
import socket
import threading
import time
import unittest

import zmq

from presenca import EstadoPresenca, handle_cmd, parse_list, parse_list_sala


def _porta_livre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# =========================================================================
# Unit: EstadoPresenca (lógica pura, sem ZMQ)
# =========================================================================
class TestEstadoPresenca(unittest.TestCase):
    def test_login_id_unico(self):
        e = EstadoPresenca()
        ok, _ = e.login("alice")
        self.assertTrue(ok)
        ok, msg = e.login("alice")
        self.assertFalse(ok)
        self.assertIn("ja em uso", msg)

    def test_login_id_vazio(self):
        e = EstadoPresenca()
        ok, _ = e.login("")
        self.assertFalse(ok)

    def test_logout_remove_e_retorna_salas(self):
        e = EstadoPresenca()
        e.login("bob")
        e.join("bob", "A")
        e.join("bob", "B")
        ok, _, salas = e.logout("bob")
        self.assertTrue(ok)
        self.assertEqual(sorted(salas), ["A", "B"])
        self.assertEqual(e.list_all(), {})

    def test_logout_inexistente(self):
        ok, *_ = EstadoPresenca().logout("fantasma")
        self.assertFalse(ok)

    def test_join_requer_login(self):
        ok, _ = EstadoPresenca().join("ghost", "A")
        self.assertFalse(ok)

    def test_join_duplicado(self):
        e = EstadoPresenca()
        e.login("a")
        e.join("a", "A")
        ok, _ = e.join("a", "A")
        self.assertFalse(ok)

    def test_leave_nao_esta_na_sala(self):
        e = EstadoPresenca()
        e.login("a")
        ok, _ = e.leave("a", "Z")
        self.assertFalse(ok)

    def test_list_all_e_list_sala(self):
        e = EstadoPresenca()
        for uid in ("alice", "bob", "carol"):
            e.login(uid)
        e.join("alice", "A")
        e.join("alice", "B")
        e.join("bob", "A")
        self.assertEqual(e.list_sala("A"), ["alice", "bob"])
        self.assertEqual(e.list_sala("B"), ["alice"])
        self.assertEqual(e.list_sala("C"), [])
        todos = e.list_all()
        self.assertEqual(set(todos.keys()), {"alice", "bob", "carol"})
        self.assertEqual(todos["carol"], [])

    def test_heartbeat_atualiza_timestamp(self):
        e = EstadoPresenca()
        e.login("a")
        ok, resp = e.heartbeat("a")
        self.assertTrue(ok)
        self.assertEqual(resp, "OK PONG")

    def test_heartbeat_nao_logado(self):
        ok, _ = EstadoPresenca().heartbeat("ninguem")
        self.assertFalse(ok)

    def test_expire_stale(self):
        e = EstadoPresenca()
        e.login("a")
        e.join("a", "X")
        # Forçar timestamp antigo
        e._last_seen["a"] = time.time() - 100
        expirados = e.expire_stale(5.0)
        self.assertEqual(len(expirados), 1)
        self.assertEqual(expirados[0][0], "a")
        self.assertIn("X", expirados[0][1])
        self.assertEqual(e.list_all(), {})


# =========================================================================
# Unit: handle_cmd (parser + eventos)
# =========================================================================
class TestHandleCmd(unittest.TestCase):
    def test_login_gera_evento_online(self):
        e = EstadoPresenca()
        resp, ev = handle_cmd(e, "LOGIN alice")
        self.assertTrue(resp.startswith("OK"))
        self.assertEqual(ev, ["PRESENCE ONLINE alice"])

    def test_login_falhou_nao_gera_evento(self):
        e = EstadoPresenca()
        handle_cmd(e, "LOGIN alice")
        resp, ev = handle_cmd(e, "LOGIN alice")
        self.assertTrue(resp.startswith("ERR"))
        self.assertEqual(ev, [])

    def test_logout_gera_leave_e_offline(self):
        e = EstadoPresenca()
        handle_cmd(e, "LOGIN alice")
        handle_cmd(e, "JOIN alice A")
        handle_cmd(e, "JOIN alice B")
        _, ev = handle_cmd(e, "LOGOUT alice")
        self.assertIn("SALA A LEAVE alice", ev)
        self.assertIn("SALA B LEAVE alice", ev)
        self.assertEqual(ev[-1], "PRESENCE OFFLINE alice")

    def test_join_leave_eventos(self):
        e = EstadoPresenca()
        handle_cmd(e, "LOGIN a")
        _, ev = handle_cmd(e, "JOIN a A")
        self.assertEqual(ev, ["SALA A JOIN a"])
        _, ev = handle_cmd(e, "LEAVE a A")
        self.assertEqual(ev, ["SALA A LEAVE a"])

    def test_list_parseavel(self):
        e = EstadoPresenca()
        handle_cmd(e, "LOGIN alice")
        handle_cmd(e, "JOIN alice A")
        handle_cmd(e, "LOGIN bob")
        resp, _ = handle_cmd(e, "LIST")
        parsed = parse_list(resp)
        self.assertEqual(parsed, {"alice": ["A"], "bob": []})

    def test_list_sala_parseavel(self):
        e = EstadoPresenca()
        handle_cmd(e, "LOGIN a")
        handle_cmd(e, "JOIN a A")
        handle_cmd(e, "LOGIN b")
        handle_cmd(e, "JOIN b A")
        resp, _ = handle_cmd(e, "LIST_SALA A")
        self.assertEqual(sorted(parse_list_sala(resp)), ["a", "b"])

    def test_comando_invalido(self):
        resp, ev = handle_cmd(EstadoPresenca(), "FOO bar")
        self.assertTrue(resp.startswith("ERR"))
        self.assertEqual(ev, [])

    def test_comando_vazio(self):
        resp, _ = handle_cmd(EstadoPresenca(), "   ")
        self.assertTrue(resp.startswith("ERR"))


# =========================================================================
# Integração: ROUTER/PUB do broker <-> REQ cliente via TCP
# =========================================================================
class TestIntegracaoBrokerPresenca(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = zmq.Context()
        cls.ctrl_port = _porta_livre()
        cls.pres_port = _porta_livre()
        cls.parar = threading.Event()
        cls.estado = EstadoPresenca()

        # Simula o _thread_controle do broker
        cls.thread = threading.Thread(
            target=cls._run_controle,
            daemon=True,
        )
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def _run_controle(cls):
        router = cls.ctx.socket(zmq.ROUTER)
        router.setsockopt(zmq.LINGER, 0)
        router.bind(f"tcp://*:{cls.ctrl_port}")

        pub = cls.ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)
        pub.bind(f"tcp://*:{cls.pres_port}")

        poller = zmq.Poller()
        poller.register(router, zmq.POLLIN)

        while not cls.parar.is_set():
            socks = dict(poller.poll(200))
            if router in socks:
                frames = router.recv_multipart()
                if len(frames) < 3:
                    continue
                identity = frames[0]
                payload = frames[2].decode("utf-8", errors="replace")
                resposta, eventos = handle_cmd(cls.estado, payload)
                router.send_multipart([identity, b"", resposta.encode("utf-8")])
                for ev in eventos:
                    pub.send_string(ev)

        router.close(linger=0)
        pub.close(linger=0)

    @classmethod
    def tearDownClass(cls):
        cls.parar.set()
        cls.thread.join(timeout=2)
        cls.ctx.term()

    def _req(self) -> zmq.Socket:
        s = self.ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, 1500)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(f"tcp://127.0.0.1:{self.ctrl_port}")
        return s

    def _cmd(self, sock, msg):
        sock.send_string(msg)
        return sock.recv_string()

    def test_login_unico(self):
        s1 = self._req()
        s2 = self._req()
        try:
            r1 = self._cmd(s1, "LOGIN test_dup")
            self.assertTrue(r1.startswith("OK"))
            r2 = self._cmd(s2, "LOGIN test_dup")
            self.assertTrue(r2.startswith("ERR"))
        finally:
            self._cmd(s1, "LOGOUT test_dup")
            s1.close()
            s2.close()

    def test_join_leave_list(self):
        s1 = self._req()
        s2 = self._req()
        try:
            self._cmd(s1, "LOGIN u1")
            self._cmd(s2, "LOGIN u2")
            self.assertTrue(self._cmd(s1, "JOIN u1 A").startswith("OK"))
            self.assertTrue(self._cmd(s2, "JOIN u2 A").startswith("OK"))

            resp = self._cmd(s1, "LIST_SALA A")
            membros = parse_list_sala(resp)
            self.assertIn("u1", membros)
            self.assertIn("u2", membros)

            self._cmd(s2, "LEAVE u2 A")
            resp = self._cmd(s1, "LIST_SALA A")
            membros = parse_list_sala(resp)
            self.assertIn("u1", membros)
            self.assertNotIn("u2", membros)
        finally:
            self._cmd(s1, "LOGOUT u1")
            self._cmd(s2, "LOGOUT u2")
            s1.close()
            s2.close()

    def test_eventos_via_sub(self):
        """Verifica que eventos de presença chegam via PUB."""
        sub = self.ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.setsockopt_string(zmq.SUBSCRIBE, "")
        sub.setsockopt(zmq.RCVTIMEO, 2000)
        sub.connect(f"tcp://127.0.0.1:{self.pres_port}")
        time.sleep(0.2)

        req = self._req()
        try:
            self._cmd(req, "LOGIN evt_test")
            msg = sub.recv_string()
            self.assertIn("ONLINE", msg)
            self.assertIn("evt_test", msg)

            self._cmd(req, "LOGOUT evt_test")
            msg = sub.recv_string()
            self.assertIn("OFFLINE", msg)
        finally:
            req.close()
            sub.close()


if __name__ == "__main__":
    unittest.main()
