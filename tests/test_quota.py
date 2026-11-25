"""TLS 握手共用配额与抢占回收的专项自测。"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.certutil import ensure_certs  # noqa: E402
from app.device import DualStackDevice  # noqa: E402
from app.quota import IPV4, IPV6, QuotaManager  # noqa: E402
from app.state import DeviceState  # noqa: E402
from app.race_client import ReloadRace  # noqa: E402
from app.web import create_app  # noqa: E402


def free_tcp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class QuotaManagerUnitTests(unittest.TestCase):
    def test_home_then_borrow_and_same_stack_reject(self) -> None:
        q = QuotaManager()
        first = q.acquire(IPV4)
        second = q.acquire(IPV4)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.permit, IPV4)
        self.assertFalse(first.borrowed)
        self.assertEqual(second.permit, IPV6)
        self.assertTrue(second.borrowed)

        # 两个名额都被 IPv4 占用；第三个 IPv4 不属于“另一入口”，直接拒绝。
        self.assertIsNone(q.acquire(IPV4))

    def test_mixed_stack_full_rejects_new_connection(self) -> None:
        q = QuotaManager()
        v4 = q.acquire(IPV4)
        v6 = q.acquire(IPV6)
        self.assertIsNotNone(v4)
        self.assertIsNotNone(v6)
        self.assertIsNone(q.acquire(IPV4))
        self.assertIsNone(q.acquire(IPV6))
        self.assertEqual(q.snapshot()["counts"]["rejected"], 2)

    def test_preemption_reserves_slot_and_waits_for_actual_close(self) -> None:
        q = QuotaManager()
        old_home = q.acquire(IPV4, peer_port=1001)
        borrowed = q.acquire(
            IPV4,
            peer_port=1002,
            on_abort=lambda lease: q.close_lease(lease, reason="unit_abort"),
        )
        self.assertIsNotNone(old_home)
        self.assertIsNotNone(borrowed)
        assert old_home is not None and borrowed is not None

        new_v6 = q.acquire(IPV6, peer_port=2001)
        self.assertIsNotNone(new_v6)
        assert new_v6 is not None
        self.assertTrue(borrowed.closed_event.wait(0.1))
        self.assertEqual(new_v6.permit, IPV6)
        self.assertIs(q.permits[IPV4], old_home)
        self.assertIs(q.permits[IPV6], new_v6)
        self.assertEqual(q.snapshot()["counts"]["preempted"], 1)

        events = q.snapshot()["events"]
        names = [e["event"] for e in events]
        self.assertLess(names.index("preempt_selected"), names.index("lease_actually_closed"))
        self.assertLess(
            names.index("lease_actually_closed"),
            names.index("quota_acquired_after_reclaim"),
        )

    def test_other_connections_rejected_while_reclamation_reserved(self) -> None:
        q = QuotaManager()
        q.acquire(IPV4)
        victim = q.acquire(IPV4)
        assert victim is not None

        victim_selected = threading.Event()
        release_victim = threading.Event()
        acquired: dict[str, object] = {}

        def abort_and_maybe_wait(lease) -> None:
            victim_selected.set()
            self.assertTrue(release_victim.wait(timeout=2.0))
            q.close_lease(lease, reason="controlled_actual_close")

        def acquirer() -> None:
            acquired["lease"] = q.acquire(IPV6, on_abort=abort_and_maybe_wait, reclaim_timeout=5.0)

        thread = threading.Thread(target=acquirer, daemon=True)
        thread.start()
        self.assertTrue(victim_selected.wait(timeout=1.0))

        # 回收期间，名额只属于上面这个 IPv6 请求 token；其他请求一律拒绝。
        self.assertIsNone(q.acquire(IPV6))
        self.assertIsNone(q.acquire(IPV4))
        self.assertIsNotNone(q.snapshot()["reservation"])

        release_victim.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(acquired.get("lease"))
        self.assertIsNone(q.snapshot()["reservation"])

    def test_preempted_late_handshake_cannot_hand_off_to_http(self) -> None:
        q = QuotaManager()
        lease = q.acquire(IPV4)
        borrowed = q.acquire(IPV4)
        assert lease is not None
        assert borrowed is not None
        selected = threading.Event()

        def abort_only_mark(used_lease) -> None:
            selected.set()

        result: dict[str, object] = {}

        def preempting_acquire() -> None:
            result["new_lease"] = q.acquire(IPV6, on_abort=abort_only_mark, reclaim_timeout=5.0)

        thread = threading.Thread(target=preempting_acquire, daemon=True)
        thread.start()
        self.assertTrue(selected.wait(timeout=1.0))

        # 模拟被中止连接的 TLS 握手恰好在抢占选择之后才成功：
        # 原子移交检查必须失败，device 层因此不会调用 finish_request。
        self.assertFalse(q.hand_off_to_http(borrowed))
        q.close_lease(borrowed, reason="closed_after_denied_late_handshake")
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(result.get("new_lease"))
        # 未被选中的另一条 IPv4 本路名额不受影响。
        self.assertIs(q.permits[IPV4], lease)

    def test_http_handoff_releases_slot(self) -> None:
        q = QuotaManager()
        lease = q.acquire(IPV4)
        assert lease is not None
        self.assertTrue(q.hand_off_to_http(lease))
        self.assertIsNone(q.permits[IPV4])
        replacement = q.acquire(IPV6)
        self.assertIsNotNone(replacement)


class QuotaEndToEndTests(unittest.TestCase):
    certs: dict[str, Path]
    tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="quota-e2e-certs-")
        cls.certs = ensure_certs("switch-demo.local", Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.port = free_tcp_port()
        self.state = DeviceState(execution_delay=0.0)
        self.device = DualStackDevice(
            port=self.port,
            cert_path=self.certs["server_cert"],
            key_path=self.certs["server_key"],
            state=self.state,
        )
        self.device.start()
        time.sleep(0.12)
        self.app = create_app(
            device=self.device,
            state=self.state,
            ca_cert=self.certs["ca_cert"],
            device_port=self.port,
        )
        self.client = self.app.test_client()
        self.raw_sockets: list[socket.socket] = []

    def tearDown(self) -> None:
        try:
            self.app.extensions["slow_handshakes"].close_all(wait=True)
        except Exception:
            pass
        for sock in self.raw_sockets:
            try:
                sock.close()
            except OSError:
                pass

        # 等服务端握手线程观察 EOF/RST，并在 finally 中关闭 SSLSocket、释放名额，
        # 避免守护线程与测试拆卸竞态产生 ResourceWarning。
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.state.quota.snapshot()["active_count"] == 0:
                break
            time.sleep(0.02)
        self.device.stop()

    def _raw_connect(self, stack: str) -> socket.socket:
        family = socket.AF_INET6 if stack == IPV6 else socket.AF_INET
        host = "::1" if stack == IPV6 else "127.0.0.1"
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        addr = (host, self.port, 0, 0) if family == socket.AF_INET6 else (host, self.port)
        sock.connect(addr)
        self.raw_sockets.append(sock)
        return sock

    def _assert_directly_rejected(self, sock: socket.socket) -> None:
        try:
            data = sock.recv(1)
            self.assertEqual(data, b"")
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            # RST 或 EOF 都表示没有进入 TLS/HTTP；超时不允许。
            self.assertNotIsInstance(exc, socket.timeout)

    def test_api_two_slow_v4_then_v6_probe_preempts_and_orders_events(self) -> None:
        response = self.client.post(
            "/api/demo/slow-handshakes",
            json={"stack": IPV4, "count": 2, "replace": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertTrue(self.state.quota.wait_for_stack_active_count(IPV4, 2, timeout=2.0))

        # 第三个 IPv4 连接不是另一入口，拿不到名额，必须直接拒绝。
        third_v4 = self._raw_connect(IPV4)
        self._assert_directly_rejected(third_v4)

        probe_response = self.client.post("/api/demo/probe", json={"stack": IPV6})
        self.assertEqual(probe_response.status_code, 200)
        payload = probe_response.get_json()
        self.assertTrue(payload["ok"], payload.get("probe"))
        self.assertEqual(payload["probe"]["http_status"], 200)
        self.assertEqual(payload["probe"]["body"]["stack"], IPV6)

        # 恰好一条本地慢握手观察到实际断开，另一条仍保留。
        manager = self.app.extensions["slow_handshakes"]
        deadline = time.monotonic() + 2.0
        disconnected = []
        while time.monotonic() < deadline:
            clients = manager.snapshot()
            disconnected = [c for c in clients if c["state"] == "disconnected"]
            if len(disconnected) == 1:
                break
            time.sleep(0.03)
        self.assertEqual(len(disconnected), 1, manager.snapshot())
        self.assertTrue(disconnected[0]["disconnect_reason"])
        holding = [c for c in manager.snapshot() if c["state"] == "holding"]
        self.assertEqual(len(holding), 1)

        events = self.state.quota.snapshot()["events"]

        def indexes(name: str) -> list[int]:
            return [i for i, event in enumerate(events) if event["event"] == name]

        preempt_i = indexes("preempt_selected")[-1]
        closed_i = indexes("lease_actually_closed")[-1]
        reserved_i = indexes("quota_acquired_after_reclaim")[-1]
        tls_i = indexes("health_probe_tls_connected")[-1]
        self.assertLess(preempt_i, closed_i)
        self.assertLess(closed_i, reserved_i)
        self.assertLess(reserved_i, tls_i)
        self.assertGreaterEqual(self.state.quota.snapshot()["counts"]["rejected"], 1)

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("TLS 握手共用配额", page.get_data(as_text=True))

    def test_http_reload_keeps_running_after_quota_released(self) -> None:
        self.state.execution_delay = 0.5
        race_result: dict[str, object] = {}

        def run_reload() -> None:
            race_result["result"] = ReloadRace(
                port=self.port,
                ca_cert=self.certs["ca_cert"],
                device_name="switch-demo.local",
                request_id="quota-http-handoff",
            ).run().to_dict()

        reload_thread = threading.Thread(target=run_reload, daemon=True)
        reload_thread.start()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.state.reload_count == 0:
            time.sleep(0.02)
        self.assertEqual(self.state.reload_count, 1)

        # POST 已进入 Handler：握手名额应已释放，HTTP 执行不占用这两个名额。
        self.assertEqual(self.state.quota.snapshot()["active_count"], 0)
        first = self._raw_connect(IPV4)
        second = self._raw_connect(IPV4)
        self.assertTrue(self.state.quota.wait_for_stack_active_count(IPV4, 2, timeout=2.0))
        for sock in (first, second):
            sock.close()

        reload_thread.join(timeout=3.0)
        self.assertFalse(reload_thread.is_alive())
        result = race_result["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["status"], "delivered", result.get("error"))
        self.state.execution_delay = 0.0

    def test_mixed_occupancy_does_not_preempt_and_rejects(self) -> None:
        v4 = self._raw_connect(IPV4)
        v6 = self._raw_connect(IPV6)
        self.assertTrue(self.state.quota.wait_for_stack_active_count(IPV4, 1, timeout=2.0))
        self.assertTrue(self.state.quota.wait_for_stack_active_count(IPV6, 1, timeout=2.0))

        extra_v4 = self._raw_connect(IPV4)
        self._assert_directly_rejected(extra_v4)
        extra_v6 = self._raw_connect(IPV6)
        self._assert_directly_rejected(extra_v6)

        # 原来的两条连接仍在握手名额中，没有被误抢占。
        snap = self.state.quota.snapshot()
        self.assertEqual(snap["by_stack"][IPV4], 1)
        self.assertEqual(snap["by_stack"][IPV6], 1)
        self.assertEqual(snap["counts"]["preempted"], 0)
        # 保持引用到断言之后，避免静态分析认为变量未使用。
        self.assertIsNotNone(v4)
        self.assertIsNotNone(v6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
