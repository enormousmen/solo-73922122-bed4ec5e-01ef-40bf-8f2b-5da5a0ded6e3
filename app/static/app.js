/* 交换机重载台前端逻辑：原生 JS，无外部依赖 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var EVENT_LABELS = {
    race_start: "竞速开始",
    start_path: "启动连接",
    tcp_connected: "TCP 已连接",
    cert_verified: "证书校验通过",
    ipv6_passed_within_window: "IPv6 在 250ms 窗口内通过",
    ipv6_early_failure_detected: "检测到 IPv6 提前失败",
    parallel_window_elapsed: "250ms 窗口到点，IPv6 尚未通过",
    winner_selected: "成为胜出路径，发送唯一 POST",
    other_path_released: "另一路连接已释放",
    loser_released: "落败连接已释放（未发送）",
    post_started: "开始发送 POST",
    post_sent: "POST 字节已全部发出",
    receipt_received: "收到完整设备回执",
    receipt_failed: "回执缺失/连接中断 → 结果未知",
    attempt_failed: "该路径尝试失败",
    post_connection_error: "发送后连接错误",
    deadline_reached: "到达 3 秒总时限",
    both_paths_failed_early: "两路均提前失败",
    finalized: "本次操作结束"
  };

  var MODE_EXPLAIN = {
    ipv6: {
      ok: "正常情况下本机回环 TLS 握手只需几十毫秒，IPv6 会在 250ms 内通过并直接胜出，不启动 IPv4。",
      stall: "演示：IPv6 的 TCP 能连上，但 TLS 握手一直卡住；250ms 后值班台并行尝试 IPv4，由 IPv4 完成下发。",
      reset: "演示：IPv6 被设备快速拒绝（RST），值班台检测到提前失败后立即尝试 IPv4，不等 250ms。"
    },
    ipv4: {
      ok: "IPv4 作为并行备份路径，正常时可随时接管。",
      stall: "演示：IPv4 握手也卡住。若与 IPv6 故障叠加，3 秒内无法开始 POST，结果为“未下发”。",
      reset: "演示：IPv4 快速失败。若 IPv6 也未在时限内通过，则判定“未下发”。"
    }
  };

  function api(url, options) {
    return fetch(url, Object.assign({
      headers: { "Content-Type": "application/json" }
    }, options || {})).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) { throw new Error(data.error || ("HTTP " + r.status)); }
        return data;
      });
    });
  }

  function stackPill(stack) {
    if (!stack) return "";
    return '<span class="pill pill-' + stack + '">' + stack.toUpperCase() + "</span>";
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ---------------- 设备状态 / 计数 / 历史 ---------------- */

  var controlsHydrated = false;
  function hydrateControlsOnce(state) {
    if (controlsHydrated || !state.settings) return;
    ["ipv6", "ipv4"].forEach(function (st) {
      var mode = state.settings[st + "_mode"];
      if (mode) {
        var radio = document.querySelector('input[name="' + st + '_mode"][value="' + mode + '"]');
        if (radio) radio.checked = true;
      }
    });
    $("disconnect").checked = !!state.settings.disconnect_after_execute;
    syncExplain();
    controlsHydrated = true;
  }

  function renderState(state) {
    hydrateControlsOnce(state);
    $("count-total").textContent = state.reload_count;
    $("count-v6").textContent = state.stack_count.ipv6;
    $("count-v4").textContent = state.stack_count.ipv4;
    $("uptime").textContent = state.uptime_s + " s";

    var rows = $("history-body");
    if (!state.history.length) {
      rows.innerHTML = '<tr><td colspan="5" class="empty">暂无重载记录</td></tr>';
    } else {
      rows.innerHTML = state.history.map(function (h) {
        var t = new Date(h.time * 1000).toISOString().substring(11, 19);
        return "<tr>" +
          "<td class='mono'>#" + h.seq + "</td>" +
          "<td class='mono'>" + t + "</td>" +
          "<td>" + stackPill(h.stack) + "</td>" +
          "<td class='mono'>" + escapeHtml(h.request_id) + "</td>" +
          "<td>" + (h.disconnect
            ? '<span class="pill pill-disconnect">执行后断连</span>'
            : '<span class="pill pill-normal">完整回执</span>') + "</td>" +
          "</tr>";
      }).join("");
    }

    // 健康点
    $("health-dot").className = "dot dot-ok";
    renderQuota(state);
  }

  function refreshState() {
    return api("/api/state").then(renderState).catch(function () {
      $("health-dot").className = "dot dot-bad";
    });
  }

  /* ---------------- 演示设置 ---------------- */

  function currentSettings() {
    return {
      ipv6_mode: document.querySelector('input[name="ipv6_mode"]:checked').value,
      ipv4_mode: document.querySelector('input[name="ipv4_mode"]:checked').value,
      disconnect_after_execute: $("disconnect").checked
    };
  }

  function syncExplain() {
    ["ipv6", "ipv4"].forEach(function (st) {
      var mode = document.querySelector('input[name="' + st + '_mode"]:checked').value;
      $("explain-" + st).textContent = MODE_EXPLAIN[st][mode] || "";
    });
  }

  var saveTimer = null;
  function saveSettings() {
    syncExplain();
    var tip = $("settings-saved");
    api("/api/settings", { method: "POST", body: JSON.stringify(currentSettings()) })
      .then(function (data) {
        tip.textContent = "已应用 ✓";
        clearTimeout(saveTimer);
        saveTimer = setTimeout(function () { tip.textContent = ""; }, 1600);
        renderState(data.state);
      })
      .catch(function (err) { tip.textContent = "设置失败：" + err.message; });
  }

  document.querySelectorAll('input[name="ipv6_mode"], input[name="ipv4_mode"]').forEach(function (el) {
    el.addEventListener("change", saveSettings);
  });
  $("disconnect").addEventListener("change", saveSettings);

  var SCENARIOS = {
    normal:      { ipv6_mode: "ok",    ipv4_mode: "ok",    disconnect_after_execute: false },
    v6stall:     { ipv6_mode: "stall", ipv4_mode: "ok",    disconnect_after_execute: false },
    v6reset:     { ipv6_mode: "reset", ipv4_mode: "ok",    disconnect_after_execute: false },
    disconnect:  { ipv6_mode: "ok",    ipv4_mode: "ok",    disconnect_after_execute: true  },
    neither:     { ipv6_mode: "stall", ipv4_mode: "stall", disconnect_after_execute: false }
  };

  document.querySelectorAll(".btn-ghost[data-scenario]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var sc = SCENARIOS[btn.getAttribute("data-scenario")];
      if (!sc) return;
      document.querySelector('input[name="ipv6_mode"][value="' + sc.ipv6_mode + '"]').checked = true;
      document.querySelector('input[name="ipv4_mode"][value="' + sc.ipv4_mode + '"]').checked = true;
      $("disconnect").checked = sc.disconnect_after_execute;
      saveSettings();
    });
  });

  $("btn-reset-count").addEventListener("click", function () {
    api("/api/reset-count", { method: "POST" }).then(function (d) { renderState(d.state); });
  });

  /* ---------------- 重载与结果渲染 ---------------- */

  function kindForEvent(ev) {
    if (ev === "cert_verified" || ev === "receipt_received" || ev === "winner_selected") return "ok";
    if (ev === "attempt_failed" || ev === "receipt_failed") return "bad";
    if (ev === "deadline_reached" || ev === "parallel_window_elapsed" ||
        ev === "ipv6_early_failure_detected" || ev === "both_paths_failed_early") return "warn";
    return "";
  }

  function renderTimeline(events) {
    var wrap = $("timeline-wrap");
    wrap.classList.remove("hidden");
    $("timeline").innerHTML = events.map(function (e) {
      var label = EVENT_LABELS[e.event] || e.event;
      var detailParts = [];
      ["reason", "error", "http_status", "handshake_ms", "cn", "bytes", "connect_ms"].forEach(function (k) {
        if (e[k] !== undefined && e[k] !== null) {
          var v = typeof e[k] === "string" && e[k].length > 120 ? e[k].slice(0, 120) + "…" : e[k];
          detailParts.push(k + "=" + v);
        }
      });
      if (e.detail && typeof e.detail === "object") {
        // race_start 的大对象不展开
      }
      return "<li class='kind-" + kindForEvent(e.event) + "'>" +
        "<span class='t'>+" + e.t_ms + "ms</span>" +
        (e.stack ? "<span class='st st-" + e.stack + "'>" + e.stack + "</span>"
                 : "<span class='st st-system'>sys</span>") +
        "<span class='ev'>" + escapeHtml(label) + "</span>" +
        (detailParts.length ? "<span class='detail'>" + escapeHtml(detailParts.join(" · ")) + "</span>" : "") +
        "</li>";
    }).join("");
  }

  function renderResult(payload) {
    var r = payload.result;
    var banner = $("result-banner");
    banner.classList.remove("hidden", "running", "delivered", "not_sent", "unknown");
    banner.classList.add(r.status);

    var icon = r.status === "delivered" ? "✓" : (r.status === "unknown" ? "⚠" : "✕");
    var sub = "";
    if (r.status === "delivered" && r.receipt) {
      sub = "设备 " + escapeHtml(r.receipt.body.device) +
            " 经 " + (r.winner || "").toUpperCase() +
            " 完成执行，共用计数现为 #" + r.receipt.body.reload_count;
    } else if (r.status === "unknown") {
      sub = "POST 已开始发送，但未收到完整回执；不会换路重发。可在下方计数/历史中确认设备是否已执行。";
    } else {
      sub = "3 秒结束前没有任何一路开始发送 POST，未下发到设备。";
    }
    banner.innerHTML = "<span style='font-size:22px'>" + icon + "</span>" +
      "<div>" + escapeHtml(r.status_text) +
      "<span class='sub'>" + sub + "</span></div>";

    $("winner-row").classList.remove("hidden");
    var w = $("winner");
    w.textContent = r.winner ? r.winner.toUpperCase() + "（同一设备名校验通过）" : "无";
    w.className = r.winner ? "winner-" + r.winner : "";
    $("elapsed").textContent = r.elapsed_ms;
    $("post-started").textContent = r.post_started ? "是" : "否";

    if (r.receipt) {
      $("receipt-wrap").classList.remove("hidden");
      $("receipt").textContent = JSON.stringify(r.receipt.body, null, 2);
    } else {
      $("receipt-wrap").classList.add("hidden");
    }
    if (r.error) {
      $("error-wrap").classList.remove("hidden");
      var lines = [r.error, "", "各路径明细："];
      Object.keys(r.attempts).forEach(function (st) {
        var a = r.attempts[st];
        lines.push("[" + st + "] cert_passed=" + a.cert_passed +
          " failed=" + a.failed + (a.error ? " error=" + a.error : "") +
          (a.cert ? " cn=" + a.cert.subject_cn + " verified_against=" + a.cert.verified_against_name : ""));
      });
      $("error-text").textContent = lines.join("\n");
    } else {
      $("error-wrap").classList.add("hidden");
    }

    renderTimeline(r.timeline);
    renderState(payload.device);
  }

  var running = false;
  $("btn-reload").addEventListener("click", function () {
    if (running) return;
    running = true;
    var btn = $("btn-reload");
    btn.disabled = true;
    btn.classList.add("loading");
    btn.querySelector(".btn-text").textContent = "竞速下发中（最长 3 秒）…";

    var banner = $("result-banner");
    banner.classList.remove("hidden", "delivered", "not_sent", "unknown");
    banner.classList.add("running");
    banner.textContent = "正在双栈竞速：先 IPv6，" + window.APP_CONFIG.parallelDelayMs +
      "ms 未过证书校验则并行 IPv4 …";

    api("/api/reload", { method: "POST", body: "{}" })
      .then(renderResult)
      .catch(function (err) {
        banner.classList.remove("running");
        banner.classList.add("unknown");
        banner.textContent = "请求控制台失败：" + err.message;
      })
      .finally(function () {
        running = false;
        btn.disabled = false;
        btn.classList.remove("loading");
        btn.querySelector(".btn-text").textContent = "重载交换机配置";
        refreshState();
      });
  });

  /* ---------------- TLS 配额 / 抢占演示 ---------------- */

  var QUOTA_EVENT_LABELS = {
    quota_acquired: "取得 TLS 握手名额",
    quota_acquired_after_reclaim: "回收完成，新连接取得预留名额",
    quota_released_http_handoff: "连接移交 HTTP，释放握手名额",
    lease_actually_closed: "连接实际关闭，关闭顺序入账",
    quota_rejected: "无可用名额，新连接被直接拒绝",
    preempt_selected: "选中一条未移交 HTTP 的连接中止",
    abort_signal_sent: "已发出 shutdown/RST 中止信号",
    preempted_handshake_won_race: "抢占已先生效，禁止该连接移交 HTTP",
    preempted_completed_handshake_denied_http: "被中止连接完成握手，但仍不得处理请求",
    reclamation_complete: "旧名额回收并预留给新连接",
    slow_client_tcp_connected: "本地慢握手 TCP 已连接",
    slow_client_actually_disconnected: "慢握手客户端观察到对端实际断开",
    slow_client_connect_failed: "慢握手客户端连接失败",
    health_probe_start: "另一入口 HTTPS 探活开始",
    health_probe_tcp_connected: "探活 TCP 已连接（可能在等待回收名额）",
    health_probe_tls_connected: "探活 TLS 握手完成",
    health_probe_result: "HTTPS 探活结果",
    health_probe_client_closed: "探活客户端已关闭"
  };

  var LEASE_STATE_LABELS = {
    handshaking: "TLS 握手中/慢握手保持",
    aborting: "抢占中止中",
    handed_off: "已移交 HTTP",
    closed: "已实际关闭"
  };

  function quotaEventKind(ev) {
    if (ev.event === "quota_rejected" || ev.event === "slow_client_connect_failed" ||
        (ev.event === "health_probe_result" && ev.ok === false)) return "bad";
    if (ev.event === "lease_actually_closed" || ev.event === "abort_signal_sent" ||
        ev.event === "preempt_selected" || ev.event === "reclamation_complete") return "warn";
    if (ev.event === "quota_released_http_handoff" ||
        ev.event === "quota_acquired_after_reclaim" ||
        ev.event === "health_probe_tls_connected" ||
        (ev.event === "health_probe_result" && ev.ok === true)) return "ok";
    return "";
  }

  function renderQuota(state) {
    var q = state.quota;
    if (!q || !$("quota-summary")) return;

    var reservation = q.reservation
      ? '<span class="quota-reserved">回收预留：' + q.reservation.stack.toUpperCase() +
        " → permit " + q.reservation.permit.toUpperCase() + "</span>"
      : "";
    $("quota-summary").innerHTML =
      '<div class="qstat"><span>名额占用</span><b>' + q.occupied + " / " + q.total + '</b></div>' +
      '<div class="qstat"><span>IPv4 占用</span><b class="v4">' + (q.by_stack.ipv4 || 0) + '</b></div>' +
      '<div class="qstat"><span>IPv6 占用</span><b class="v6">' + (q.by_stack.ipv6 || 0) + '</b></div>' +
      '<div class="qstat"><span>拒绝 / 抢占</span><b>' + q.counts.rejected + " / " + q.counts.preempted + '</b></div>' +
      reservation;

    $("quota-slots").innerHTML = q.slots.map(function (slot) {
      if (!slot.occupied || !slot.lease) {
        return '<div class="slot-card free"><div class="slot-title">' +
          slot.permit.toUpperCase() + ' 本路名额</div><div class="slot-state">空闲</div></div>';
      }
      var l = slot.lease;
      var classes = "slot-card occupied stack-" + l.stack + (l.preempted ? " preempted" : "");
      return '<div class="' + classes + '">' +
        '<div class="slot-title">' + slot.permit.toUpperCase() + " 本路名额" +
          (l.borrowed ? '<span class="tag-borrow">被 ' + l.stack.toUpperCase() + " 借用</span>" : "") +
        "</div>" +
        '<div class="slot-line">' + stackPill(l.stack) +
          "<strong>#" + l.id + "</strong>" +
          (l.preempted ? '<span class="tag-preempt">已选中止</span>' : "") +
        "</div>" +
        '<div class="slot-state">' + escapeHtml(LEASE_STATE_LABELS[l.state] || l.state) + "</div>" +
        '<div class="slot-meta">peer_port=' + escapeHtml(l.peer_port || "-") +
          " · " + (l.age_ms / 1000).toFixed(1) + "s</div>" +
      "</div>";
    }).join("");

    var clients = state.traffic_clients || [];
    var body = $("slow-clients-body");
    if (!clients.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty">尚未发起本地慢握手</td></tr>';
    } else {
      body.innerHTML = clients.map(function (c) {
        var stateClass = c.state === "disconnected" ? "client-off" :
          (c.state === "failed" ? "client-failed" : "client-on");
        return "<tr class='" + stateClass + "'>" +
          "<td class='mono'>" + escapeHtml(c.id.slice(-10)) + "</td>" +
          "<td>" + stackPill(c.stack) + "</td>" +
          "<td class='mono'>" + escapeHtml(c.local_port || "–") + "</td>" +
          "<td>" + escapeHtml(c.state) + "</td>" +
          "<td class='mono small'>" + escapeHtml(c.disconnect_reason || c.error || "保持中") + "</td>" +
        "</tr>";
      }).join("");
    }

    var events = (q.events || []).slice(-45);
    $("quota-events").innerHTML = events.map(function (e) {
      var label = QUOTA_EVENT_LABELS[e.event] || e.event;
      var keys = ["stack", "victim_stack", "lease_id", "new_lease_id", "permit", "reason",
        "ok", "http_status", "elapsed_ms", "handshake_ms", "local_port", "client_id", "error"];
      var details = keys.filter(function (k) {
        return e[k] !== undefined && e[k] !== null && e[k] !== "";
      }).map(function (k) {
        var v = String(e[k]);
        if (v.length > 90) v = v.slice(0, 90) + "…";
        return k + "=" + v;
      });
      return "<li class='qevent kind-" + quotaEventKind(e) + "'>" +
        "<span class='qseq'>#" + e.seq + "</span>" +
        "<span class='qt'>+" + e.t_ms + "ms</span>" +
        (e.stack ? "<span class='st st-" + e.stack + "'>" + e.stack + "</span>"
                 : "<span class='st st-system'>sys</span>") +
        "<span class='qev'>" + escapeHtml(label) + "</span>" +
        (details.length ? "<span class='qdetail'>" + escapeHtml(details.join(" · ")) + "</span>" : "") +
      "</li>";
    }).join("");
    var box = $("quota-events");
    box.scrollTop = box.scrollHeight;
  }

  function selectedSlowStack() {
    return $("slow-stack").value === "ipv6" ? "ipv6" : "ipv4";
  }

  function otherStack(stack) {
    return stack === "ipv4" ? "ipv6" : "ipv4";
  }

  function updateProbeButtonLabel() {
    var target = otherStack(selectedSlowStack());
    $("btn-probe-other").textContent = "② 从另一入口 HTTPS 探活（" + target.toUpperCase() + "）";
  }

  function setQuotaTip(text, isError) {
    var tip = $("quota-action-tip");
    tip.textContent = text || "";
    tip.style.color = isError ? "var(--bad)" : "var(--ok)";
  }

  function renderProbe(probe) {
    var banner = $("probe-banner");
    var detail = $("probe-detail");
    banner.classList.remove("hidden", "running", "delivered", "not_sent", "unknown");
    if (probe.ok) {
      banner.classList.add("delivered");
      banner.innerHTML = "<span>✓</span><div><b>" + probe.stack.toUpperCase() +
        " HTTPS 探活成功</b><span class='sub'>HTTP " + probe.http_status +
        " · 总耗时 " + probe.elapsed_ms + "ms · TLS " + (probe.tls_ms == null ? "–" : probe.tls_ms) +
        "ms；若触发抢占，事件列表中可看到完整回收顺序。</span></div>";
    } else {
      banner.classList.add("not_sent");
      banner.innerHTML = "<span>✕</span><div><b>" + probe.stack.toUpperCase() +
        " HTTPS 探活失败/被拒绝</b><span class='sub'>阶段 " + escapeHtml(probe.stage || "?") +
        " · 耗时 " + probe.elapsed_ms + "ms · " + escapeHtml(probe.error || "unknown") +
        "</span></div>";
    }
    detail.classList.remove("hidden");
    detail.textContent = JSON.stringify(probe, null, 2);
  }

  var quotaBusy = false;
  function withQuotaButtonsDisabled(busy) {
    ["btn-start-slow", "btn-probe-other", "btn-demo-quota", "btn-clear-slow"].forEach(function (id) {
      $(id).disabled = busy;
    });
  }

  function startSlowHandshakes() {
    if (quotaBusy) return Promise.resolve();
    quotaBusy = true;
    withQuotaButtonsDisabled(true);
    var stack = selectedSlowStack();
    setQuotaTip("正在 " + stack.toUpperCase() + " 建立两条本地慢握手…");
    return api("/api/demo/slow-handshakes", {
      method: "POST",
      body: JSON.stringify({ stack: stack, count: 2, replace: true })
    }).then(function (data) {
      renderState(data.state);
      setQuotaTip("两条 " + stack.toUpperCase() + " 慢握手已占满两个名额（含借用另一路）。");
    }).catch(function (err) {
      setQuotaTip("发起慢握手失败：" + err.message, true);
    }).finally(function () {
      quotaBusy = false;
      withQuotaButtonsDisabled(false);
    });
  }

  function runOtherProbe() {
    if (quotaBusy) return Promise.resolve();
    quotaBusy = true;
    withQuotaButtonsDisabled(true);
    var target = otherStack(selectedSlowStack());
    var banner = $("probe-banner");
    banner.classList.remove("hidden", "delivered", "not_sent", "unknown");
    banner.classList.add("running");
    banner.textContent = "正在从 " + target.toUpperCase() + " 发起 HTTPS 探活；满额时先中止并回收一条慢握手…";
    return api("/api/demo/probe", {
      method: "POST",
      body: JSON.stringify({ stack: target, timeout_s: 8 })
    }).then(function (data) {
      renderProbe(data.probe);
      renderState(data.state);
    }).catch(function (err) {
      banner.classList.remove("running");
      banner.classList.add("unknown");
      banner.textContent = "探活控制台请求失败：" + err.message;
    }).finally(function () {
      quotaBusy = false;
      withQuotaButtonsDisabled(false);
      refreshState();
    });
  }

  $("slow-stack").addEventListener("change", updateProbeButtonLabel);
  $("btn-start-slow").addEventListener("click", startSlowHandshakes);
  $("btn-probe-other").addEventListener("click", runOtherProbe);
  $("btn-demo-quota").addEventListener("click", function () {
    startSlowHandshakes().then(function () {
      return new Promise(function (resolve) { setTimeout(resolve, 250); });
    }).then(runOtherProbe);
  });
  $("btn-clear-slow").addEventListener("click", function () {
    api("/api/demo/clear-slow", { method: "POST" })
      .then(function (data) { renderState(data.state); setQuotaTip("本地慢连接已清理。"); })
      .catch(function (err) { setQuotaTip("清理失败：" + err.message, true); });
  });
  updateProbeButtonLabel();

  /* ---------------- 启动 ---------------- */
  syncExplain();
  refreshState();
  setInterval(refreshState, 1000);
})();
