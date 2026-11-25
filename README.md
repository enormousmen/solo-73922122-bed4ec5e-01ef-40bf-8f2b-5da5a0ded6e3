# 交换机配置重载台（Flask · 双栈 HTTPS 竞速下发）

值班员点击“重载配置”后，控制台通过**内置的本地双栈 HTTPS 样例设备**完成一次严格规则的竞速下发，并在网页展示设备回执、竞速时间线、三种判定以及 IPv4/IPv6 两路共用的重载计数。

当前版本还为 IPv4/IPv6 管理入口加入了**总共 2 个、两路共享的 TLS 握手名额**，支持本路名额、跨路借用、异栈抢占回收、回收预留和无名额直接 RST 拒绝，并提供专门的网页面板演示“同路两条慢握手 → 另一入口 HTTPS 探活”的完整断开/回收顺序。

## 做成的功能

### 核心竞速规则（`app/race_client.py`）

1. **IPv6 优先**：先连接 `https://[::1]:8443/reload`。
2. **250ms 并行窗口**：从操作开始计时，IPv6 在 250ms 内没有通过 TLS 证书校验，立即在旁并行启动 IPv4（`https://127.0.0.1:8443/reload`）。
3. **提前失败立即切 IPv4**：IPv6 若在 250ms 内连接被拒/RST/证书校验失败，检测到终态后**立刻**启动 IPv4，不等满窗口。
4. **同一设备名校验两路证书**：两路 TLS 都以 `server_hostname="switch-demo.local"` 做 SNI 与 hostname 校验，信任同一张内置 CA；证书 SAN 含该设备名、`localhost`、`127.0.0.1`、`::1`。设备名不匹配时两路都会被拒（有自测覆盖）。
5. **最先通过者只发一次 POST**：证书校验通过后在同一把锁内原子选出唯一 winner，仅它在该连接上发送**一次** POST；另一路无论仍在握手还是已通过待选，都会被立即关闭释放。`parallel_delay=0` 的双路齐发用例连跑 15 次，计数始终为 1。
6. **整次操作 3 秒上限**：默认 `deadline=3.0s`，到点关闭所有残留 socket。
7. **未下发（`not_sent`）**：截止时 POST **尚未开始发送**（例如双路握手都卡住），标红“未下发”，设备计数不增加。
8. **结果未知（`unknown`）且不重发**：一旦 POST **已经开始发送**，之后任何断连/超时/回执不完整都判定为“结果未知”，**不会**换另一路重发；设备侧共用计数最多 +1，可据此核对设备是否其实已执行。
9. “是否已开始发送”与“截止/取消”在同一把锁下协调，消除“到点边界上误标状态”的竞态。

### 内置双栈样例设备（`app/device.py` + `app/state.py`）

- 同端口分别绑定 `::1`（AF_INET6）与 `127.0.0.1`（AF_INET），两个 `ThreadingHTTPServer` 独立线程服务，共用同一个 `DeviceState`。
- `POST /reload`：在共用锁内递增**两路共用**的 `reload_count` 与分栈计数 `stack_count.{ipv6,ipv4}`，返回 JSON 回执（含新计数、实际服务栈、request_id、时间）。
- 故障注入（可按栈分别设置）：
  - `ok`：正常 TLS + 回执；
  - `stall`：accept TCP 后**卡住 TLS 握手**（不回 ServerHello），用于演示“IPv6 握手卡住”；
  - `reset`：accept 后立即 RST，用于演示“IPv6 提前失败”。
- `disconnect_after_execute`：计数 +1（设备已执行）后不发完整回执、直接 RST 断连，用于演示“设备执行后断连 → 结果未知”。
- `GET /health` 设备栈探活。

### 共用 TLS 握手配额（`app/quota.py` + `app/device.py` + `app/demo_traffic.py`）

规则与实现要点：

1. **总共 2 个名额，每路各 1 个本路名额**：permit 名称为 `ipv4` / `ipv6`；某栈申请时优先占用自己的本路名额，本路忙而另一路空闲时自动借用。
2. **名额覆盖 accept → TLS 握手 → 移交 HTTP**：设备在每连接线程中先申请名额，再执行故障注入/TLS 握手。TLS 成功后通过 `QuotaManager.hand_off_to_http()` 与抢占状态做原子核对；允许移交时立即释放握手名额，之后重载由 `BaseHTTPRequestHandler` 原流程处理。
3. **实际关闭后才完成回收**：握手失败或被抢占时，设备线程先关闭登记在 lease 上的 raw/SSL socket，再调用 `close_lease()`；配额事件 `lease_actually_closed` 即代表物理关闭记账完成。
4. **同路占满，异栈可抢占一条未移交 HTTP 的连接**：例如两条 IPv4 占掉 `ipv4` 本路和空闲的 `ipv6` 借用名额后，IPv6 新连接会选中占用 IPv6 本路 permit 的借用连接，设置 `preempted/reservation`，发送 `SO_LINGER(0)` + `shutdown(SHUT_RDWR)` 唤醒握手。
5. **回收期间名额只留给触发回收的新连接 token**：此时其它 IPv4/IPv6 申请都会看到 `slot_reserved_for_reclamation` 并被直接拒绝；旧连接实际关闭后，等待中的指定请求才取得该 permit 并开始 TLS 握手。
6. **晚完成握手也不能进入 HTTP**：若旧连接在抢占选择之后才从 `do_handshake()` 返回，`hand_off_to_http()` 返回 `False`，设备层直接关闭，不调用 `finish_request()` / `ReloadHandler`。有专门的状态机测试覆盖这个竞态。
7. **其余满额连接直接拒绝**：同栈第三个连接、IPv4/IPv6 混占后的新连接、回收预留期间的其它连接都不排队，设备 accept 后设置 `SO_LINGER` 并 RST/关闭。
8. **不影响已移交 HTTP 的重载**：POST 处理（含执行延迟、执行后断连）不再占握手名额；有测试验证 HTTP 执行期间两个握手名额仍可被新的慢握手占用，且原重载仍 `delivered`。

新增/使用的 HTTP API：

- `GET /api/state`：设备状态 + `quota` 名额快照/事件 + `traffic_clients` 慢客户端状态。
- `GET /api/quota`：只取配额快照。
- `POST /api/demo/slow-handshakes`：body 如 `{"stack":"ipv4","count":2,"replace":true}`，在控制台进程内建立两条本地 TCP 慢握手（连接后暂时不推进 TLS），并等待服务端确认两个名额已占用。
- `POST /api/demo/probe`：body 如 `{"stack":"ipv6","timeout_s":8}`，用内置 CA 做真实受信任 HTTPS `GET /health`；满额时该请求会触发抢占回收。
- `POST /api/demo/clear-slow`：关闭演示慢客户端。
- `GET /api/demo/traffic`：查看慢客户端记录。

### 网页控制台（`app/templates/index.html` + `app/static`）

- 大号**两路共用重载计数**，以及 IPv6/IPv4 分计数、运行时长、最近 30 条设备执行记录。
- 重载按钮（执行中转圈并禁用）、三态结果横幅：
  - 绿色“已下发，收到设备回执”+ 格式化回执 JSON；
  - 红色“未下发”；
  - 橙色“结果未知”。
- winner 路径、总耗时、POST 是否已开始、逐毫秒**竞速时间线**（TCP、证书校验、250ms 窗口、winner、释放另一路、POST 开始/发出、回执/失败、3s 截止等），以及每路证书 CN/SAN/校验名与错误明细。
- 演示控制面板：IPv6/IPv4 三态单选、执行后断连开关、五个一键场景按钮（双栈正常 / IPv6 卡住 / IPv6 提前失败 / 执行后断连 / 双路均不可用）、计数清零；状态每 2.5s 自动刷新。
- 纯原生 HTML/CSS/JS，无外部 CDN/字体，离线可用。
- **TLS 配额演示面板**：可选择慢握手入口（IPv4/IPv6），一键发起两条本地慢握手、从另一入口发起真实 HTTPS 探活，或运行完整链路；页面展示两个 permit 卡位、借用/抢占/预留状态、拒绝/抢占计数、慢客户端观察到的 EOF/RST，以及按全局 `seq` 排序的“选中止 → abort 信号 → 实际关闭 → 回收完成 → 新连接 TLS 完成/探活结果”事件流。状态每秒刷新，操作响应也会立即渲染。

### 部署

- `Dockerfile`：`python:3.12-slim`，安装 Flask + cryptography，非 root 运行，带 healthcheck。
- `docker-compose.yml`：单服务 `reload-console`，映射 `http://localhost:8080`。
- Flask 值班台与样例设备在**同一容器网络命名空间**内经 `::1` / `127.0.0.1` 互访，因此**不需要**为 Docker daemon 开启 IPv6；只要容器内 lo 上有 `::1`（Docker 默认如此）即可。
- 证书在启动时用 `cryptography` 自动生成到 `/app/certs`（Dockerfile 已将该目录分配给非 root 用户；无需挂载卷）。想取出内置 CA 可执行 `docker compose cp reload-console:/app/certs/ca.pem ./ca.pem`。

## 目录结构

```
.
├── app/
│   ├── main.py           # 入口：生成证书 → 启动双栈设备 → 启动 Flask
│   ├── web.py            # Flask 工厂与 /api/* 路由
│   ├── race_client.py    # 双栈竞速、250ms 窗口、3s 截止、三态判定
│   ├── quota.py          # 两栈共享的 2 个 TLS 握手名额、预留与抢占状态机
│   ├── demo_traffic.py   # 网页演示用本地慢握手客户端与 HTTPS /health 探活
│   ├── device.py         # 双栈 HTTPS 样例设备、配额接入与故障注入
│   ├── state.py          # 两路共用计数/设置/历史（加锁）
│   ├── certutil.py       # 内置 CA 与 SAN 服务器证书生成
│   ├── templates/index.html
│   └── static/{style.css,app.js}
├── tests/test_selftest.py
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 启动方式

### Docker Compose（推荐交付方式）

```bash
docker compose up --build
# 打开 http://localhost:8080
```

停止：`docker compose down`（保留证书卷）；连证书卷一起清理：`docker compose down -v`。

### 本地直接运行（无需 Docker）

需要 Python 3.11+：

```bash
python3 -m pip install -r requirements.txt
python3 -m app.main
# http://127.0.0.1:8080
```

## 演示操作与预期现象

| 操作 | 预期结果 |
| --- | --- |
| 默认设置直接点“重载交换机配置” | 约几十 ms 内 **IPv6 证书通过并胜出**，不启动 IPv4；绿色成功，共用计数 +1（IPv6 分计数 +1） |
| 点场景“IPv6 握手卡住 → IPv4”再重载 | 时间线显示 `+250ms parallel_window_elapsed` 后启动 IPv4，IPv4 胜出并拿到回执；计数 +1（IPv4 +1，IPv6 +0） |
| 点场景“IPv6 提前失败 → 立即 IPv4” | `start_path` 的 reason 为 `ipv6_early_failure`，通常几十 ms 内即启动 IPv4，**早于 250ms** |
| 点场景“执行后断连 → 结果未知” | 橙色“结果未知”，`POST 已开始=是`，无回执；共用计数仍 +1，且等待后不出现第二次增加（无重发） |
| 点场景“双路均不可用 → 未下发（3s）” | 约 **3000ms** 后红色“未下发”，`POST 已开始=否`、无 winner，计数不变 |
| 在“TLS 握手共用配额”面板选 IPv4，点“发起两条本地慢握手” | 两个 IPv4 慢客户端 holding；卡位显示 IPv4 占用本路 `ipv4` permit，并借用空闲 `ipv6` permit |
| 不清理，直接点“从另一入口 HTTPS 探活（IPv6）” | 探活绿色 `200 /health`；事件按 seq 显示选中 IPv4 借用连接 → abort → 实际关闭 → 预留名额移交 IPv6 → TLS/探活成功；一条慢客户端显示 EOF/RST，另一条保持 |
| 两条同路慢握手后，再开第三个同路连接 | 不进入 TLS/HTTP，直接收到 RST/EOF；`quota.counts.rejected` 增加 |
| 任意时刻看顶部计数/底部记录表 | 两路共用同一 `reload_count`，并列出每次实际由哪一栈接收 |

HTTP 直调示例：

```bash
curl -s -X POST localhost:8080/api/settings \
  -H 'Content-Type: application/json' -d '{"ipv6_mode":"stall"}'
curl -s -X POST localhost:8080/api/reload -H 'Content-Type: application/json' -d '{}'
curl -s localhost:8080/api/state
```

## 自测

自动化测试在临时目录生成证书、用随机端口真实启动双栈 TLS 设备和 Flask 应用。现有套件共 **4 个测试类、20 个测试方法**：

```bash
python3 -m unittest discover -s tests -v
```

最近一次实测结果（沙箱 Python 3.11.2 / Flask 3.1.3 / cryptography 50.0.2；依赖安装在独立目录并通过 `PYTHONPATH` 注入，同时启用 `-W error::ResourceWarning`）：

```
Ran 20 tests in 10.827s

OK
```

原有 11 个测试继续覆盖：正常 IPv6 胜出且只发一次 POST；IPv6 stall 后恰在 ~250ms 启动 IPv4 并由其成功；IPv6 RST 后以 ipv6_early_failure 立即启动 IPv4；执行后断连判 unknown 且计数只 +1；双 stall 耗满约 3.0s 判 not_sent；parallel_delay=0 双路齐发仍恰好一次 POST；错误设备名证书校验被拒判 not_sent；设置校验；Flask 首页/healthz/settings/reload/reset 路由。

新增 `tests/test_quota.py` 的 9 个测试覆盖：

- IPv4 先占本路名额、第二条 IPv4 借用 IPv6 空闲名额；
- 同路第三条连接直接拒绝；
- IPv4/IPv6 混占后任意新连接均拒绝且不发生误抢占；
- 异栈申请触发抢占，且事件顺序为 `preempt_selected → lease_actually_closed → quota_acquired_after_reclaim`；
- 回收预留期间，其它 IPv4/IPv6 申请均被拒绝；
- 抢占后才完成 TLS 握手的旧连接无法通过 `hand_off_to_http()`，即不会进入 HTTP Handler；
- 成功移交 HTTP 后立即释放名额；
- 真实 Flask API + 双栈设备端到端：两条 IPv4 慢握手 → 第三条 IPv4 被 RST/EOF 拒绝 → IPv6 HTTPS `/health` 探活 200，恰好一条慢客户端观察到 EOF/RST，另一条保持；
- HTTP reload 已进入 Handler 后不再占用握手名额，其 0.5s 执行延迟期间两个握手名额可重新占满，最终 reload 仍 `delivered`。

此外已真实启动当前代码（`python -m app.main`，测试端口 `18082/18445`）核对：

- `GET /`、`/static/app.js`、`/static/style.css` 均为 200；
- `POST /api/demo/slow-handshakes {"stack":"ipv4","count":2}` 返回两个 `holding` 客户端，卡位为 `ipv4 permit=self` 与 `ipv6 permit=borrowed by ipv4`；
- `POST /api/demo/probe {"stack":"ipv6"}` 返回 `ok=true http_status=200`，实测约 7ms；事件 seq 顺序为 `preempt_selected → abort_signal_sent → lease_actually_closed → slow_client_actually_disconnected → reclamation_complete → quota_acquired_after_reclaim → health_probe_tls_connected → quota_released_http_handoff → health_probe_result`；
- `node --check app/static/app.js` 通过；JS 引用的 33 个元素 ID 与模板中的 36 个 ID 交叉核对，无缺失；
- 用临时目录中的 jsdom 加载真实页面并对接运行中的服务：点击“发起两条慢握手”后出现借用卡位和两行 `client-on`，点击“另一入口探活”后绿色成功，恰好一行 `client-off`，事件文案齐全；随后点击原有“重载交换机配置”也成功，全程无 JS 报错。
- 另用 jsdom 回归原有四个场景按钮：`v6stall → delivered/winner=ipv4`、`disconnect → unknown`、`neither → not_sent`、`normal → delivered`，均通过且无 JS 报错。

## 未完成项 / 已知限制（如实说明）

1. **本构建沙箱没有 docker / docker compose 可执行程序，也没有 `/var/run/docker.sock`**，因此 `docker compose up --build` 未能在交付前于本机实际跑过；已做的替代验证是：compose YAML 解析通过、Dockerfile 基于官方 `python:3.12-slim` 且依赖均有 manylinux wheel、同一 `python -m app.main` 入口在宿主机 Python 3.11 下端到端实测通过。Docker 构建/启动仍需在有 Docker 的环境执行一次确认。
2. 沙箱网络下载 Chromium 仅约 17KB/s，未做成真实浏览器像素级截图核对；已用 jsdom 完成真实 DOM + 点击 + 接口的前端行为回归（无 JS 错误），外加 JS 语法检查与元素 ID 交叉核对。视觉排版未经过像素级确认。
3. Flask 使用自带开发服务器（`threaded=True`），面向单个值班员的演示/内网低频操作足够；未接 gunicorn，是为了保证“设备 + 控制台同进程、双栈端口与共用状态”只有一份。
4. 样例设备与 CA/服务器证书均为演示用途、自动生成，**不要**用于真实网络设备或生产环境。
5. 3 秒总时限以协调线程的 20ms 轮询 + socket 关闭实现，实测判定时刻约在 3.00–3.02s；非硬实时。
6. 配额状态与事件流是单进程内存实现（事件最多保留 300 条、卡位最近记录最多 40 条）；当前 Compose/入口只运行一个 Flask 进程，因此正确。若以后改多 worker/多副本，需要把共享配额迁移到外部协调服务。
7. 网页“慢握手”采用最确定的实现：本地客户端完成 TCP connect 后暂停 TLS 推进，使服务端稳定停在握手配额内；它不是逐字节慢速发送 ClientHello 的带宽模拟器。抢占保证的是状态机原子性，并有“握手晚完成也不能移交 HTTP”的测试覆盖。
