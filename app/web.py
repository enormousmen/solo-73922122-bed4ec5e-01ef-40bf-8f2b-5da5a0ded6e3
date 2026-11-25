"""Flask 值班台应用工厂与 HTTP 路由。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from . import race_client
from .certutil import DEFAULT_DEVICE_NAME, ensure_certs
from .demo_traffic import SlowHandshakeManager, probe_health
from .device import DualStackDevice
from .quota import OTHER_STACK, STACKS
from .state import DeviceState


def create_app(
    *,
    device: DualStackDevice | None = None,
    state: DeviceState | None = None,
    ca_cert: str | Path | None = None,
    device_port: int | None = None,
    device_name: str = DEFAULT_DEVICE_NAME,
) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    # 直接输出中文 JSON（Flask 3 使用 json provider）
    app.config["JSON_AS_ASCII"] = False
    try:
        app.json.ensure_ascii = False  # type: ignore[attr-defined]
    except AttributeError:
        pass

    # 允许 main.py 注入已启动的内置设备；测试也可注入临时端口
    st = state or (device.state if device else DeviceState(device_name))
    app.extensions["device_state"] = st
    app.extensions["device_handle"] = device
    paths = ensure_certs(device_name)
    app.extensions["ca_cert"] = Path(ca_cert) if ca_cert else paths["ca_cert"]
    app.extensions["device_name"] = device_name
    if device_port is not None:
        app.extensions["device_port"] = device_port
    elif device is not None:
        app.extensions["device_port"] = device.port
    else:
        app.extensions["device_port"] = int(os.environ.get("DEVICE_PORT", "8443"))

    traffic = SlowHandshakeManager(
        port=int(app.extensions["device_port"]),
        ca_cert=app.extensions["ca_cert"],
        device_name=device_name,
        quota=st.quota,
    )
    app.extensions["slow_handshakes"] = traffic

    def full_state() -> dict[str, Any]:
        payload = st.snapshot()
        payload["traffic_clients"] = traffic.snapshot()
        return payload

    @app.get("/healthz")
    def healthz() -> Any:
        return jsonify({"ok": True})

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            device_name=device_name,
            device_port=app.extensions["device_port"],
            parallel_delay_ms=int(race_client.PARALLEL_DELAY * 1000),
            deadline_ms=int(race_client.DEADLINE * 1000),
        )

    @app.get("/api/state")
    def api_state() -> Any:
        return jsonify(full_state())

    @app.post("/api/settings")
    def api_settings() -> Any:
        st: DeviceState = app.extensions["device_state"]
        patch = request.get_json(silent=True) or {}
        try:
            settings = st.update_settings(patch)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "settings": settings, "state": full_state()})

    @app.post("/api/reset-count")
    def api_reset_count() -> Any:
        st.reset_count()
        return jsonify({"ok": True, "state": full_state()})

    @app.get("/api/quota")
    def api_quota() -> Any:
        return jsonify({"ok": True, "quota": st.quota.snapshot()})

    @app.get("/api/demo/traffic")
    def api_demo_traffic() -> Any:
        return jsonify({"ok": True, "clients": traffic.snapshot()})

    @app.post("/api/demo/slow-handshakes")
    def api_start_slow_handshakes() -> Any:
        body = request.get_json(silent=True) or {}
        stack = str(body.get("stack", "ipv4")).lower()
        if stack not in STACKS:
            return jsonify({"ok": False, "error": "stack must be ipv4 or ipv6"}), 400
        try:
            count = int(body.get("count", 2))
            hold_s = float(body.get("hold_s", 45.0))
            clients = traffic.start_batch(
                stack,
                count=count,
                hold_s=hold_s,
                replace=bool(body.get("replace", True)),
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "clients": clients,
                "traffic": traffic.snapshot(),
                "quota": st.quota.snapshot(),
                "state": full_state(),
            }
        )

    @app.post("/api/demo/clear-slow")
    def api_clear_slow_handshakes() -> Any:
        traffic.close_all(wait=True)
        return jsonify({"ok": True, "traffic": traffic.snapshot(), "state": full_state()})

    @app.post("/api/demo/probe")
    def api_demo_probe() -> Any:
        body = request.get_json(silent=True) or {}
        stack = str(body.get("stack", "ipv6")).lower()
        if stack == "other":
            source_stack = str(body.get("source_stack", "ipv4")).lower()
            stack = OTHER_STACK.get(source_stack, "ipv6")
        if stack not in STACKS:
            return jsonify({"ok": False, "error": "stack must be ipv4 or ipv6"}), 400
        try:
            timeout_s = max(1.0, min(15.0, float(body.get("timeout_s", 8.0))))
        except (TypeError, ValueError):
            timeout_s = 8.0
        probe = probe_health(
            stack=stack,
            port=int(app.extensions["device_port"]),
            ca_cert=app.extensions["ca_cert"],
            device_name=device_name,
            quota=st.quota,
            timeout_s=timeout_s,
        )
        return jsonify(
            {
                "ok": bool(probe.get("ok")),
                "probe": probe,
                "traffic": traffic.snapshot(),
                "state": full_state(),
            }
        )

    @app.post("/api/reload")
    def api_reload() -> Any:
        body = request.get_json(silent=True) or {}
        request_id = body.get("request_id")
        race = race_client.ReloadRace(
            port=int(app.extensions["device_port"]),
            ca_cert=app.extensions["ca_cert"],
            device_name=app.extensions["device_name"],
            parallel_delay=float(
                body.get("parallel_delay_s", race_client.PARALLEL_DELAY)
            ),
            deadline=float(body.get("deadline_s", race_client.DEADLINE)),
            request_id=str(request_id) if request_id else None,
        )
        result = race.run()
        return jsonify({"result": result.to_dict(), "device": full_state()})

    return app
