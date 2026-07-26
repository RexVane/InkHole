# 墨洞 InkHole 稳定性与速度深度诊断

**日期**: 2026-07-24
**范围**: 局域网发现/连接不稳定 · 跨网络传输不稳定且慢 · 轻量化重构路线

---

## 一、局域网「扫描不稳定、连接不稳定」的根因

### 根因 1（最主要）：探活参数对省电中的手机过于苛刻，误杀后又引发连锁重建

- 桌面端 `src/inkhole/p2p.py:81-83`：探活间隔 5s、**单地址 TCP 超时 1.5s、连续 2 轮失败即剔除**（约 10 秒）。
- Android 端 `InkHoleNode.kt:157-163`：超时更短（**1.2s**），自动发现设备同样 2 轮剔除；注释自己承认「息屏 WiFi 休眠易误判」，却只给手动设备加了容忍（4 轮），自动发现设备没加。
- 手机息屏进入 WiFi 省电模式后，RTT 尖峰到几秒非常常见 → 1.2~1.5s 超时必然间歇性失败 → 2 轮就被踢 → 设备列表忽隐忽现。**这就是「扫描设备不稳定」的直接机制。**
- 更糟的放大器：`p2p.py:2754-2757`，任何自动发现设备被剔除都会**整层拆掉重建 mDNS**（新 Zeroconf 实例）。重建期间本机从网络上消失几秒 → 对端探活本机失败 → 对端也剔除并重建 → **两台设备互相触发对方重建，来回震荡**。git 历史中「幽灵在线/设备残留」修了 5 次（v1.3.19/21/23/27/28）都是在这套机制上打补丁。

### 根因 2：无外网的局域网里会把 127.0.0.1 宣告出去

- `p2p.py:2896-2905` `_get_local_ip()` 用 `connect(("8.8.8.8", 80))` 探默认路由，**没有外网路由时返回 127.0.0.1**；`_get_local_ips()`（p2p.py:2913）把它放在地址列表第一位，mDNS 宣告（p2p.py:1298）原样带出去。
- 场景：路由器没插网线的内网、飞行模式开热点等 → 对端拿到 127.0.0.1 当首选地址 → 连接自己 → 失败。**这就是部分「明明在一个网里却连不上」的场景。**

### 根因 3：休眠唤醒靠轮询间隙推断，恢复慢

- `p2p.py:2788-2818`：每 5s 轮询，凭 sleep 间隙 >20s 判定唤醒后才重建 mDNS，没有接系统网络变化通知。唤醒后最长有一整个探测周期的「搜不到设备」窗口；期间对端还可能已把本机剔除。

### 根因 4：架构性 —— 三份手写协议栈互相漂移

- 发现+验证+传输+续传全部手写了三遍：Python（p2p.py 3215 行）、Kotlin（InkHoleNode.kt 2134 行 + WHPP/WHF1.kt）、Go（transport-core）。同一套 WHPC/WHPP 协议在三端各自演化，每修一个稳定性 bug 要同步三处，漂移即 bug。157 个提交里大量「fix(android) 同步」类提交就是证据。

### 附：局域网速率不对称（发送几百 MB/s、接收几 MB/s）

- `p2p.py:64-66`：传输块 256KB、socket 缓冲上限 4MB，叠加接收端「收→解密→落盘」单线程串行。发送速率显示的是内存写入，接收才是真实落盘速率（TRANSFER_ISSUES.md 已分析）。

---

## 二、跨网络「不稳定、慢」的根因

### 数据路径（修复前及直连失败时的 SSH 中继回退路径）

```
发送端 app → 127.0.0.1 桥（Go core）→ SSH 加密隧道 → 新加坡 VPS 反向端口
       → 对端 SSH 隧道 → 对端 Go core → 对端 app
```

### 慢的根因（按影响排序）

1. **修复前全部流量双向经过单一 VPS 中继，没有任何 P2P 打洞**；直连失败时的回退路径仍受此限制。速度上限 = min(两端到 VPS 的国际带宽)，中国大陆 ↔ 新加坡晚高峰常只有几 Mbps。当前版本已增加 STUN/QUIC 打洞，成功时不再经过 VPS 数据面。
2. **SSH channel 流控窗口 2MB（x/crypto/ssh 库内部写死，不可配）**：吞吐 ≤ 2MB/RTT。RTT 250ms 时数学上限约 8MB/s，丢包后更低。与 v1.3.24 修过的「TCP 窗口钉死」同类，但这次钉在依赖库里。
3. **每次传输连接都重做 SSH channel + Noise XX 握手 + 新建 yamux session 且只开 1 个流**（`ssh_session.go:606-639`，TRANSPORT_FIXES.md 遗留清单第 3 条也承认）。跨国 RTT 下每个文件多付 0.5~1s 启动延迟；yamux 的多路复用能力完全没用上。
4. **至少两层加密叠加**：Noise（端到端）+ SSH 隧道加密；应用层再开 WHE2 则三层。CPU 开销 ×2~3。
5. 数据泵 `bridge.go:140-145` 用默认 32KB `io.Copy`，Noise 层逐帧分配内存（`noise.go:189-208`），高吞吐时 GC 压力可观。

### 不稳定的根因

1. **对端离线时 `dialPeer` 最长阻塞 45s**（`ssh_session.go:28,645-690`）才报错，UI 层表现为长时间无响应。
2. **非 OpenChannelError 一律推倒整个 SSH 客户端重建**（`ssh_session.go:676` invalidateClient），短暂网络抖动引发全连接重建风暴（遗留清单第 1 条自己也承认）。
3. keepalive 30s/30s（`ssh_session.go:26-27`）对跨国链路偏保守，闲置连接易被中间设备静默掐断后要等两个周期才发现。
4. 超时全部硬编码，无法按网络环境调整（遗留清单第 2 条）。

---

## 三、改进方案

### 第一批：止血修复（本次直接实施，不改架构）

| # | 修复 | 位置 | 预期效果 |
|---|------|------|----------|
| 1 | 探活超时 1.5s→3s、strikes 2→4；Android 同步（1.2s→3s、2→4） | p2p.py:81-83, InkHoleNode.kt:157-163 | 息屏手机不再被误杀，设备列表稳定 |
| 2 | 删除「设备离线→整层重建 mDNS」的连锁 | p2p.py:2754-2757 | 消灭互相触发的重建震荡 |
| 3 | 宣告地址过滤 127.0.0.1 | p2p.py:2913 | 无外网局域网可正常互连 |
| 4 | 传输块 256KB→1MB、socket 缓冲 4→16MB | p2p.py:64-66 | 千兆局域网吞吐明显提升 |
| 5 | SSH 数据通道 yamux session 按对端复用 | ssh_session.go | 消掉每次传输的握手延迟；减少重连风暴 |

### 第二批：架构级（建议下一步，需要拍板）

1. **跨网提速的根本解：P2P 打洞，VPS 退居信令+兜底**。✅ **已实施（2026-07-24）**：SSH 中继模式现在会在首次中继传输后自动尝试 QUIC 直连——两端通过中继上的加密信令流（IKQ1）交换 STUN 探测的公网 UDP 端点与自签证书指纹，同时互发包打洞，`instanceID` 大的一方发起 QUIC 握手（TLS 1.3 双向指纹校验）。打通后传输走直连、速度只受两端宽带限制；打不通冷却 5 分钟内继续走中继，行为不变。旧版对端自动回退，无协议破坏。实现见 `transport-core/core/quic_direct.go`。
2. **统一核心，消灭三份协议栈**：发现（mDNS+UDP 广播）、WHPC/WHPP/WHF1、续传、加密全部下沉到 Go `transport-core`（桌面 sidecar 已存在、Android gomobile AAR 已存在——路已铺好，只是局域网层还没搬进去）。三端 UI 只做壳。这是根治「修一处漂移两处」的唯一办法。
3. **轻量化桌面端**：现状 PySide6+PyInstaller 打包 100MB+。核心下沉 Go 后，UI 可换 Wails（Go+系统 WebView，10~20MB）或保留极薄的 Python 壳。「轻量」诉求主要靠这一步。

### 语言选型建议

**不建议整体换 Rust/重写**：transport-core 的 Go 底子（Noise、yamux、wormhole、gomobile）已经验证可用，Go 单二进制天然轻量跨平台，且是三端唯一已共享的层。**建议方向 = 「Go 核心扩容 + UI 减薄」**，而不是换语言重来。

---

## 四、2026-07-25 统一核心深度审查修复记录

对新 LAN 栈（`core/lan/` + `lan_service.go`）与 Wails 桌面壳（`desktop/`）的深度审查发现并修复：

| # | 问题 | 位置 | 修复 |
|---|------|------|------|
| P0-1 | acceptLoop 清 deadline 后读 IKCI token / WHPC nonce，半开连接永久挂 goroutine；Close 的 `wg.Wait` 只关 listener 不关连接 → `lan.stop` / desktop `Stop()` 挂死 | lan_service.go, desktop/inkhole.go | 握手 deadline 保持到分发完成；跟踪活动连接，Close 时统一关闭。回归测试 `TestLANStopWithStalledHandshakes` 锁死该行为 |
| P0-2 | desktop 手动设备探测单轮失败即下线（Android 端有 `PROBE_STRIKES_MANUAL=4`，新壳丢了）| desktop/inkhole.go | `manualProbeStrikes=4`，失败期间保留上次运行时条目 |
| P0-3 | 事件通道满 128 条静默丢弃，会丢 `lan.sent`/`wormhole.ready` 等一次性关键事件 | core/service.go | 分级：`lan.progress/status/peers` 快照可丢，其余阻塞送达（服务关闭兜底）|
| P1-5 | 伪造 UDP announcement 可无限触发 3s 超时的验证 goroutine（DoS）| core/lan/discovery.go | 验证并发信号量（8），超限丢弃待下轮 |
| P1-6 | 同一设备经 mDNS 与广播两个 key 重复进设备列表 | core/lan/discovery.go | 按 InstanceID 合并地址进已有条目 |
| P1-7 | yamux 断开后 acceptLocal 静默退出，悬空 listener 吞连接 | core/bridge.go | 失败即 cancel 整个 bridge |
| P1-8 | `Start` 后台 startSSH 与 SaveSSHConfig 的 restartSSH 并发调 `ssh.listen`，泄漏会话 | desktop/transport.go | `sshMu` 串行化 |
| 去重 | desktop 壳复制了一份 acceptLoop 分发逻辑，与 lan_service.go 已出现行为分歧 | core/lan/inbound.go（新）| 下沉为 `lan.HandleInbound`，两壳共用 |

**后续协议演进**：WHE4 已在三端实施（2026-07-25）——口令经 600k PBKDF2 对固定应用盐派生主密钥并缓存（每进程每口令一次），每流经 HKDF-SHA256(master, salt=流盐, info) 派生流密钥；通过签名的 WHPC `whe4` capability 协商，只有接收端明确声明能力时发送端才使用 WHE4，旧端自动回退 WHE3，无协议破坏。Go（whe.go + lan_service/desktop 接线）、Python（crypto.py/p2p.py）、Kotlin（Crypto.kt/WHPP.kt/InkHoleNode.kt）均已声明能力并支持收发，三端共用同一 known-answer 向量锁定字节兼容（whe_test.go / tests/test_whe4.py / CryptoTest.kt）。仍遗留：WHE1 小文件路径保留 100k PBKDF2，仅用于历史跨端兼容且 Go 生产路径不调用；SendFolder 因 header 需预知整流 SHA-256 仍需两遍读盘，留待下一版分块哈希协议处理。

验证：`go vet` + `go test ./core/... -race` 全过（含新回归测试与 WHE4 KAT）、desktop `go build` 通过、Python 套件 132 passed、Android `testDebugUnitTest` 全过。

**2026-07-26 复查收尾**（跨网 WHE4 + 缓存加固）：

| # | 问题 | 位置 | 修复 |
|---|------|------|------|
| F-1 | SSH / 一次性短码端点能力写死 `folder-v1`，跨网传输永远回退 WHE3；Python 外部端点探测不带 IKAT 令牌被桥接拒绝，文件夹只能走 ZIP 兜底 | desktop/inkhole.go, src/inkhole/p2p.py, InkHoleNode.kt | 三端 WHPC 探测支持端点令牌（Go `lan.ProbeEndpoint`、Python `_probe_peer(endpoint_token=)`、Kotlin `probePeer(endpointToken=)`），发送前透过桥接探测一次真实能力再决定 WHE4，同时钉住对端身份指纹；旧对端探测失败安全回退 WHE3。desktop 的 Tailscale 手动设备也把探测到的能力带进 `PeerView.Capabilities` 参与 WHE4 协商。测试 `TestProbeEndpointNegotiatesWHE4ThroughBridgeAuth` 覆盖令牌鉴权与能力透传 |
| F-2 | Python/Kotlin WHE4 主密钥缓存用明文口令做键，换掉的旧口令长期留存进程内存 | src/inkhole/crypto.py, Crypto.kt | 与 Go masterCache 同设计：进程随机 HMAC-SHA256 摘要作缓存键，明文口令不再作为键留存（tests/test_whe4.py 断言缓存键非明文）|
| F-3 | 桌宠恢复位置时未吸附坐标不做越界校正，外接屏拔掉后桌宠停在屏幕外（如 x=2721 vs 屏宽 1440）| desktop/frontend/src/pet.ts | 恢复后按当前屏幕工作区 clamp（含 edge=-1），并修复旧版错误保存的边缘状态；桌宠拖动/吸附已改为 macOS 原生窗口拖动 + Go 端动画 |
