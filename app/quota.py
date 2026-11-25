"""IPv4/IPv6 管理入口共用的 TLS 握手名额管理。

模型：
- 总共两个名额，分别是 ``ipv4`` / ``ipv6`` 的本路名额；
- 本路名额空闲时优先使用本路，否则可借用另一路的空闲名额；
- 名额只覆盖“已 accept → TLS 握手 → 交给 HTTP”这段连接建立过程；
- 成功移交 HTTP 时立即释放名额；握手失败或被抢占时，等物理 socket 关闭后释放；
- 两个名额都被另一栈占用时，当前栈可以抢占一个占用其本路名额、且尚未交给
  HTTP 的连接。回收期间建立一个与请求 token 绑定的预留位，其它连接一律拒绝。

本模块只做并发状态与事件记录，不直接依赖 Flask 或 HTTP 实现。
"""
from __future__ import annotations

import itertools
import socket
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

IPV4 = "ipv4"
IPV6 = "ipv6"
STACKS: tuple[str, str] = (IPV4, IPV6)
OTHER_STACK = {IPV4: IPV6, IPV6: IPV4}

DEFAULT_RECLAIM_TIMEOUT_S = 2.0


def _now_wall() -> float:
    return time.time()


def _now_mono() -> float:
    return time.monotonic()


@dataclass
class Lease:
    """一个 TLS 握手名额的占用凭证。"""

    id: int
    token: str
    stack: str
    permit: str
    peer: str = ""
    peer_port: int | None = None
    borrowed: bool = False
    created_mono: float = field(default_factory=_now_mono)
    created_wall: float = field(default_factory=_now_wall)
    state: str = "handshaking"  # handshaking | aborting | handed_off | closed
    preempted: bool = False
    close_reason: str | None = None
    error: str | None = None
    abort_requested_mono: float | None = None
    abort_requested_wall: float | None = None
    handed_off_mono: float | None = None
    closed_mono: float | None = None
    closed_wall: float | None = None
    closed_event: threading.Event = field(default_factory=threading.Event, repr=False)
    on_abort: Callable[["Lease"], None] | None = field(default=None, repr=False)
    physical_socket: Any = field(default=None, repr=False)

    def age_ms(self, now_mono: float | None = None) -> int:
        now = _now_mono() if now_mono is None else now_mono
        end = self.closed_mono or self.handed_off_mono or now
        return max(0, int((end - self.created_mono) * 1000))

    def snapshot(self, now_mono: float | None = None) -> dict[str, Any]:
        now = _now_mono() if now_mono is None else now_mono
        return {
            "id": self.id,
            "token": self.token,
            "stack": self.stack,
            "permit": self.permit,
            "peer": self.peer,
            "peer_port": self.peer_port,
            "borrowed": self.borrowed,
            "state": self.state,
            "preempted": self.preempted,
            "close_reason": self.close_reason,
            "error": self.error,
            "age_ms": self.age_ms(now),
            "created_at": self.created_wall,
            "abort_requested_at": self.abort_requested_wall,
            "handed_off_at": (
                _wall_from_mono(self.handed_off_mono, self.created_mono, self.created_wall)
                if self.handed_off_mono
                else None
            ),
            "closed_at": self.closed_wall,
        }


def _wall_from_mono(value_mono: float, created_mono: float, created_wall: float) -> float:
    return created_wall + (value_mono - created_mono)


class QuotaManager:
    """两个栈共享的 TLS 握手配额与抢占状态机。"""

    def __init__(self, *, total: int = 2, event_limit: int = 300, recent_limit: int = 40) -> None:
        if total != 2:
            # 当前需求明确为总共两个名额；保留参数仅为快照/测试表达方便。
            raise ValueError("this demo implements exactly two shared handshake slots")
        self.total = total
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._lease_ids = itertools.count(1)
        self._event_seqs = itertools.count(1)
        self.started_mono = _now_mono()
        self.started_wall = _now_wall()

        # permit 名称就是其“本路所属栈”。None 表示当前没有握手连接占用。
        self.permits: dict[str, Lease | None] = {stack: None for stack in STACKS}
        self.reservation: dict[str, Any] | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=event_limit)
        self.recent_leases: deque[dict[str, Any]] = deque(maxlen=recent_limit)

        self.granted_total = 0
        self.handed_off_total = 0
        self.closed_total = 0
        self.rejected_total = 0
        self.preemption_total = 0

    # ---------------- 事件 / 快照 ----------------

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            return self._record_locked(event, **fields)

    def _record_locked(self, event: str, **fields: Any) -> dict[str, Any]:
        item: dict[str, Any] = {
            "seq": next(self._event_seqs),
            "event": event,
            "t_ms": int((_now_mono() - self.started_mono) * 1000),
            "time": _now_wall(),
        }
        item.update(fields)
        self.events.append(item)
        return dict(item)

    def snapshot(self) -> dict[str, Any]:
        now = _now_mono()
        with self._lock:
            active = [lease.snapshot(now) for lease in self.permits.values() if lease is not None]
            slots: list[dict[str, Any]] = []
            for permit_name in STACKS:
                lease = self.permits[permit_name]
                slots.append(
                    {
                        "permit": permit_name,
                        "home_stack": permit_name,
                        "occupied": lease is not None,
                        "lease": lease.snapshot(now) if lease else None,
                    }
                )
            by_stack = {stack: 0 for stack in STACKS}
            for lease in self.permits.values():
                if lease is not None:
                    by_stack[lease.stack] += 1

            available = sum(1 for lease in self.permits.values() if lease is None)
            if self.reservation is not None:
                # 预留名额在回收完成前对外仍不算“可分配”。
                available = 0

            return {
                "total": self.total,
                "available": available,
                "occupied": self.total - available if self.reservation is None else 2,
                "active_count": len(active),
                "by_stack": by_stack,
                "home_quota": {stack: 1 for stack in STACKS},
                "slots": slots,
                "active_leases": active,
                "recent_leases": list(self.recent_leases),
                "reservation": dict(self.reservation) if self.reservation else None,
                "counts": {
                    "granted": self.granted_total,
                    "handed_off": self.handed_off_total,
                    "closed": self.closed_total,
                    "rejected": self.rejected_total,
                    "preempted": self.preemption_total,
                },
                "events": list(self.events),
            }

    def wait_for_stack_active_count(
        self, stack: str, count: int, *, timeout: float = 2.0
    ) -> bool:
        deadline = _now_mono() + timeout
        with self._condition:
            while _now_mono() < deadline:
                current = sum(
                    1
                    for lease in self.permits.values()
                    if lease is not None and lease.stack == stack
                )
                if current >= count:
                    return True
                self._condition.wait(max(0.01, min(0.1, deadline - _now_mono())))
            return False

    def wait_for_peer_ports_gone(self, ports: set[int], *, timeout: float = 2.0) -> bool:
        if not ports:
            return True
        deadline = _now_mono() + timeout
        with self._condition:
            while _now_mono() < deadline:
                present = {
                    lease.peer_port
                    for lease in self.permits.values()
                    if lease is not None and lease.peer_port in ports
                }
                if not present:
                    return True
                self._condition.wait(max(0.01, min(0.05, deadline - _now_mono())))
            return False

    def wait_for_peer_ports_present(self, ports: set[int], *, timeout: float = 2.0) -> bool:
        if not ports:
            return True
        deadline = _now_mono() + timeout
        with self._condition:
            while _now_mono() < deadline:
                present = {
                    lease.peer_port
                    for lease in self.permits.values()
                    if lease is not None and lease.peer_port in ports
                }
                if ports.issubset(present):
                    return True
                self._condition.wait(max(0.01, min(0.05, deadline - _now_mono())))
            return False

    # ---------------- 名额申请 / 释放 ----------------

    def acquire(
        self,
        stack: str,
        *,
        peer: str = "",
        peer_port: int | None = None,
        token: str | None = None,
        on_abort: Callable[["Lease"], None] | None = None,
        physical_socket: Any = None,
        reclaim_timeout: float = DEFAULT_RECLAIM_TIMEOUT_S,
    ) -> Lease | None:
        """立即获取名额；满额且符合抢占条件时，回收并预留名额。

        不符合条件或回收期间已有其它预留时返回 ``None``，调用方应直接拒绝连接。
        """
        if stack not in STACKS:
            raise ValueError(f"unknown stack: {stack!r}")
        request_token = token or f"acq-{uuid.uuid4().hex[:12]}"

        victim: Lease | None = None
        with self._lock:
            lease = self._try_grant_locked(
                stack,
                request_token,
                peer=peer,
                peer_port=peer_port,
                on_abort=on_abort,
                physical_socket=physical_socket,
            )
            if lease is not None:
                self.granted_total += 1
                self._record_locked(
                    "quota_acquired",
                    lease_id=lease.id,
                    token=request_token,
                    stack=stack,
                    permit=lease.permit,
                    borrowed=lease.borrowed,
                    peer=peer,
                    peer_port=peer_port,
                )
                self._condition.notify_all()
                return lease

            reject_reason = "quota_full"
            active = [item for item in self.permits.values() if item is not None]
            if self.reservation is not None:
                reject_reason = "slot_reserved_for_reclamation"
                self.rejected_total += 1
                self._record_locked(
                    "quota_rejected",
                    stack=stack,
                    token=request_token,
                    reason=reject_reason,
                    reservation=dict(self.reservation),
                    peer=peer,
                    peer_port=peer_port,
                )
                self._condition.notify_all()
                return None
            elif len(active) == self.total and all(item.stack != stack for item in active):
                # 优先回收请求栈的本路名额；正常借用顺序下，这正是外栈借用的名额。
                victim = self.permits[stack]
                if victim is None or victim.state == "handed_off":
                    victim = max(active, key=lambda item: item.created_mono)
                self.reservation = {
                    "token": request_token,
                    "stack": stack,
                    "permit": victim.permit,
                    "victim_lease_id": victim.id,
                    "created_at": _now_wall(),
                }
                victim.preempted = True
                victim.state = "aborting"
                victim.abort_requested_mono = _now_mono()
                victim.abort_requested_wall = _now_wall()
                victim.on_abort = on_abort or victim.on_abort
                self.preemption_total += 1
                self._record_locked(
                    "preempt_selected",
                    stack=stack,
                    victim_stack=victim.stack,
                    lease_id=victim.id,
                    permit=victim.permit,
                    token=request_token,
                    peer_port=victim.peer_port,
                )
                self._condition.notify_all()
            else:
                if len(active) < self.total:
                    reject_reason = "quota_state_unavailable"
                elif any(item.stack == stack for item in active):
                    reject_reason = "same_stack_quota_full"
                else:
                    reject_reason = "mixed_stack_quota_full"
                self.rejected_total += 1
                self._record_locked(
                    "quota_rejected",
                    stack=stack,
                    token=request_token,
                    reason=reject_reason,
                    active=[item.snapshot() for item in active],
                    peer=peer,
                    peer_port=peer_port,
                )
                self._condition.notify_all()
                return None

        # 在管理器锁外触发 shutdown，避免系统调用阻塞状态查询。
        assert victim is not None
        self._invoke_abort(victim)

        closed = victim.closed_event.wait(timeout=reclaim_timeout)
        if not closed:
            self._force_close_victim(victim)
            victim.closed_event.wait(timeout=0.5)
            # 即使属主线程尚未完成异常展开，也由配额状态机完成关闭记账。
            self.close_lease(victim, reason="reclaim_force_close_timeout")

        with self._lock:
            reservation = self.reservation
            if (
                reservation is None
                or reservation.get("token") != request_token
                or reservation.get("victim_lease_id") != victim.id
            ):
                # 不应发生：预留只属于本次 acquire。保守拒绝，避免超额。
                self._record_locked(
                    "reclamation_lost",
                    stack=stack,
                    token=request_token,
                    lease_id=victim.id,
                )
                return None

            permit_name = str(reservation["permit"])
            self.reservation = None
            lease = Lease(
                id=next(self._lease_ids),
                token=request_token,
                stack=stack,
                permit=permit_name,
                peer=peer,
                peer_port=peer_port,
                borrowed=(permit_name != stack),
                on_abort=on_abort,
                physical_socket=physical_socket,
            )
            self.permits[permit_name] = lease
            self.granted_total += 1
            self._record_locked(
                "reclamation_complete",
                stack=stack,
                lease_id=victim.id,
                closed_lease_id=victim.id,
                new_lease_id=lease.id,
                permit=permit_name,
                actual_close_ms=(
                    int((victim.closed_mono - victim.abort_requested_mono) * 1000)
                    if victim.closed_mono and victim.abort_requested_mono
                    else None
                ),
            )
            self._record_locked(
                "quota_acquired_after_reclaim",
                stack=stack,
                lease_id=lease.id,
                token=request_token,
                permit=permit_name,
                peer=peer,
                peer_port=peer_port,
            )
            self._condition.notify_all()
            return lease

    def _try_grant_locked(
        self,
        stack: str,
        token: str,
        *,
        peer: str,
        peer_port: int | None,
        on_abort: Callable[["Lease"], None] | None,
        physical_socket: Any,
    ) -> Lease | None:
        # 回收预留存在时，除了等待该预留的 acquire 外（它不会重入本方法），
        # 所有新申请都不能占用名额。
        if self.reservation is not None:
            return None

        if self.permits[stack] is None:
            permit = stack
            borrowed = False
        else:
            other = OTHER_STACK[stack]
            if self.permits[other] is None:
                permit = other
                borrowed = True
            else:
                return None

        lease = Lease(
            id=next(self._lease_ids),
            token=token,
            stack=stack,
            permit=permit,
            peer=peer,
            peer_port=peer_port,
            borrowed=borrowed,
            on_abort=on_abort,
            physical_socket=physical_socket,
        )
        self.permits[permit] = lease
        return lease

    def hand_off_to_http(self, lease: Lease) -> bool:
        """把连接原子移交给 HTTP；若已被抢占则返回 False 且绝不移交。"""
        with self._lock:
            if lease.closed_event.is_set() or lease.state == "closed":
                return False
            if lease.preempted or lease.state == "aborting":
                lease.state = "closed"
                lease.close_reason = lease.close_reason or "preempted_before_http"
                if lease.closed_mono is None:
                    lease.closed_mono = _now_mono()
                    lease.closed_wall = _now_wall()
                if self.permits.get(lease.permit) is lease:
                    # 保留给等待中的回收流程；不从 permit 上抹掉，由 close_lease 处理。
                    pass
                self._record_locked(
                    "preempted_handshake_won_race",
                    lease_id=lease.id,
                    stack=lease.stack,
                    permit=lease.permit,
                )
                self._condition.notify_all()
                return False

            lease.state = "handed_off"
            lease.handed_off_mono = _now_mono()
            self.handed_off_total += 1
            if self.permits.get(lease.permit) is lease:
                self.permits[lease.permit] = None
            self.recent_leases.appendleft(lease.snapshot())
            self._record_locked(
                "quota_released_http_handoff",
                lease_id=lease.id,
                stack=lease.stack,
                permit=lease.permit,
            )
            self._condition.notify_all()
            return True

    def close_lease(self, lease: Lease, *, reason: str | None = None, error: str | None = None) -> None:
        """物理 socket 关闭后调用；幂等。"""
        with self._lock:
            already_closed = lease.closed_event.is_set()
            if not already_closed:
                lease.state = "closed"
                lease.closed_mono = _now_mono()
                lease.closed_wall = _now_wall()
            if reason:
                lease.close_reason = reason
            if error:
                lease.error = error[:300]
            if self.permits.get(lease.permit) is lease:
                self.permits[lease.permit] = None

            if not already_closed:
                self.closed_total += 1
                self.recent_leases.appendleft(lease.snapshot())
                reserved_for = (
                    self.reservation.get("token")
                    if self.reservation
                    and self.reservation.get("victim_lease_id") == lease.id
                    else None
                )
                self._record_locked(
                    "lease_actually_closed",
                    lease_id=lease.id,
                    stack=lease.stack,
                    permit=lease.permit,
                    reason=lease.close_reason,
                    peer_port=lease.peer_port,
                    reserved_for=reserved_for,
                )
                lease.closed_event.set()
            self._condition.notify_all()

    # ---------------- 内部 ----------------

    def _invoke_abort(self, victim: Lease) -> None:
        callback = victim.on_abort
        if callback is None:
            return
        try:
            callback(victim)
        except Exception as exc:  # 回调失败时走超时强关，不能打死申请线程
            with self._lock:
                self._record_locked(
                    "abort_callback_error",
                    lease_id=victim.id,
                    error=f"{type(exc).__name__}: {exc}",
                )

    def _force_close_victim(self, victim: Lease) -> None:
        sock = victim.physical_socket
        if sock is None:
            return
        with self._lock:
            self._record_locked("reclaim_force_closing_socket", lease_id=victim.id)
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
