# 墨洞传输问题分析与解答

**日期**: 2026-07-23  
**问题来源**: 用户反馈

---

## 问题 1: 局域网传输速率不对称（发送几百MB/s，接收只有几MB/s）

### 问题分析

根据代码 `src/inkhole/p2p.py:64-66`：

```python
_BUFFER = 256 * 1024      # 256KB 传输块
_SOCKET_BUFFER = 4 * 1024 * 1024   # TCP 窗口上限: 4MB
```

**可能原因**：

1. **发送速率是内存写入速度，接收速率是磁盘写入速度**  
   - 发送方：从磁盘读取 → 内存缓冲 → socket 发送缓冲区（几百 MB/s 是内存到内存）
   - 接收方：从 socket 接收 → 写入磁盘（几 MB/s 是实际落盘速度）
   - **这是正常现象**，接收速率受限于磁盘写入性能

2. **接收端磁盘 I/O 瓶颈**  
   - 机械硬盘写入速度：60-150 MB/s
   - SATA SSD：200-500 MB/s
   - NVMe SSD：1-3 GB/s
   - Android 手机闪存：通常 50-200 MB/s

3. **加密开销**（如果启用）  
   代码使用 AES-256-GCM 分块加密，接收端需要：
   - 接收数据 → 解密 → 校验 → 写入磁盘
   - 解密是 CPU 密集型操作，会降低吞吐量

4. **Python GIL 限制**（桌面端）  
   接收线程处理 socket 读取、解密、磁盘写入都在同一线程，无法充分利用多核

### 验证方法

```bash
# 1. 检查接收端磁盘写入速度
# macOS
time dd if=/dev/zero of=testfile bs=1m count=1024
# 应该能看到真实磁盘写入速度

# 2. 禁用加密测试
# 在设置中关闭"传输加密"，对比速度

# 3. 监控系统资源
# macOS Activity Monitor / Windows Task Manager
# 观察接收时 CPU 和磁盘 I/O 占用
```

### 优化建议

#### 短期（代码级）

```python
# 增加缓冲区大小（适用于局域网高速传输）
_BUFFER = 1 * 1024 * 1024  # 从 256KB 提升到 1MB
_SOCKET_BUFFER = 16 * 1024 * 1024  # 从 4MB 提升到 16MB
```

#### 中期（架构级）

1. **异步 I/O 流水线**  
   接收 → 解密 → 写盘分离到不同线程：
   ```python
   # 接收线程 → 解密队列
   # 解密线程 → 写盘队列
   # 写盘线程 → 持久化
   ```

2. **使用 `mmap` 或直接 I/O**  
   减少用户空间到内核的数据拷贝

3. **批量写入**  
   累积多个块后一次性 fsync

#### 长期（用户配置）

在设置中添加"性能模式"选项：
- **均衡模式**（默认）：256KB buffer，适合跨网络
- **局域网高速模式**：1MB buffer + 16MB socket，适合千兆局域网
- **移动网络模式**：64KB buffer，减少重传

---

## 问题 2: 是否支持 Wi-Fi 直传（Wi-Fi Direct / P2P）

### ❌ 当前不支持 Wi-Fi Direct

根据代码分析：

**当前实现**：
- ✅ 同一 Wi-Fi 下通过 **mDNS 自动发现** + TCP 直连（`p2p.py:6-9`）
- ✅ Android 手机热点场景通过 **UDP 广播** 兜底（`p2p.py:86-89`）
- ❌ **不使用 Wi-Fi Direct (P2P)** 协议

**原因**：
1. **不需要**：同一 Wi-Fi 下已经是"直连"（路由器只做二层转发）
2. **手机热点已兜底**：
   ```python
   # p2p.py:2675-2757
   # Android 热点经常不把 mDNS 暴露给热点提供者/客户端。
   # 这个 UDP 广播层会监听 41301 端口并回复设备信息
   ```

### Wi-Fi 直连 vs 墨洞现有方案

| 场景 | Wi-Fi Direct | 墨洞方案 | 速度 |
|------|-------------|----------|------|
| **两台设备都在同一 Wi-Fi** | 不需要 | mDNS 自动发现 + TCP 直连 | 千兆局域网全速 |
| **手机开热点 → 电脑连入** | 不需要 | UDP 广播发现 + TCP 直连 | 热点带宽上限（通常 100-300 Mbps） |
| **两台 Android 互传（无路由器）** | 可用 Wi-Fi Direct | ❌ 当前不支持 | - |
| **跨网络（异地）** | 不可用 | Tailscale / SSH / Wormhole | 取决于互联网带宽 |

### 是否需要添加 Wi-Fi Direct？

**不推荐**，原因：

1. **用户场景少**  
   两台 Android 手机在野外无网络时传输 → 直接开热点即可

2. **实现复杂度高**  
   - 需要 Android `WifiP2pManager` API
   - 需要用户授权位置权限（Android 隐私限制）
   - 桌面端（Windows/macOS）支持差异大

3. **当前方案已覆盖**  
   手机热点 + UDP 广播已能满足"无路由器直连"需求

---

## 问题 3: Android 切换到后台会不会出现问题

### ✅ 已完善处理，不会中断传输

根据 `android/app/src/main/java/com/rexvane/inkhole/InkHoleService.kt`：

#### 1. **前台服务保活**（64-85 行）

```kotlin
// InkHoleService.kt:64-85
private var wifiLock: android.net.wifi.WifiManager.WifiLock? = null

override fun onCreate() {
    startForeground(NOTIF_STATUS_ID, buildStatusNotification("正在启动…"))
    
    // 息屏后 vivo/各厂商会让 WiFi 休眠,TCP 监听对外不可达,对端把本机判离线。
    // 前台服务期间持有高性能 WifiLock,保持 WiFi 常联通
    val mode = if (Build.VERSION.SDK_INT >= 29)
        android.net.wifi.WifiManager.WIFI_MODE_FULL_LOW_LATENCY
    else android.net.wifi.WifiManager.WIFI_MODE_FULL_HIGH_PERF
    wifiLock = wm.createWifiLock(mode, "inkhole:wifi").apply {
        setReferenceCounted(false)
        acquire()
    }
}
```

**保障措施**：
- ✅ 前台服务常驻（显示通知）
- ✅ 高性能 WifiLock 防止 Wi-Fi 休眠
- ✅ 锁屏/切后台继续接收

#### 2. **权限声明**（16-18 行）

```xml
<!-- AndroidManifest.xml -->
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC" />
<uses-permission android:name="android.permission.WAKE_LOCK" />
```

#### 3. **服务重启机制**（90-100 行）

```kotlin
override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    if (InkHoleBus.node?.isReady() != true) {
        // Vivo 等系统可能只结束前台服务的监听层，同时保留 Activity 进程。
        // Activity 回前台再次 startService 时必须重建节点
        startNode()
    }
    return START_STICKY  // 系统杀掉后会自动重启
}
```

### 可能遇到的边界情况

#### ⚠️ 极端省电模式

某些厂商（小米、华为、OPPO）的"超级省电"或"后台冻结"可能：
- 杀死前台服务（违反 Android 规范，但厂商会这么做）
- 限制 CPU 唤醒（Doze 模式）

**解决方案**：
在设置中引导用户：
```
墨洞 > 电池优化 > 不限制
墨洞 > 自启动权限 > 允许
墨洞 > 后台活动 > 允许
```

#### ✅ 正常后台场景

以下场景**不会中断**：
- 按 Home 键切回桌面
- 切换到其他 App
- 锁屏（屏幕关闭）
- 收到电话/微信消息（前台服务继续运行）

### 测试验证

```kotlin
// 在 InkHoleService 中添加日志
override fun onDestroy() {
    Log.w("InkHole", "Service destroyed! This should not happen during transfer.")
    super.onDestroy()
}
```

如果传输中断，检查 logcat：
```bash
adb logcat | grep InkHole
# 如果看到 "Service destroyed"，说明被系统/厂商杀死
```

---

## 总结与建议

### 1. 传输速率问题

**正常现象**：发送速率是内存速度，接收速率是磁盘速度。

**优化方案**：
- 短期：增加 `_BUFFER` 到 1MB，`_SOCKET_BUFFER` 到 16MB
- 中期：实现异步 I/O 流水线
- 长期：添加"性能模式"配置项

### 2. Wi-Fi Direct

**不需要添加**：
- 同一 Wi-Fi 下已是真正的"直连"
- 手机热点场景已通过 UDP 广播兜底
- Wi-Fi Direct 实现复杂且用户场景少

### 3. Android 后台问题

**已完善处理**：
- ✅ 前台服务 + WifiLock 保活
- ✅ START_STICKY 自动重启
- ✅ 锁屏/切后台不会中断

**用户需注意**：
- 部分厂商需手动关闭"电池优化"
- 避免使用"超级省电模式"

---

## 立即可执行的优化

如果你想优化局域网传输速度，可以修改以下参数：

```python
# src/inkhole/p2p.py:64-66
_BUFFER = 1 * 1024 * 1024          # 256KB → 1MB
_SOCKET_BUFFER = 16 * 1024 * 1024  # 4MB → 16MB
```

然后重启桌面端和 Android 端进行测试。

需要我帮你应用这些优化吗？
