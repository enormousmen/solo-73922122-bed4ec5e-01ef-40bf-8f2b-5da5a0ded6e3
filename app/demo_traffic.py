"""网页“慢握手占用 + 另一入口 HTTPS 探活”演示所需的本地流量工具。

慢握手客户端只完成 TCP connect，暂时不推进 TLS；服务端因此停在 TLS 握手/
故障注入等待阶段并持续占用 QuotaManager 名额。被服务端抢占时，客户端会记录
自己实际观察到 EOF/RST 的顺序，和服务端 quota 事件共用同一事件日志。
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .quota import STACKS, QuotaManager
from .race_client import HOSTS, build_client_context


class SlowHandshakeManager:
    def __init__(
        self,
        *,
        port: int,
        ca_cert: str | Path,
        device_name: str,
        quota: QuotaManager,
        hosts: dict[str, str] | None = None,
    ) -> None:
        self.port = port
        self.ca_cert = Path(ca_cert)
        self.device_name = device_name
        self.quota = quota
        self.hosts = dict(hosts or HOSTS)
        self._lock = threading.RLock()
        self._clients: dict[str, dict[str, Any]] = {}
        self._stops: dict[str, threading.Event] = {}
        self._sockets: dict[str, socket.socket] = {}
        self._threads: dict[str, threading.Thread] = {}

    def start_batch(
        self,
        stack: str,
        *,
        count: int = 2,
        hold_s: float = 45.0,
        replace: bool = True,
    ) -> list[dict[str, Any]]:
        if stack not in STACKS:
            raise ValueError(f"unknown stack: {stack!r}")
        count = max(1, min(4, int(count)))
        hold_s = max(1.0, min(120.0, float(hold_s)))

        if replace:
            self.close_all(wait=True)
            with self._lock:
                # 页面每次演示只关注当前这一批两条慢握手；清掉上一批的历史行，
                # 完整顺序仍保留在 quota event log 中。
                self._clients.clear()
                self._threads.clear()

        ids: list[str] = []
        for index in range(count):
            client_id = f"slow-{stack}-{uuid.uuid4().hex[:10]}"
            stop = threading.Event()
            record: dict[str, Any] = {
                "id": client_id,
                "stack": stack,
                "index": index + 1,
                "state": "starting",
                "hold_s": hold_s,
                "local_port": None,
                "connected_at": None,
                "disconnected_at": None,
                "disconnect_reason": None,
                "error": None,
            }
            with self._lock:
                self._clients[client_id] = record
                self._stops[client_id] = stop
                ids.append(client_id)
            thread = threading.Thread(
                target=self._run_slow,
                args=(client_id, stack, hold_s, stop),
                name=client_id,
                daemon=True,
            )
            with self._lock:
                self._threads[client_id] = thread
            thread.start()

        self.wait_connected(ids, timeout=2.0)
        ports = self.client_peer_ports(ids)
        self.quota.wait_for_peer_ports_present(ports, timeout=2.0)
        return [self._public_client(client_id) for client_id in ids]

    def wait_connected(self, client_ids: list[str], *, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if all(self._clients.get(cid, {}).get("local_port") for cid in client_ids):
                    return True
            time.sleep(0.02)
        return False

    def client_peer_ports(self, client_ids: list[str] | None = None) -> set[int]:
        ids = client_ids if client_ids is not None else list(self._clients)
        ports: set[int] = set()
        with self._lock:
            for cid in ids:
                port = self._clients.get(cid, {}).get("local_port")
                if isinstance(port, int):
                    ports.add(port)
        return ports

    def close_all(self, *, wait: bool = True) -> None:
        with self._lock:
            ids = list(self._clients.keys())
            stops = [self._stops[cid] for cid in ids if cid in self._stops]
            socks = [self._sockets[cid] for cid in ids if cid in self._sockets]
            threads = [self._threads[cid] for cid in ids if cid in self._threads]
            for stop in stops:
                stop.set()
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if wait:
            for thread in threads:
                thread.join(timeout=0.8)
            self.quota.wait_for_peer_ports_gone(
                {p for p in self.client_peer_ports(ids) if p}, timeout=1.5
            )

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._clients.values()]

    def _public_client(self, client_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._clients[client_id])

    def _update(self, client_id: str, **changes: Any) -> None:
        with self._lock:
            if client_id in self._clients:
                self._clients[client_id].update(changes)

    def _run_slow(
        self, client_id: str, stack: str, hold_s: float, stop: threading.Event
    ) -> None:
        family = socket.AF_INET6 if stack == "ipv6" else socket.AF_INET
        host = self.hosts[stack]
        sock: socket.socket | None = None
        disconnect_reason = "unknown"
        error: str | None = None
        local_port: int | None = None

        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            with self._lock:
                self._sockets[client_id] = sock
            sock.settimeout(2.0)
            addr: tuple[Any, ...]
            if family == socket.AF_INET6:
                addr = (host, self.port, 0, 0)
            else:
                addr = (host, self.port)
            sock.connect(addr)
            local_port = int(sock.getsockname()[1])
            now = time.time()
            self._update(
                client_id,
                state="holding",
                local_port=local_port,
                connected_at=now,
            )
            self.quota.record(
                "slow_client_tcp_connected",
                client_id=client_id,
                stack=stack,
                local_port=local_port,
            )

            # 不发送 ClientHello：服务端已 accept 并把连接计入 TLS 握手名额。
            sock.settimeout(0.1)
            deadline = time.monotonic() + hold_s
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    chunk = sock.recv(1)
                    if chunk == b"":
                        disconnect_reason = "observed_eof_after_server_close"
                        break
                    # TLS 握手完成前服务端不应发送应用数据；收到字节也继续保持。
                except socket.timeout:
                    continue
                except ConnectionResetError as exc:
                    disconnect_reason = "observed_rst_after_server_abort"
                    error = str(exc)
                    break
                except OSError as exc:
                    if stop.is_set():
                        disconnect_reason = "closed_by_demo_control"
                        error = None
                    else:
                        disconnect_reason = "observed_socket_error"
                        error = f"{type(exc).__name__}: {exc}"
                    break
            else:
                disconnect_reason = "client_hold_timeout_or_cancel"
        except Exception as exc:  # noqa: BLE001 - 演示客户端需把失败显示到页面
            disconnect_reason = "connect_failed"
            error = f"{type(exc).__name__}: {exc}"
            self._update(client_id, state="failed", error=error)
            self.quota.record(
                "slow_client_connect_failed",
                client_id=client_id,
                stack=stack,
                error=error,
            )
            return
        finally:
            if stop.is_set() and disconnect_reason == "unknown":
                disconnect_reason = "closed_by_demo_control"
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
            now = time.time()
            self._update(
                client_id,
                state="disconnected",
                disconnected_at=now,
                disconnect_reason=disconnect_reason,
                error=error,
            )
            self.quota.record(
                "slow_client_actually_disconnected",
                client_id=client_id,
                stack=stack,
                local_port=local_port,
                reason=disconnect_reason,
                error=error,
            )
            with self._lock:
                self._sockets.pop(client_id, None)
                self._stops.pop(client_id, None)


def probe_health(
    *,
    stack: str,
    port: int,
    ca_cert: str | Path,
    device_name: str,
    quota: QuotaManager,
    hosts: dict[str, str] | None = None,
    timeout_s: float = 8.0,
    request_id: str | None = None,
) -> dict[str, Any]:
    """从指定 IPv4/IPv6 入口发起一次真实受信任的 HTTPS GET /health。"""
    if stack not in STACKS:
        raise ValueError(f"unknown stack: {stack!r}")
    host_map = dict(hosts or HOSTS)
    host = host_map[stack]
    request_id = request_id or f"probe-{uuid.uuid4().hex[:10]}"
    t0 = time.monotonic()
    result: dict[str, Any] = {
        "request_id": request_id,
        "stack": stack,
        "ok": False,
        "stage": "connect",
        "http_status": None,
        "body": None,
        "error": None,
        "elapsed_ms": 0,
        "tcp_ms": None,
        "tls_ms": None,
    }
    quota.record("health_probe_start", stack=stack, request_id=request_id)

    family = socket.AF_INET6 if stack == "ipv6" else socket.AF_INET
    raw: socket.socket | None = None
    tls: socket.socket | None = None
    try:
        raw = socket.socket(family, socket.SOCK_STREAM)
        raw.settimeout(timeout_s)
        addr: tuple[Any, ...]
        if family == socket.AF_INET6:
            addr = (host, port, 0, 0)
        else:
            addr = (host, port)
        connect_start = time.monotonic()
        raw.connect(addr)
        result["tcp_ms"] = int((time.monotonic() - connect_start) * 1000)
        quota.record("health_probe_tcp_connected", stack=stack, request_id=request_id)

        result["stage"] = "tls"
        context = build_client_context(ca_cert)
        tls_start = time.monotonic()
        tls = context.wrap_socket(raw, server_hostname=device_name)
        raw = None
        result["tls_ms"] = int((time.monotonic() - tls_start) * 1000)
        result["tls_version"] = tls.version()
        quota.record(
            "health_probe_tls_connected",
            stack=stack,
            request_id=request_id,
            handshake_ms=result["tls_ms"],
            tls_version=tls.version(),
        )

        result["stage"] = "http"
        request_bytes = (
            f"GET /health HTTP/1.1\r\n"
            f"Host: {device_name}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        tls.sendall(request_bytes)

        response = http.client.HTTPResponse(tls, method="GET")  # type: ignore[arg-type]
        response.begin()
        raw_body = response.read(8192)
        result["http_status"] = response.status
        try:
            result["body"] = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except json.JSONDecodeError:
            result["body_text"] = raw_body.decode("utf-8", errors="replace")
        if response.status == 200 and isinstance(result["body"], dict) and result["body"].get("ok"):
            result["ok"] = True
        else:
            result["error"] = f"unhealthy HTTP {response.status}"
        quota.record(
            "health_probe_result",
            stack=stack,
            request_id=request_id,
            ok=result["ok"],
            http_status=response.status,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - 探活失败要显示给值班员
        result["error"] = f"{type(exc).__name__}: {exc}"
        quota.record(
            "health_probe_result",
            stack=stack,
            request_id=request_id,
            ok=False,
            stage=result["stage"],
            error=result["error"],
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    finally:
        for sock in (tls, raw):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
        result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        quota.record(
            "health_probe_client_closed",
            stack=stack,
            request_id=request_id,
            ok=result["ok"],
        )
    return result
