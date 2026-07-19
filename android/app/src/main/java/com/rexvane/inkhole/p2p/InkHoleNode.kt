package com.rexvane.inkhole.p2p

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import java.io.*
import java.net.InetAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.Semaphore
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/** 检测到设备/收到文件/状态变化时的回调。 */
interface InkHoleListener {
    fun onPeerChanged(peers: List<Peer>)
    fun onFileReceived(filename: String, path: String)
    fun onStatus(msg: String)
    /** 传输进度。kind = "send"/"recv"；节流后最多约 4 次/秒。 */
    fun onProgress(kind: String, filename: String, done: Long, total: Long) {}
    /** 成功、失败、断网或取消都会触发；UI 用它可靠清除卡住的进度环。 */
    fun onTransferEnded(kind: String, filename: String, completed: Boolean) {}
    /** Activity 重建后也能恢复发送按钮/取消按钮状态。 */
    fun onSendingChanged(active: Boolean) {}
}

data class Peer(
    val name: String,           // 显示名(重名设备带 " (2)" 后缀)
    val host: String,
    val port: Int,
    val serviceName: String = "",  // NSD 服务实例名(唯一，用于离线精确匹配)
    // 对端全部已知地址：桌面多网卡/VPN 场景发出连接的源 IP 可能不是解析到的
    // 那一个,「仅接收目标设备」按整个列表放行(来自 TXT ips / API34 hostAddresses)
    val hosts: List<String> = listOf(host),
    val instanceId: String = "",
    val capabilities: Set<String> = emptySet(),
    val manual: Boolean = false,
)

private data class PeerProbeResult(
    val instanceId: String,
    val peerName: String,
    val capabilities: Set<String>,
    val connectedAddress: String,
)

private class IdentityMismatchException(message: String) : IOException(message)
private class TailnetUnavailableException(message: String) : IOException(message)

private data class ProbeOutcome(
    val key: String,
    val manual: Boolean,
    val result: PeerProbeResult? = null,
    val identityError: String? = null,
    val tailnetUnavailable: Boolean = false,
)

private data class ManualProbeOutcome(
    val peer: ManualPeer,
    val result: PeerProbeResult? = null,
    val identityError: String? = null,
)

/**
 * 墨洞 P2P 引擎 (Android 版)。
 *
 * - NSD (NsdManager) 注册/发现 _inkhole._tcp 服务, 与桌面版 zeroconf 互通。
 *   服务名带唯一实例 ID 后缀(同名设备不冲突)，显示名走 TXT 属性 peer_name。
 * - TCP ServerSocket 接收文件, WHPP 协议与桌面版一致(含 ACK 回执)。
 * - 可选 AES-256-GCM 端到端加密 (与桌面版 crypto.py 兼容)。
 */
class InkHoleNode(
    private val context: Context,
    private val peerName: String,
    private val inboxDir: File,
    private val secret: String = "",
    private val trustedOnly: Boolean = false,   // true = 只接受当前选中目标设备的连接
    private val listenPort: Int = 0,            // 固定监听端口;0 = 系统自动分配(跨网手动直连需固定)
    private val listener: InkHoleListener,
) {
    companion object {
        private const val SERVICE_TYPE = "_inkhole._tcp."
        private const val DISK_MARGIN = 256L * 1024 * 1024   // 收完至少还要剩这么多
        private const val PROGRESS_INTERVAL_MS = 250L
        private const val CHUNK_ENC_THRESHOLD = 32L * 1024 * 1024  // 超过走 WHE2 分块
        private const val MAX_WHE1_SIZE = 64L * 1024 * 1024
        private const val HEADER_TIMEOUT_MS = 15_000
        private const val RECV_IDLE_TIMEOUT_MS = 300_000
        private const val DRAIN_TIMEOUT_MS = 2_000
        private const val MAX_INCOMING_CONNECTIONS = 4
        private const val DRAIN_CAP = 8L * 1024 * 1024       // 拒收时最多帮对端消化的字节数
        // TCP 收发缓冲(4MB):决定窗口上限,必须在 bind/connect 之前设置——
        // 窗口缩放因子在握手时协商,连接建立后再放大不生效,且显式设置会
        // 禁用内核自动调优,设晚了反而把窗口钉死在小值
        private const val SOCKET_BUFFER = 4 * 1024 * 1024
        // onServiceLost 误报兜底：探活参数(连续失败才真移除)
        private const val LOST_PROBE_TIMEOUT_MS = 1200        // 单次 TCP 探活超时
        private const val LOST_PROBE_ATTEMPTS = 3             // 连续失败几次才判定真离线
        private const val LOST_PROBE_INTERVAL_MS = 1000L      // 两次探活间隔
        // 全量存活探活：定期 TCP 探测所有对端(含自动发现),本机断网/对端崩溃时清残留
        private const val PROBE_INTERVAL_MS = 5_000L          // 探活轮询间隔
        private const val PROBE_STRIKES = 2                   // 自动发现设备连续失败几轮剔除
        private const val PROBE_STRIKES_MANUAL = 4            // 手动设备双倍容忍(息屏 WiFi 休眠易误判)
        // 手动设备探活超时:Tailscale 空闲后懒惰唤醒(打洞/DERP 建链)首次
        // 握手常超 1.2s,太紧会把在线的跨网设备判死或迟迟不上线
        private const val PROBE_TIMEOUT_MANUAL_MS = 3_000
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var nsdManager: NsdManager? = null
    private var serverSocket: ServerSocket? = null
    private var tcpStartError: String? = null
    private val activeSockets = ConcurrentHashMap.newKeySet<Socket>()
    private val activeSendSocket = AtomicReference<Socket?>(null)
    private val activeSendInput = AtomicReference<InputStream?>(null)
    private val sendInProgress = AtomicBoolean(false)
    private val sendCancelled = AtomicBoolean(false)
    private val incomingSlots = Semaphore(MAX_INCOMING_CONNECTIONS)
    private var actualPort = 0
    // 唯一实例 ID 持久化到 SharedPreferences：同一台设备无论 App/前台服务重启
    // 多少次都用同一个服务名，避免旧注册变成永不消失的"幽灵设备"。
    private val instanceId = loadOrCreateInstanceId(context)
    private val advertisedPeerName = ReceiveFiles.utf8Prefix(peerName, 200)
        .ifBlank { "Android" }
    private val requestedServiceName =
        "${ReceiveFiles.utf8Prefix(advertisedPeerName.replace(".", "-"), 40)}-${instanceId.take(8)}"

    private fun loadOrCreateInstanceId(ctx: Context): String {
        val prefs = ctx.getSharedPreferences("inkhole", Context.MODE_PRIVATE)
        prefs.getString("instance_id", null)
            ?.takeIf { it.matches(Regex("[0-9a-fA-F]{32}")) }
            ?.let { return it.lowercase() }
        val id = UUID.randomUUID().toString().replace("-", "")
        prefs.edit().putString("instance_id", id).apply()
        return id
    }

    // serviceName(唯一) -> Peer
    private val peers = LinkedHashMap<String, Peer>()
    private val peersLock = Any()
    private val receiveFileLock = Any()
    // onServiceLost 误报兜底：正在 TCP 探活确认的 serviceName，避免并发重复探活
    private val probingLost = java.util.Collections.synchronizedSet(HashSet<String>())
    @Volatile private var selectedPeer: String? = null   // 显示名
    @Volatile private var lastSelectedService: String? = null  // 智能保留：记住选中设备的 serviceName
    @Volatile private var running = false
    /** 系统实际注册下来的服务名(冲突时可能被系统改名)，用于"不发现自己"。 */
    @Volatile private var registeredName: String? = null

    // 手动添加的设备(跨网/固定 IP 直连):没有 NSD 通告,靠探活循环维持状态
    private val manualPeers =
        ManualPeers.load(context.getSharedPreferences("inkhole", Context.MODE_PRIVATE)).toMutableList()
    private val manualPeersLock = Any()
    private val identityErrors = java.util.Collections.synchronizedSet(HashSet<String>())

    // ---- 生命周期 ----

    fun start() {
        if (running) return
        running = true
        inboxDir.mkdirs()
        val tcpStarted = startTcpServer()
        if (!tcpStarted) {
            running = false
            listener.onPeerChanged(emptyList())
            val detail = tcpStartError?.takeIf { it.isNotBlank() }?.let { ": $it" }.orEmpty()
            listener.onStatus(if (listenPort != 0) {
                "墨洞未开启：固定监听端口 $listenPort 不可用$detail"
            } else {
                "墨洞未开启：监听端口启动失败$detail"
            })
            return
        }
        try {
            nsdManager = context.getSystemService(Context.NSD_SERVICE) as NsdManager
        } catch (e: Exception) {
            listener.onStatus("NSD 初始化失败: ${e.message}")
        }
        if (actualPort > 0) {
            try {
                registerNsd()
            } catch (e: Exception) {
                listener.onStatus("NSD 注册失败: ${e.message}")
            }
        }
        try {
            discoverNsd()
        } catch (e: Exception) {
            listener.onStatus("NSD 发现启动失败: ${e.message}")
        }
        // 手动设备不再启动即乐观入列:前台服务被厂商省电反复杀死重启,每次
        // 重启都会把早已关机的对端"复活"成假在线。改由探活循环首轮(立即执行)
        // 验证,连得上才显示——列表语义收紧为"当前真实在线的设备"。
        startProbeLoop()  // 全量存活探活:定期 TCP 探测所有对端(含自动发现),清理断网/崩溃残留
        // 发现自愈:NSD 发现流偶尔"卡死"(WiFi 省电丢组播/系统服务抽风),
        // 表现为对端明明在线列表却空着。列表持续为空时周期性重启发现。
        scope.launch {
            while (running) {
                delay(30_000)
                val hasDiscoveredPeer = synchronized(peersLock) {
                    peers.keys.any { !it.startsWith("manual|") }
                }
                if (!hasDiscoveredPeer && running) restartDiscovery()
            }
        }
        // 把当前列表主动推给 UI:服务被杀重启后 Activity 可能还挂着旧节点的
        // 设备列表,周围没有设备时不会再有 onPeerChanged 事件来纠正它。
        listener.onPeerChanged(getPeers())
        listener.onStatus("墨洞已开启 · $peerName")
    }

    /** 重启 NSD 发现(手动刷新按钮/自愈循环用)。stop 是异步的,稍等再启。 */
    fun restartDiscovery() {
        val nsd = nsdManager ?: return
        if (!running || !discoveryRestartPending.compareAndSet(false, true)) return
        try { discoveryListener?.let { nsd.stopServiceDiscovery(it) } } catch (_: Exception) {}
        scope.launch {
            try {
                delay(400)
                if (running) try {
                    discoverNsd()
                } catch (e: Exception) {
                    listener.onStatus("NSD 发现启动失败: ${e.message}")
                }
            } finally {
                discoveryRestartPending.set(false)
            }
        }
    }

    private fun bindManualIdentity(m: ManualPeer, result: PeerProbeResult): ManualPeer? {
        m.instanceId?.let { expected ->
            if (expected != result.instanceId) {
                throw IdentityMismatchException("设备身份已变化，请删除后重新添加")
            }
            return m
        }
        val bound = ManualPeers.pinIdentity(
            context.getSharedPreferences("inkhole", Context.MODE_PRIVATE),
            m,
            result.instanceId,
        ) ?: return null
        if (bound.instanceId != result.instanceId) {
            throw IdentityMismatchException("设备身份已变化，请删除后重新添加")
        }
        synchronized(manualPeersLock) {
            val index = manualPeers.indexOfFirst { it.key == m.key }
            if (index >= 0) {
                manualPeers[index] = bound
            }
        }
        return bound
    }

    private fun registerManual(m: ManualPeer, result: PeerProbeResult) {
        val addresses = resolveHostAddresses(m.host).toMutableList()
        if (result.connectedAddress !in addresses) addresses.add(0, result.connectedAddress)
        addPeer(
            m.key, m.name.ifEmpty { result.peerName }, result.connectedAddress, m.port,
            addresses, result.instanceId, result.capabilities, manual = true,
        )
    }

    /** 全量存活探活:定期 TCP 探测所有对端(含自动发现),清理断网/崩溃残留。
     *  首轮立即执行:手动设备的"验证后上线"也靠它,启动后 ~1s 内在线的手动设备就会出现。
     *  自动发现的设备:连续失败 PROBE_STRIKES 轮剔除(~10s),误移除后自动重启发现让 NSD 重新找回。
     *  手动设备:双倍容忍度(息屏 WiFi 休眠易误判);若不在列表但探活成功则自动加回(回线恢复)。 */
    private fun startProbeLoop() {
        scope.launch {
            val strikes = HashMap<String, Int>()
            while (running) {
                // 获取当前所有已知对端的探活目标(key、hosts、port)
                val targets = synchronized(peersLock) { peers.toList() }
                val manualSnapshot = synchronized(manualPeersLock) { manualPeers.toList() }
                val manualByKey = manualSnapshot.associateBy { it.key }
                val lanLinks = currentLanLinks()
                // 清理已不在列表的对端的失败计数
                strikes.keys.retainAll(targets.map { it.first }.toSet())

                // 设备级并行探活:串行时一台离线设备就按地址数×1.2s 拖慢一整轮,
                // 多台离线时状态清理以分钟计,亮屏后旧设备迟迟不消失
                val probed = targets.map { (key, peer) ->
                    async {
                        val manualPeer = manualByKey[key]
                        val isManual = manualPeer != null
                        val probeHosts = if (manualPeer != null) {
                            (listOf(manualPeer.host, peer.host) + peer.hosts).distinct()
                        } else {
                            LanReachability.hostsOnCurrentLan(peer.hosts, lanLinks)
                        }
                        val timeout = if (isManual) PROBE_TIMEOUT_MANUAL_MS
                            else LOST_PROBE_TIMEOUT_MS
                        val expected = manualPeer?.instanceId ?: peer.instanceId
                        try {
                            ProbeOutcome(key, isManual,
                                result = probePeer(probeHosts, peer.port, timeout, expected))
                        } catch (e: IdentityMismatchException) {
                            ProbeOutcome(key, isManual, identityError = e.message)
                        } catch (_: TailnetUnavailableException) {
                            ProbeOutcome(key, isManual, tailnetUnavailable = true)
                        } catch (_: Exception) {
                            ProbeOutcome(key, isManual)
                        }
                    }
                }.awaitAll()

                var autoRemovedThisRound = false
                for (outcome in probed) {
                    if (!running) break
                    val key = outcome.key
                    val isManual = outcome.manual
                    val present = synchronized(peersLock) { peers.containsKey(key) }
                    if (!present) continue  // 已被其他逻辑移除,跳过
                    if (outcome.identityError != null) {
                        strikes.remove(key)
                        removePeer(key)
                        if (identityErrors.add(key)) {
                            listener.onStatus("设备身份验证失败: ${outcome.identityError}")
                        }
                        continue
                    }
                    val threshold = when {
                        isManual && outcome.tailnetUnavailable -> 1
                        isManual -> PROBE_STRIKES_MANUAL
                        else -> PROBE_STRIKES
                    }

                    if (outcome.result == null) {
                        val s = (strikes[key] ?: 0) + 1
                        if (s >= threshold) {
                            strikes.remove(key)
                            removePeer(key)
                            if (!isManual) autoRemovedThisRound = true
                        } else {
                            strikes[key] = s
                        }
                    } else {
                        strikes.remove(key)
                        identityErrors.remove(key)
                        synchronized(peersLock) {
                            peers[key]?.let { current ->
                                val result = outcome.result ?: return@let
                                val addresses = if (isManual) {
                                    val configured = manualByKey[key]
                                    configured?.let {
                                        resolveHostAddresses(it.host).toMutableList().apply {
                                            if (result.connectedAddress !in this) {
                                                add(0, result.connectedAddress)
                                            }
                                        }
                                    }
                                        ?: current.hosts
                                } else current.hosts
                                peers[key] = current.copy(
                                    host = result.connectedAddress,
                                    hosts = addresses.distinct(),
                                    instanceId = result.instanceId,
                                    capabilities = result.capabilities,
                                )
                            }
                        }
                    }
                }

                // 自动发现设备被移除后,重启发现让 NSD 重新找回(Android NSD 不会自动重触发 onServiceFound)
                if (autoRemovedThisRound) {
                    restartDiscovery()
                }

                // 手动设备兜底:不在列表但能连上 → 加回(启动首轮的"验证后上线"同样走这里)
                manualSnapshot.filter { m ->
                    synchronized(peersLock) { !peers.containsKey(m.key) }
                }.map { m ->
                    async {
                        try {
                            ManualProbeOutcome(
                                m, result = probePeer(
                                    listOf(m.host), m.port,
                                    PROBE_TIMEOUT_MANUAL_MS, m.instanceId ?: ""))
                        } catch (e: IdentityMismatchException) {
                            ManualProbeOutcome(m, identityError = e.message)
                        } catch (_: Exception) {
                            ManualProbeOutcome(m)
                        }
                    }
                }.awaitAll().forEach { outcome ->
                    if (!running) return@forEach
                    val m = outcome.peer
                    if (outcome.identityError != null) {
                        if (identityErrors.add(m.key)) {
                            listener.onStatus(
                                "${m.name.ifEmpty { m.host }} 身份验证失败: ${outcome.identityError}")
                        }
                        return@forEach
                    }
                    val result = outcome.result ?: return@forEach
                    try {
                        val bound = bindManualIdentity(m, result) ?: return@forEach
                        strikes.remove(m.key)
                        identityErrors.remove(m.key)
                        registerManual(bound, result)
                    } catch (e: IdentityMismatchException) {
                        if (identityErrors.add(m.key)) {
                            listener.onStatus(
                                "${m.name.ifEmpty { m.host }} 身份验证失败: ${e.message}")
                        }
                    }
                }

                // 间隔期可被 probeNow() 提前唤醒:回前台立即刷新在线状态
                withTimeoutOrNull(PROBE_INTERVAL_MS) { probeKick.receive() }
            }
        }
    }

    // 回前台立即探活的信号;CONFLATED 让连续多次触发合并成一轮
    private val probeKick = Channel<Unit>(Channel.CONFLATED)

    /** 立即开始一轮全量探活。息屏期间探活循环随进程冻结,下线设备的剔除
     *  不会推进;Activity 回前台时踢一脚,不用干等下一个轮询周期。 */
    fun probeNow() {
        probeKick.trySend(Unit)
    }

    fun stop() {
        running = false
        cancelSend()
        // 注册/发现可能从未成功，注销时系统会抛 IllegalArgumentException——不能让退出流程崩掉
        nsdManager?.let { nsd ->
            try { discoveryListener?.let { nsd.stopServiceDiscovery(it) } } catch (_: Exception) {}
            try { registrationListener?.let { nsd.unregisterService(it) } } catch (_: Exception) {}
        }
        try { serverSocket?.close() } catch (_: IOException) {}
        serverSocket = null
        activeSockets.forEach { socket ->
            try { socket.close() } catch (_: IOException) {}
        }
        activeSockets.clear()
        actualPort = 0
        resolveQueue.clear()
        resolving.set(false)
        scope.cancel()
        synchronized(peersLock) { peers.clear() }
        listener.onPeerChanged(emptyList())
    }

    // ---- TCP 服务器 ----

    /** 绑定监听端口。必须开 SO_REUSEADDR:设置保存会重启节点,旧连接的
     *  TIME_WAIT 会让立刻重绑同端口失败——不开的话固定端口会"莫名变随机"。
     *  接收缓冲必须在 bind 前设在 ServerSocket 上:accept 出的连接继承它,
     *  并以它协商窗口缩放,大文件接收吞吐的天花板在这里定下。 */
    private fun bindServer(port: Int): ServerSocket =
        ServerSocket().apply {
            reuseAddress = true
            try { receiveBufferSize = SOCKET_BUFFER } catch (_: Exception) {}
            bind(java.net.InetSocketAddress(port.coerceIn(0, 65535)))
        }

    private fun startTcpServer(): Boolean {
        tcpStartError = null
        val server = try {
            bindServer(listenPort).also { actualPort = it.localPort }
        } catch (e: IOException) {
            actualPort = 0
            tcpStartError = e.message
            return false
        }
        serverSocket = server
        scope.launch {
            while (running) {
                try {
                    val conn = server.accept()
                    if (!incomingSlots.tryAcquire()) {
                        try { conn.close() } catch (_: IOException) {}
                        continue
                    }
                    activeSockets.add(conn)
                    if (!running) {
                        activeSockets.remove(conn)
                        incomingSlots.release()
                        try { conn.close() } catch (_: IOException) {}
                        break
                    }
                    launch {
                        try {
                            handleConnection(conn)
                        } finally {
                            activeSockets.remove(conn)
                            incomingSlots.release()
                        }
                    }
                } catch (e: IOException) {
                    if (running) listener.onStatus("接收连接失败: ${e.message}")
                    break
                }
            }
        }
        return true
    }

    // 接收是对端发起的，不主动清理系统缓存；只按当前真实可用空间保守判断。
    @android.annotation.SuppressLint("UsableSpace")
    private fun handleConnection(conn: Socket) {
        var partFile: File? = null
        var folderPart: File? = null
        var wantAck = false
        var ok = false
        var headerRead = false
        var receivedName = ""
        try {
            conn.soTimeout = HEADER_TIMEOUT_MS
            val input = BufferedInputStream(conn.getInputStream(), WHPP.BUFFER_SIZE)
            // 存活探测空连接在 readMagic 处 EOF；WHPC 能力请求不进入传输流程。
            val magic = WHPP.readMagic(input)
            if (magic.contentEquals(WHPP.CAP_MAGIC)) {
                WHPP.writeCapabilities(conn.getOutputStream(), instanceId, advertisedPeerName)
                return
            }
            if (!magic.contentEquals(WHPP.MAGIC)) return
            val header = WHPP.readHeaderAfterMagic(input)
            headerRead = true
            wantAck = header.wantAck
            conn.soTimeout = RECV_IDLE_TIMEOUT_MS

            // 仅接收目标设备：来源 IP 不在选中设备的地址列表就拒收。
            // 必须按完整列表匹配——桌面多网卡/VPN 时连接源 IP 常不是
            // NSD 解析到的那一个,只比对单个 host 会把自己人误拒
            // (对端还会把 ACK_FAIL 显示成"口令不一致",极难排查)。
            if (trustedOnly) {
                val src = conn.inetAddress?.hostAddress
                val sel = selectedPeer?.let { s ->
                    synchronized(peersLock) { peers.values.find { it.name == s } }
                }
                if (sel == null || src == null || src !in allowedSourceAddresses(sel)) {
                    listener.onStatus("已拒收 ${src ?: "?"} 的传输（仅接收目标设备）")
                    drain(conn, input, minOf(header.size, DRAIN_CAP))
                    return   // finally 统一回 ACK_FAIL
                }
            }

            // basename 防路径穿越
            val safeName = ReceiveFiles.safeName(header.filename)
            receivedName = safeName
            val isFolder = header.kind == WHPP.FOLDER_KIND
            if (header.kind !in listOf("", "file", WHPP.FOLDER_KIND)) {
                listener.onStatus("拒收 $safeName：不支持的传输类型")
                return
            }

            // size 来自网络，不可信
            val sizeLimit = if (isFolder) Crypto.chunkedWireSize(WHPP.MAX_FILE_SIZE)
                else WHPP.MAX_FILE_SIZE
            if (header.size < 0 || header.size > sizeLimit) {
                listener.onStatus("拒收 $safeName：文件大小非法")
                return
            }
            if (isFolder) {
                if (header.plainSize < 8 || header.plainSize > WHPP.MAX_FILE_SIZE ||
                    header.modifiedMs < 0) {
                    listener.onStatus("拒收 $safeName：文件夹大小或时间非法")
                    return
                }
                if (header.encrypted && (header.encMode != "chunked" ||
                    header.size != Crypto.chunkedWireSize(header.plainSize))) {
                    listener.onStatus("拒收 $safeName：文件夹加密声明非法")
                    return
                }
                if (!header.encrypted && header.size != header.plainSize) {
                    listener.onStatus("拒收 $safeName：文件夹大小声明不一致")
                    return
                }
            }
            val storageSize = if (isFolder) header.plainSize else header.size
            // Android 10+ MediaStore export temporarily needs the private folder
            // and public pending files at once. Reserve 2x so a completed receive
            // does not become an unusable private-only folder during export.
            val requiredSpace = if (isFolder && storageSize <=
                (Long.MAX_VALUE - DISK_MARGIN) / 2) {
                storageSize * 2 + DISK_MARGIN
            } else if (storageSize <= Long.MAX_VALUE - DISK_MARGIN) {
                storageSize + DISK_MARGIN
            } else Long.MAX_VALUE
            if (requiredSpace > inboxDir.usableSpace) {
                listener.onStatus("拒收 $safeName：存储空间不足")
                drain(conn, input, minOf(header.size, DRAIN_CAP))
                return
            }
            if (header.encrypted && secret.isEmpty()) {
                listener.onStatus("拒收 $safeName：对方启用了加密，本机未设口令")
                drain(conn, input, minOf(header.size, DRAIN_CAP))
                return
            }
            if (header.encrypted && header.encMode == "chunked" && header.size < 32) {
                listener.onStatus("拒收 $safeName：加密流大小非法")
                return
            }
            if (!isFolder && header.encrypted && header.encMode != "chunked" &&
                header.size > MAX_WHE1_SIZE) {
                listener.onStatus("拒收 $safeName：整块加密文件过大")
                drain(conn, input, DRAIN_CAP)
                return
            }

            var lastReport = 0L
            fun report(done: Long) {
                val now = System.currentTimeMillis()
                if (done >= header.size || now - lastReport >= PROGRESS_INTERVAL_MS) {
                    lastReport = now
                    listener.onProgress("recv", safeName, done, header.size)
                }
            }

            val dst: File
            if (isFolder) {
                val staging = File(inboxDir, ".inkhole-${UUID.randomUUID()}.folder.part")
                if (!staging.mkdir()) throw IOException("无法创建文件夹暂存目录")
                folderPart = staging
                val payload: InputStream = if (header.encrypted) {
                    ChunkedFolderInputStream(
                        input, header.size, header.plainSize, secret, ::report)
                } else {
                    BoundedPayloadInputStream(input, header.plainSize, ::report)
                }
                WHF1.receive(payload, header.plainSize, staging)
                (payload as VerifiablePayloadInput).verifyComplete()
                if (header.modifiedMs > 0) staging.setLastModified(header.modifiedMs)
                val committed = synchronized(receiveFileLock) {
                    val candidate = ReceiveFiles.uniqueDirectory(inboxDir, safeName)
                    candidate.takeIf { staging.renameTo(it) }
                }
                if (committed == null) {
                    listener.onStatus("落盘失败: $safeName")
                    return
                }
                folderPart = null
                dst = committed
            } else {
                val part = File.createTempFile("inkhole-", ".part", inboxDir)
                partFile = part
                if (header.encrypted && header.encMode == "chunked") {
                    // WHE2 分块流：边收边解密边落盘，内存峰值 4MB
                    val hdr32 = ByteArray(32)
                    DataInputStream(input).readFully(hdr32)
                    val decryptor = try {
                        Crypto.ChunkedDecryptor(secret, hdr32)
                    } catch (e: IllegalArgumentException) {
                        listener.onStatus("拒收 $safeName：加密流头非法")
                        return
                    }
                    var consumed = 32L
                    var intact = true
                    val din = DataInputStream(input)
                    FileOutputStream(part).use { fout ->
                        while (consumed < header.size) {
                            val ctLen = try { din.readInt() } catch (_: IOException) { intact = false; break }
                            if (ctLen < 16 || ctLen > Crypto.CHUNK_SIZE + 16) { intact = false; break }
                            if (consumed + 4L + ctLen > header.size) { intact = false; break }
                            val ct = ByteArray(ctLen)
                            try { din.readFully(ct) } catch (_: IOException) { intact = false; break }
                            val plain = decryptor.decryptChunk(ct)
                            if (plain == null) {
                                listener.onStatus("解密失败: $safeName（两端口令不一致？）")
                                return
                            }
                            fout.write(plain)
                            consumed += 4 + ctLen
                            report(consumed)
                        }
                    }
                    if (!intact || consumed != header.size) {
                        listener.onStatus("接收中断: $safeName")
                        return
                    }
                } else {
                    // 明文 / WHE1 整块加密：写 .part
                    FileOutputStream(part).use { fout ->
                        val buf = ByteArray(WHPP.BUFFER_SIZE)
                        var remaining = header.size
                        while (remaining > 0) {
                            val toRead = minOf(buf.size.toLong(), remaining).toInt()
                            val n = input.read(buf, 0, toRead)
                            if (n < 0) break
                            fout.write(buf, 0, n)
                            remaining -= n
                            report(header.size - remaining)
                        }
                        if (remaining > 0) {
                            listener.onStatus("接收中断: $safeName")
                            return
                        }
                    }

                    // WHE1 整块加密：解密成功才算收到
                    if (header.encrypted) {
                        val blob = part.readBytes()
                        val plain = Crypto.decrypt(secret, blob)
                        if (plain == null) {
                            listener.onStatus("解密失败: $safeName（两端口令不一致？）")
                            return
                        }
                        part.writeBytes(plain)
                    }
                }

                val committed = synchronized(receiveFileLock) {
                    val candidate = ReceiveFiles.uniqueFile(inboxDir, safeName)
                    candidate.takeIf { part.renameTo(it) }
                }
                if (committed == null) {
                    listener.onStatus("落盘失败: $safeName")
                    return
                }
                partFile = null
                dst = committed
            }
            ok = true

            listener.onFileReceived(dst.name, dst.absolutePath)
            listener.onStatus("已接收：${dst.name}")
        } catch (e: java.io.EOFException) {
            // 探活空连接(probe)：对端 connect 后立即 close，读协议头时 EOF，静默忽略
            if (headerRead) listener.onStatus("接收中断: ${receivedName.ifEmpty { "未知文件" }}")
        } catch (_: SocketTimeoutException) {
            // 未发协议头的半开连接静默关闭；传输开始后超时才提示用户。
            if (headerRead) listener.onStatus("接收中断: ${receivedName.ifEmpty { "未知文件" }}")
        } catch (_: java.net.SocketException) {
            // 对端取消发送(RST 硬断开)或网络断开:按"中断"而非"失败"提示
            if (headerRead) listener.onStatus("接收中断: ${receivedName.ifEmpty { "未知文件" }}")
        } catch (e: Exception) {
            if (running) listener.onStatus("接收失败: ${e.message ?: "未知错误"}")
        } finally {
            partFile?.delete()
            folderPart?.deleteRecursively()
            if (wantAck) {
                try {
                    conn.getOutputStream().apply {
                        write(if (ok) WHPP.ACK_OK else WHPP.ACK_FAIL)
                        flush()
                    }
                } catch (_: IOException) {}
            }
            try { conn.close() } catch (_: IOException) {}
            if (receivedName.isNotEmpty()) {
                listener.onTransferEnded("recv", receivedName, ok)
            }
        }
    }

    /** 拒收时把对端已发出的最多 n 字节读掉再关连接——
     *  不读就 close 会触发 RST，可能冲掉已排队的失败回执。 */
    private fun drain(conn: Socket, input: InputStream, n: Long) {
        try {
            conn.soTimeout = DRAIN_TIMEOUT_MS
            val buf = ByteArray(WHPP.BUFFER_SIZE)
            var left = n
            while (left > 0) {
                val got = input.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                if (got < 0) return
                left -= got
            }
        } catch (_: IOException) {}
    }

    // ---- 发送文件 ----

    fun sendFile(filePath: String, displayName: String = File(filePath).name): Boolean {
        val file = File(filePath)
        if (!file.isFile) {
            listener.onStatus("文件不存在")
            return false
        }
        return sendStream(file.length(), displayName) { file.inputStream() }
    }

    fun cancelSend(): Boolean {
        val active = sendInProgress.get()
        sendCancelled.set(true)
        activeSendSocket.get()?.let { socket ->
            // 取消要立刻生效:SO_LINGER(0) 让 close 直接 RST 丢弃发送缓冲里
            // 已排队的数据(最多 4MB),否则内核把缓冲慢慢发完才断开,跨网
            // 中继链路上对端还要"收"十几秒才看到传输中断
            try { socket.setSoLinger(true, 0) } catch (_: Exception) {}
            try { socket.close() } catch (_: IOException) {}
        }
        activeSendInput.get()?.let { input ->
            try { input.close() } catch (_: IOException) {}
        }
        return active
    }

    fun isSending(): Boolean = sendInProgress.get()

    private fun <T> useSendInput(factory: () -> InputStream,
                                 block: (InputStream) -> T): T {
        val input = factory()
        activeSendInput.set(input)
        return try {
            input.use(block)
        } finally {
            activeSendInput.compareAndSet(input, null)
        }
    }

    /** 直接从 content:// 等输入流发送，避免大文件先完整复制到 cache 再读一遍。 */
    @Synchronized
    fun sendStream(plainSize: Long, displayName: String,
                   shouldCancel: (() -> Boolean)? = null,
                   inputFactory: () -> InputStream): Boolean {
        if (plainSize < 0 || plainSize > WHPP.MAX_FILE_SIZE) {
            listener.onStatus("文件大小无效")
            return false
        }
        val selected = selectedPeer ?: run {
            listener.onStatus("请先选择目标设备")
            return false
        }
        val peer = synchronized(peersLock) { peers.values.find { it.name == selected } } ?: run {
            listener.onStatus("目标设备已离线")
            return false
        }

        val transferName = ReceiveFiles.safeName(displayName)
        var completed = false
        fun cancellationRequested(): Boolean =
            sendCancelled.get() || shouldCancel?.invoke() == true
        sendCancelled.set(false)
        sendInProgress.set(true)
        return try {
            val socket = connectToPeer(peer)
            activeSockets.add(socket)
            activeSendSocket.set(socket)
            if (!running) {
                activeSockets.remove(socket)
                socket.close()
                throw IOException("墨洞节点已停止")
            }
            if (cancellationRequested()) throw java.io.InterruptedIOException("发送已取消")
            try {
                socket.use { s ->
                    val out = BufferedOutputStream(s.getOutputStream(), WHPP.BUFFER_SIZE)
                    var lastReport = 0L
                    val progress: (Long, Long) -> Unit = { done, total ->
                        val now = System.currentTimeMillis()
                        if (done >= total || now - lastReport >= PROGRESS_INTERVAL_MS) {
                            lastReport = now
                            listener.onProgress("send", transferName, done, total)
                        }
                    }

                    if (secret.isNotEmpty() && plainSize > CHUNK_ENC_THRESHOLD) {
                        // 大文件走 WHE2 分块流式加密：内存峰值 4MB
                        val wireSize = Crypto.chunkedWireSize(plainSize)
                        WHPP.writeHeader(out, WHPP.Header(
                            transferName, wireSize, encrypted = true, wantAck = true,
                            encMode = "chunked"))
                        val enc = Crypto.ChunkedEncryptor(secret)
                        out.write(enc.streamHeader)
                        var sent = enc.streamHeader.size.toLong()
                        var plainRead = 0L
                        val dout = DataOutputStream(out)
                        useSendInput(inputFactory) { fin ->
                            val buf = ByteArray(Crypto.CHUNK_SIZE)
                            while (plainRead < plainSize) {
                                if (cancellationRequested()) {
                                    throw java.io.InterruptedIOException("发送已取消")
                                }
                                val wanted = minOf(buf.size.toLong(), plainSize - plainRead).toInt()
                                val n = readFull(fin, buf, wanted)
                                if (n <= 0) throw EOFException("文件读取不完整")
                                val ct = enc.encryptChunk(buf, n)
                                dout.writeInt(ct.size)
                                dout.write(ct)
                                plainRead += n
                                sent += 4 + ct.size
                                progress(sent, wireSize)
                            }
                        }
                        dout.flush()
                    } else if (secret.isNotEmpty()) {
                        // 小文件 WHE1 整块(与所有旧版本互通)
                        val plain = useSendInput(inputFactory) { it.readBytes() }
                        if (plain.size.toLong() != plainSize) throw EOFException("文件读取不完整")
                        if (cancellationRequested()) {
                            throw java.io.InterruptedIOException("发送已取消")
                        }
                        val enc = Crypto.encrypt(secret, plain)
                        WHPP.writeFrame(
                            out, transferName, enc.size.toLong(), true,
                            ByteArrayInputStream(enc),
                            onProgress = { progress(it, enc.size.toLong()) },
                            shouldCancel = ::cancellationRequested,
                        )
                    } else {
                        // 明文: 流式
                        useSendInput(inputFactory) { input ->
                            WHPP.writeFrame(
                                out, transferName, plainSize, false, input,
                                onProgress = { progress(it, plainSize) },
                                shouldCancel = ::cancellationRequested,
                            )
                        }
                    }

                    // 等接收方回执。老版本对端读完即关连接 -> read 返回 -1，按成功；超时也不误报
                    s.soTimeout = 60_000
                    val resp = try { s.getInputStream().read() } catch (e: Exception) {
                        if (cancellationRequested()) throw java.io.InterruptedIOException("发送已取消")
                        if (e is SocketTimeoutException) -1 else throw e
                    }
                    if (resp == WHPP.ACK_FAIL) {
                        listener.onStatus("${peer.name} 接收失败（口令不一致、被拒收或存储问题）")
                        return false
                    }
                }
            } finally {
                activeSendSocket.compareAndSet(socket, null)
                activeSockets.remove(socket)
            }
            completed = true
            listener.onStatus("已发送：$transferName")
            true
        } catch (e: Exception) {
            if (cancellationRequested()) listener.onStatus("已取消发送：$transferName")
            else listener.onStatus("发送失败: ${e.message}")
            false
        } finally {
            activeSendSocket.set(null)
            activeSendInput.set(null)
            sendInProgress.set(false)
            sendCancelled.set(false)
            listener.onTransferEnded("send", transferName, completed)
        }
    }

    /** 尽量读满 buf(文件尾可能不足)，返回实际读到的字节数；EOF 返回 -1。 */
    private fun readFull(input: InputStream, buf: ByteArray, limit: Int = buf.size): Int {
        var off = 0
        while (off < limit) {
            val n = input.read(buf, off, limit - off)
            if (n < 0) break
            off += n
        }
        return if (off == 0) -1 else off
    }

    private fun connectToPeer(peer: Peer): Socket {
        var lastError: Exception? = null
        val targets = (listOf(peer.host) + peer.hosts)
            .filter { it.isNotBlank() }
            .distinct()
            .flatMap(::resolveHostAddresses)
            .let(TailnetAddress::order)
        for (address in targets) {
            if (!running) throw IOException("墨洞节点已停止")
            val socket = socketForAddress(address)
            if (socket == null) {
                lastError = IOException("Tailscale 未连接，无法到达 $address")
                continue
            }
            // 缓冲必须在 connect 前设置(窗口缩放在握手时协商),发送吞吐靠 sndbuf
            try { socket.sendBufferSize = SOCKET_BUFFER } catch (_: Exception) {}
            try { socket.receiveBufferSize = SOCKET_BUFFER } catch (_: Exception) {}
            try {
                socket.connect(java.net.InetSocketAddress(address, peer.port), 15_000)
                if (!running) {
                    socket.close()
                    throw IOException("墨洞节点已停止")
                }
                return socket
            } catch (e: Exception) {
                lastError = e
                try { socket.close() } catch (_: IOException) {}
            }
        }
        throw lastError ?: IOException("目标设备没有可用地址")
    }

    private fun allowedSourceAddresses(peer: Peer): Set<String> {
        val allowed = LinkedHashSet<String>()
        for (host in listOf(peer.host) + peer.hosts) {
            allowed.addAll(resolveHostAddresses(host))
        }
        return allowed
    }

    // ---- 对端管理 ----

    fun getPeers(): List<Peer> = synchronized(peersLock) { peers.values.toList().sortedBy { it.name } }

    /** 实际监听端口(0=尚未启动)。设置页展示"本机"信息用。 */
    fun getActualPort(): Int = actualPort

    fun selectPeer(name: String?) {
        selectedPeer = name
        // 智能保留：记住 serviceName，离线后重新上线能自动恢复选中
        lastSelectedService = if (name != null) {
            synchronized(peersLock) { peers.values.find { it.name == name }?.serviceName }
        } else null
        listener.onStatus(if (name != null) "目标: $name" else "未选择目标")
    }

    fun getSelectedPeer(): String? = selectedPeer

    /** 当前选中目标的 serviceName（用于节点重建后恢复选中）。 */
    fun getSelectedServiceName(): String? = lastSelectedService

    /** 预设"上次选中的 serviceName"：设置变更重建节点时，让智能保留在对端
     *  重新被发现时自动恢复选中，避免用户重新点连接。 */
    fun restoreSelectedService(serviceName: String?) {
        lastSelectedService = serviceName
    }

    private fun addPeer(serviceName: String, displayName: String, host: String, port: Int,
                        hosts: List<String> = listOf(host), instanceId: String = "",
                        capabilities: Set<String> = emptySet(), manual: Boolean = false) {
        var added = false
        var finalName: String
        synchronized(peersLock) {
            val baseName = ReceiveFiles.utf8Prefix(
                displayName.filterNot { it.isISOControl() }.trim(),
                200,
            ).ifBlank { host }
            fun uniqueName(): String {
                var candidate = baseName
                var n = 2
                while (peers.any { (key, peer) ->
                        key != serviceName && peer.name == candidate
                    }) {
                    candidate = "$baseName (${n++})"
                }
                return candidate
            }
            val existing = peers[serviceName]
            if (existing != null) {
                // 同一服务重新解析：同步地址和对端改名，并保持选中状态。
                finalName = uniqueName()
                peers[serviceName] = existing.copy(
                    name = finalName,
                    host = host,
                    port = port,
                    hosts = hosts,
                    instanceId = instanceId,
                    capabilities = capabilities,
                    manual = manual,
                )
                if (selectedPeer == existing.name && lastSelectedService == serviceName) {
                    selectedPeer = finalName
                }
            } else {
                // 不同设备撞了显示名：给后来者加 " (2)" 后缀
                finalName = uniqueName()
                peers[serviceName] = Peer(
                    finalName, host, port, serviceName, hosts,
                    instanceId, capabilities, manual)
                added = true
            }
            // 智能保留：若此设备的 serviceName 匹配之前选中的，自动恢复选择
            if (serviceName == lastSelectedService && selectedPeer == null) {
                selectedPeer = finalName
                listener.onStatus("目标设备 $finalName 重新上线，已自动恢复选中")
            }
        }
        if (added) listener.onStatus("发现: $finalName")
        listener.onPeerChanged(getPeers())
    }

    private fun removePeer(serviceName: String) {
        val removed = synchronized(peersLock) { peers.remove(serviceName) } ?: return
        if (selectedPeer == removed.name) selectedPeer = null
        listener.onPeerChanged(getPeers())
        listener.onStatus("${removed.name} 离线")
    }

    /** onServiceLost 误报兜底：连续 TCP 探活都失败才真移除；任意一次连上视为误报忽略。
     *  探活期间对端可能被重新发现(addPeer 更新 host/port)，故每次都重读最新地址。 */
    private fun verifyLostThenRemove(serviceName: String) {
        if (!probingLost.add(serviceName)) return   // 已在探活，避免并发重复
        scope.launch {
            try {
                repeat(LOST_PROBE_ATTEMPTS) { attempt ->
                    val peer = synchronized(peersLock) { peers[serviceName] }
                        ?: return@launch          // 已被别处移除，无需再探
                    val hosts = LanReachability.hostsOnCurrentLan(
                        (listOf(peer.host) + peer.hosts).distinct(), currentLanLinks())
                    try {
                        probePeer(hosts, peer.port, LOST_PROBE_TIMEOUT_MS, peer.instanceId)
                        return@launch  // 还活着，误报忽略
                    } catch (_: Exception) {}
                    if (attempt < LOST_PROBE_ATTEMPTS - 1) delay(LOST_PROBE_INTERVAL_MS)
                }
                // 连续都失败：确认真离线
                removePeer(serviceName)
            } finally {
                probingLost.remove(serviceName)
            }
        }
    }

    /** 真正的 Tailscale 网络 = 持有 Tailnet IPv4/IPv6 地址的 VPN 网络。不能只看
     *  TRANSPORT_VPN:Clash/Mihomo 等代理的 TUN 也是 VPN,且会对任意 TCP
     *  连接先本地假 accept——纯 connect 探活在它上面永远"成功"。 */
    private fun tailscaleNetwork(): Network? = try {
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE)
            as? ConnectivityManager
        manager?.allNetworks?.firstOrNull { network ->
            manager.getNetworkCapabilities(network)
                ?.hasTransport(NetworkCapabilities.TRANSPORT_VPN) == true &&
                manager.getLinkProperties(network)?.linkAddresses.orEmpty().any { link ->
                    link.address.hostAddress?.let(TailnetAddress::isTailnet) == true
                }
        }
    } catch (_: Exception) {
        null
    }

    private fun resolveHostAddresses(host: String): List<String> {
        TailnetAddress.numericAddress(host)?.hostAddress?.let { return listOf(it) }
        val addresses = LinkedHashSet<String>()
        tailscaleNetwork()?.let { network ->
            try {
                network.getAllByName(host).mapNotNullTo(addresses) { it.hostAddress }
            } catch (_: Exception) {}
        }
        try {
            InetAddress.getAllByName(host).mapNotNullTo(addresses) { it.hostAddress }
        } catch (_: Exception) {}
        return TailnetAddress.order(addresses.toList())
    }

    /** 为数字目标地址创建出站 socket。Tailnet 目标必须绑定真正的
     *  Tailscale 网络:它不在时返回 null 直接判不可达。否则 connect 走
     *  默认路由——被代理 TUN 假 accept 或泄漏进运营商 CGNAT,表现为对端
     *  明明下线(甚至 Tailscale 都没开)探活却一直"成功",设备永远在列表。 */
    private fun socketForAddress(address: String): Socket? {
        if (!TailnetAddress.isTailnet(address)) return Socket()
        val vpn = tailscaleNetwork() ?: return null
        return try {
            vpn.socketFactory.createSocket()
        } catch (_: Exception) {
            null
        }
    }

    private fun probePeer(hosts: List<String>, port: Int, timeoutMs: Int,
                          expectedInstanceId: String = ""): PeerProbeResult {
        var lastError: Exception? = null
        var identityError: IdentityMismatchException? = null
        var tailnetUnavailable: TailnetUnavailableException? = null
        val targets = TailnetAddress.order(hosts.flatMap(::resolveHostAddresses))
        for (address in targets) {
            val socket = socketForAddress(address)
            if (socket == null) {
                tailnetUnavailable = TailnetUnavailableException(
                    "Tailscale 未连接，无法到达 $address")
                continue
            }
            try {
                socket.use {
                    it.connect(java.net.InetSocketAddress(address, port), timeoutMs)
                    it.soTimeout = timeoutMs
                    it.getOutputStream().apply {
                        write(WHPP.CAP_MAGIC)
                        flush()
                    }
                    val capabilities = WHPP.readCapabilities(it.getInputStream())
                    if (expectedInstanceId.isNotEmpty() &&
                        capabilities.instanceId != expectedInstanceId.lowercase()) {
                        throw IdentityMismatchException(
                            "设备身份已变化，请删除后重新添加")
                    }
                    return PeerProbeResult(
                        capabilities.instanceId,
                        capabilities.peerName,
                        capabilities.capabilities,
                        address,
                    )
                }
            } catch (e: IdentityMismatchException) {
                identityError = e
            } catch (e: Exception) {
                lastError = e
            }
        }
        identityError?.let { throw it }
        tailnetUnavailable?.let { throw it }
        throw (lastError ?: IOException("目标设备没有可用地址"))
    }

    // ---- NSD 注册 ----

    private var registrationListener: NsdManager.RegistrationListener? = null
    private var discoveryListener: NsdManager.DiscoveryListener? = null

    private fun registerNsd() {
        val nsd = nsdManager ?: return
        val info = NsdServiceInfo().apply {
            // 服务名带唯一后缀：两台同名手机(如同型号 Build.MODEL)不再撞名
            serviceName = requestedServiceName
            serviceType = SERVICE_TYPE
            port = actualPort
            // 与桌面版 zeroconf 互通的 TXT 属性
            setAttribute("peer_name", advertisedPeerName)
            setAttribute("instance_id", instanceId)
            setAttribute("whpc", WHPP.CAP_VERSION.toString())
            setAttribute("caps", WHPP.FOLDER_KIND)
        }
        registrationListener = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(serviceInfo: NsdServiceInfo) {
                // 系统冲突改名后这里才是真实注册名，自我过滤必须用它
                if (running) registeredName = serviceInfo.serviceName
            }
            override fun onRegistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                if (running) listener.onStatus("NSD 注册失败: $errorCode")
            }
            override fun onServiceUnregistered(serviceInfo: NsdServiceInfo) {}
            override fun onUnregistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {}
        }
        nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, registrationListener)
    }

    // ---- NSD 发现 ----
    // Android 同一时刻只允许一个 resolveService 在跑，并发会 FAILURE_ALREADY_ACTIVE
    // (设备"时而发现不了"的经典原因)。这里排队逐个解析。

    private val resolveQueue = ConcurrentLinkedQueue<NsdServiceInfo>()
    private val resolving = AtomicBoolean(false)
    private val discoveryRestartPending = AtomicBoolean(false)

    private fun enqueueResolve(serviceInfo: NsdServiceInfo) {
        if (!running) return
        resolveQueue.add(serviceInfo)
        drainResolveQueue()
    }

    @Suppress("DEPRECATION")
    private fun drainResolveQueue() {
        if (!running) {
            resolveQueue.clear()
            resolving.set(false)
            return
        }
        if (!resolving.compareAndSet(false, true)) return
        val next = resolveQueue.poll()
        if (next == null) {
            resolving.set(false)
            return
        }
        // 用"发现时"的服务名做对端表的 key：resolve 返回的名字转义可能不一致
        // (含空格等字符时)，而 onServiceLost 给的是发现时的名字
        val discoveryName = next.serviceName
        val nsd = nsdManager
        if (nsd == null) {
            resolving.set(false)
            return
        }
        try {
            nsd.resolveService(next, object : NsdManager.ResolveListener {
                override fun onServiceResolved(info: NsdServiceInfo) {
                    if (running) handleResolved(discoveryName, info)
                    resolving.set(false)
                    if (running) drainResolveQueue()
                }
                override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
                    if (running && errorCode == NsdManager.FAILURE_ALREADY_ACTIVE) {
                        resolveQueue.add(next)   // 稍后重试
                    }
                    resolving.set(false)
                    if (running) drainResolveQueue()
                }
            })
        } catch (e: Exception) {
            resolving.set(false)
            if (running) {
                listener.onStatus("NSD 解析失败: ${e.message}")
                drainResolveQueue()
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun handleResolved(discoveryName: String, info: NsdServiceInfo) {
        if (!running) return
        val resolvedHosts = LinkedHashSet<String>()
        if (android.os.Build.VERSION.SDK_INT >= 34) {
            try {
                info.hostAddresses.forEach { address ->
                    address.hostAddress?.let { resolvedHosts.add(it) }
                }
            } catch (_: Exception) {}
        } else {
            info.host?.hostAddress?.let { resolvedHosts.add(it) }
        }
        val host = resolvedHosts.firstOrNull() ?: return
        val attrs = try { info.attributes } catch (_: Exception) { emptyMap<String, ByteArray>() }
        val txtInstanceId = attrs["instance_id"]?.toString(Charsets.UTF_8)?.lowercase()
            ?.takeIf { it.matches(Regex("[0-9a-f]{32}")) } ?: return
        if (attrs["whpc"]?.toString(Charsets.UTF_8) != WHPP.CAP_VERSION.toString()) return
        if (txtInstanceId == instanceId) return
        val displayName = attrs["peer_name"]?.toString(Charsets.UTF_8)?.takeIf { it.isNotBlank() }
            ?: discoveryName
        // 兜底自我过滤：同名 + 地址是本机 IP，判定为自己的历史注册(旧 instanceId、
        // goodbye 丢包残留)，丢弃不显示。
        if (displayName == peerName && host in localIps()) return
        // 对端全部地址：TXT ips(桌面端宣告,多网卡/VPN 全覆盖) + API34 hostAddresses
        val hosts = LinkedHashSet<String>()
        hosts.addAll(resolvedHosts)
        attrs["ips"]?.toString(Charsets.UTF_8)?.split(",")
            ?.map { it.trim() }?.filter { it.isNotEmpty() }?.let { hosts.addAll(it) }
        val hostList = hosts.toList()
        // 发现与更新都先做 WHPC v2 身份验证。系统 mDNS 缓存(Android 13+ 常驻缓存,
        // 对端崩溃/断网不发 goodbye 时记录可存活几十分钟)会在重启发现时立即
        // 回灌陈旧记录——探活循环刚剔除的下线设备下一秒又被 resolve"复活",
        // 表现为对端明明关了却一直显示在线。可达性标准与探活循环一致:仅认
        // 当前 WiFi/以太网可达的地址,不给 Tailscale 等 VPN 路径兜底的机会。
        scope.launch {
            val candidates = LanReachability.hostsOnCurrentLan(hostList, currentLanLinks())
            val result = try {
                probePeer(candidates, info.port, LOST_PROBE_TIMEOUT_MS, txtInstanceId)
            } catch (_: Exception) { return@launch }
            if (running) addPeer(
                discoveryName, displayName, result.connectedAddress, info.port,
                hostList, result.instanceId, result.capabilities, manual = false)
        }
    }

    private fun localIps(): Set<String> {
        val ips = mutableSetOf("127.0.0.1")
        try {
            for (nif in NetworkInterface.getNetworkInterfaces()) {
                for (addr in nif.inetAddresses) {
                    if (!addr.isLoopbackAddress) addr.hostAddress?.let { ips.add(it) }
                }
            }
        } catch (_: Exception) {}
        return ips
    }

    /** 当前真正的局域网链路；排除蜂窝网络和 Tailscale 等 VPN transport。 */
    @Suppress("DEPRECATION")
    private fun currentLanLinks(): List<LanLink> {
        return try {
            val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as? ConnectivityManager ?: return emptyList()
            manager.allNetworks.flatMap { network ->
                val capabilities = manager.getNetworkCapabilities(network)
                    ?: return@flatMap emptyList()
                // Android 的 VPN 网络会继承底层网络的 transport(Tailscale 跑在
                // WiFi 上时同时报告 WIFI + VPN),必须显式排除 VPN,否则 TUN 接口
                // 地址也会被当成局域网链路。
                val isLan = (capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
                    capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) &&
                    !capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
                if (!isLan) return@flatMap emptyList()
                manager.getLinkProperties(network)?.linkAddresses.orEmpty().mapNotNull { link ->
                    val address = link.address
                    if (address.isLoopbackAddress || address.isAnyLocalAddress) null
                    else address.hostAddress?.let { LanLink(it, link.prefixLength) }
                }
            }.distinct()
        } catch (_: Exception) {
            emptyList()
        }
    }

    private fun discoverNsd() {
        if (!running) return
        val nsd = nsdManager ?: return
        discoveryListener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onDiscoveryStopped(serviceType: String) {}

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                if (!running) return
                // 跳过自己(用系统实际注册名比对；冲突改名场景由 resolve 后的 instance_id 兜底)
                if (serviceInfo.serviceName == (registeredName ?: requestedServiceName)) return
                enqueueResolve(serviceInfo)
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                if (!running) return
                // NSD onServiceLost 在 WiFi 组播丢包时经常误报(设备其实还在线)。
                // 直接移除会导致"离线→仅接收目标拒收→又上线"的反复抖动。
                // 先 TCP 探活确认真连不上再移除。
                verifyLostThenRemove(serviceInfo.serviceName)
            }

            override fun onStartDiscoveryFailed(serviceType: String?, errorCode: Int) {
                if (running) listener.onStatus("NSD 发现启动失败: $errorCode")
            }
            override fun onStopDiscoveryFailed(serviceType: String?, errorCode: Int) {}
        }
        nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener)
    }
}
