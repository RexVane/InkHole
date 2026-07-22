package com.rexvane.inkhole.p2p

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import java.io.*
import java.net.InetAddress
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.Semaphore
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import org.json.JSONObject

/** 检测到设备/收到文件/状态变化时的回调。 */
interface InkHoleListener {
    fun onPeerChanged(peers: List<Peer>)
    fun onFileReceived(filename: String, path: String, transferId: String)
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
    // 对端全部已知地址，用于多网卡/VPN 场景下逐个尝试连接。
    val hosts: List<String> = listOf(host),
    val instanceId: String = "",
    val capabilities: Set<String> = emptySet(),
    val manual: Boolean = false,
    val transport: String = if (manual) "tailscale" else "lan",
    val endpointToken: String = "",
    val publicKey: String = "",
    val identityFingerprint: String = "",
)

internal fun transferRouteCandidates(
    selected: Peer,
    available: List<Peer>,
): List<Peer> {
    if (!selected.instanceId.matches(Regex("[0-9a-f]{32}")) ||
        selected.transport == "wormhole") return listOf(selected)
    val priority = mapOf("ssh" to 0, "lan" to 1, "tailscale" to 2)
    return available.asSequence()
        .filter { candidate ->
            candidate === selected ||
                (candidate.instanceId == selected.instanceId && when (candidate.transport) {
                    "ssh" -> candidate.endpointToken.isNotEmpty()
                    "lan", "tailscale" -> selected.identityFingerprint.isNotEmpty() &&
                        candidate.identityFingerprint == selected.identityFingerprint
                    else -> false
                })
        }
        .distinctBy { candidate ->
            candidate.serviceName.ifEmpty {
                "${candidate.transport}|${candidate.host}|${candidate.port}"
            }
        }
        .sortedWith(compareBy<Peer>(
            { if (it === selected) 0 else 1 },
            { priority[it.transport] ?: 9 },
            { it.serviceName },
        ))
        .toList()
        .ifEmpty { listOf(selected) }
}

private fun transferRouteLabel(peer: Peer): String = when (peer.transport) {
    "ssh" -> "SSH 中继"
    "lan" -> "局域网"
    "tailscale" -> "Tailscale"
    else -> "备用通道"
}

private data class PeerProbeResult(
    val instanceId: String,
    val peerName: String,
    val capabilities: Set<String>,
    val connectedAddress: String,
    val publicKey: String,
    val fingerprint: String,
)

private class IdentityMismatchException(message: String) : IOException(message)
private class TailnetUnavailableException(message: String) : IOException(message)
private class ReceiverRejectedException(message: String) : IOException(message)

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
    private val listenPort: Int = 0,            // 固定监听端口;0 = 系统自动分配(跨网手动直连需固定)
    private val listener: InkHoleListener,
) {
    companion object {
        private const val SERVICE_TYPE = "_inkhole._tcp."
        private const val JMDNS_SERVICE_TYPE = "_inkhole._tcp.local."
        private const val DISK_MARGIN = 256L * 1024 * 1024   // 收完至少还要剩这么多
        private const val PROGRESS_INTERVAL_MS = 250L
        private const val HEADER_TIMEOUT_MS = 15_000
        private const val RECV_IDLE_TIMEOUT_MS = 300_000
        private const val CHECKPOINT_MAX_AGE_MS = 7L * 24 * 60 * 60 * 1000
        private const val MAX_INCOMING_CONNECTIONS = 4
        private const val MAX_IDENTITY_FIELD = 512
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
        private const val LAN_CHANGE_CHECK_INTERVAL_MS = 5_000L
        private const val EMPTY_DISCOVERY_RESTART_TICKS = 6
        // 手动设备探活超时:Tailscale 空闲后懒惰唤醒(打洞/DERP 建链)首次
        // 握手常超 1.2s,太紧会把在线的跨网设备判死或迟迟不上线
        private const val PROBE_TIMEOUT_MANUAL_MS = 3_000
        private val CORE_MAGIC = "IKCI".toByteArray(Charsets.US_ASCII)
        private val AUTH_MAGIC = "IKAT".toByteArray(Charsets.US_ASCII)
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var nsdManager: NsdManager? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    private var jmDnsDiscovery: JmDnsDiscovery? = null
    private var lanBroadcastDiscovery: LanBroadcastDiscovery? = null
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
    private val deviceIdentity = DeviceIdentity(context)
    private val coreIngressToken = ByteArray(24).also(SecureRandom()::nextBytes).let {
        android.util.Base64.encodeToString(
            it,
            android.util.Base64.URL_SAFE or android.util.Base64.NO_WRAP or
                android.util.Base64.NO_PADDING,
        )
    }
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
    private val checkpointGate = CheckpointGate()
    private val outgoingStateLock = Any()
    // onServiceLost 误报兜底：正在 TCP 探活确认的 serviceName，避免并发重复探活
    private val probingLost = java.util.Collections.synchronizedSet(HashSet<String>())
    private val pendingDiscoveryProbes = ConcurrentHashMap.newKeySet<String>()
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

    private fun cleanupTransferArtifacts() {
        val cutoff = System.currentTimeMillis() - CHECKPOINT_MAX_AGE_MS
        val groups = LinkedHashMap<String, MutableList<File>>()
        inboxDir.listFiles().orEmpty().forEach { artifact ->
            if (!artifact.name.startsWith(".inkhole-")) return@forEach
            val transferId = Regex("^\\.inkhole-([0-9a-f]{64})\\.")
                .find(artifact.name)?.groupValues?.get(1)
            if (transferId != null) {
                groups.getOrPut(transferId) { mutableListOf() }.add(artifact)
            } else if (artifact.lastModified() in 1 until cutoff) {
                if (artifact.isDirectory) artifact.deleteRecursively() else artifact.delete()
            }
        }
        groups.forEach { (transferId, artifacts) ->
            val newest = artifacts.maxOfOrNull(File::lastModified) ?: return@forEach
            if (newest >= cutoff || checkpointGate.isActive(transferId)) return@forEach
            artifacts.forEach { artifact ->
                if (artifact.isDirectory) artifact.deleteRecursively() else artifact.delete()
            }
        }
    }

    private fun acquireMulticastLock() {
        if (multicastLock?.isHeld == true) return
        try {
            val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE)
                as? WifiManager ?: return
            multicastLock = wifi.createMulticastLock("inkhole-mdns").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (error: Exception) {
            multicastLock = null
            listener.onStatus("局域网组播初始化失败: ${error.message}")
        }
    }

    private fun releaseMulticastLock() {
        try {
            multicastLock?.takeIf { it.isHeld }?.release()
        } catch (_: Exception) {
        } finally {
            multicastLock = null
        }
    }

    // ---- 生命周期 ----

    fun start() {
        if (running) return
        running = true
        inboxDir.mkdirs()
        cleanupTransferArtifacts()
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
        acquireMulticastLock()
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
        jmDnsDiscovery = JmDnsDiscovery(
            JMDNS_SERVICE_TYPE,
            onResolved = { record ->
                handleResolvedRecord(
                    record.name, record.port, record.addresses, record.attributes)
            },
            onLost = { serviceName ->
                if (running) verifyLostThenRemove(serviceName)
            },
        )
        scope.launch { restartJmDnsDiscovery() }
        lanBroadcastDiscovery = LanBroadcastDiscovery(
            scope = scope,
            instanceId = instanceId,
            listenPort = actualPort,
            links = ::currentLanLinks,
            onAnnouncement = ::handleLanAnnouncement,
            onError = { detail ->
                if (running) listener.onStatus("热点设备发现启动失败: $detail")
            },
        ).also { it.start() }
        // 手动设备不再启动即乐观入列:前台服务被厂商省电反复杀死重启,每次
        // 重启都会把早已关机的对端"复活"成假在线。改由探活循环首轮(立即执行)
        // 验证,连得上才显示——列表语义收紧为"当前真实在线的设备"。
        startProbeLoop()  // 全量存活探活:定期 TCP 探测所有对端(含自动发现),清理断网/崩溃残留
        // 网络变化与发现自愈。Android 的 NSD 不会稳定地跟随 WiFi/热点接口切换，
        // 每 5 秒比较一次真实 LAN 链路；变化后立即重启发现。列表持续为空时仍
        // 每 30 秒重启一次，覆盖 WiFi 省电丢组播/系统服务卡住的情况。
        scope.launch {
            var previousLinks = lanLinkSignature(currentLanLinks())
            var emptyTicks = 0
            while (running) {
                delay(LAN_CHANGE_CHECK_INTERVAL_MS)
                val currentLinks = lanLinkSignature(currentLanLinks())
                if (currentLinks != previousLinks) {
                    previousLinks = currentLinks
                    emptyTicks = 0
                    if (running) {
                        restartDiscovery()
                        probeNow()
                    }
                    continue
                }
                emptyTicks += 1
                if (emptyTicks < EMPTY_DISCOVERY_RESTART_TICKS) continue
                emptyTicks = 0
                val hasDiscoveredPeer = synchronized(peersLock) {
                    peers.values.any { !it.manual && it.transport == "lan" }
                }
                if (!hasDiscoveredPeer && running) restartDiscovery()
            }
        }
        // 把当前列表主动推给 UI:服务被杀重启后 Activity 可能还挂着旧节点的
        // 设备列表,周围没有设备时不会再有 onPeerChanged 事件来纠正它。
        listener.onPeerChanged(getPeers())
        listener.onStatus("墨洞已开启 · $peerName")
    }

    /** 重启系统 NSD 与显式接口 mDNS 发现。stop 是异步的,稍等再启。 */
    fun restartDiscovery() {
        if (!running || !discoveryRestartPending.compareAndSet(false, true)) return
        scope.launch {
            try {
                restartJmDnsDiscovery(force = true)
                val nsd = nsdManager ?: return@launch
                try {
                    discoveryListener?.let { nsd.stopServiceDiscovery(it) }
                } catch (_: Exception) {}
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
            publicKey = result.publicKey, identityFingerprint = result.fingerprint,
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
                val targets = synchronized(peersLock) {
                    peers.toList().filterNot { (key, _) -> key.startsWith("external|") }
                }
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
                            LanReachability.verifiedPeerCandidates(
                                peer.hosts, lanLinks, peer.host)
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
                    val result = outcome.result
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

                    if (result == null) {
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
                                    publicKey = result.publicKey,
                                    identityFingerprint = result.fingerprint,
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
        jmDnsDiscovery?.stop()
        jmDnsDiscovery = null
        lanBroadcastDiscovery?.stop()
        lanBroadcastDiscovery = null
        releaseMulticastLock()
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

    private fun validSha256(value: String): Boolean =
        value.length == 64 && value.all { it in '0'..'9' || it in 'a'..'f' }

    private fun ByteArray.hex(): String = joinToString("") { "%02x".format(it) }

    private fun sha256(file: File): ByteArray {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().buffered(WHPP.BUFFER_SIZE).use { input ->
            val buffer = ByteArray(WHPP.BUFFER_SIZE)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                if (count > 0) digest.update(buffer, 0, count)
            }
        }
        return digest.digest()
    }

    private fun readJson(file: File): JSONObject? = try {
        if (file.isFile) JSONObject(file.readText(Charsets.UTF_8)) else null
    } catch (_: Exception) { null }

    private fun writeJsonAtomic(file: File, value: JSONObject) {
        val temporary = File(file.parentFile, "${file.name}.${UUID.randomUUID()}.tmp")
        try {
            FileOutputStream(temporary).use { output ->
                output.write(value.toString().toByteArray(Charsets.UTF_8))
                output.flush()
                output.fd.sync()
            }
            if (!temporary.renameTo(file)) throw IOException("无法保存传输检查点")
        } finally {
            temporary.delete()
        }
    }

    private fun receiveMetadata(header: WHPP.Header, safeName: String): JSONObject =
        JSONObject().apply {
            put("version", WHPP.PROTOCOL_VERSION)
            put("filename", safeName)
            put("plain_size", header.plainSize)
            put("sha256", header.sha256)
            put("kind", header.kind)
            put("mtime_ms", header.modifiedMs)
            put("sender_instance_id", header.senderInstanceId)
            put("sender_fingerprint", DeviceAuth.fingerprint(header.senderPublicKey))
        }

    // 接收是对端发起的，不主动清理系统缓存；只按当前真实可用空间保守判断。
    @android.annotation.SuppressLint("UsableSpace")
    private fun handleConnection(conn: Socket) {
        var folderPart: File? = null
        var checkpointId = ""
        var checkpointClaimed = false
        var ackSent = false
        var ok = false
        var transferStarted = false
        var receivedName = ""
        try {
            conn.soTimeout = HEADER_TIMEOUT_MS
            val input = BufferedInputStream(conn.getInputStream(), WHPP.BUFFER_SIZE)
            val din = DataInputStream(input)
            val output = BufferedOutputStream(conn.getOutputStream(), WHPP.BUFFER_SIZE)
            val dout = DataOutputStream(output)
            var magic = WHPP.readMagic(input)
            if (magic.contentEquals(CORE_MAGIC)) {
                val supplied = ByteArray(coreIngressToken.length)
                din.readFully(supplied)
                if (!MessageDigest.isEqual(
                        supplied, coreIngressToken.toByteArray(Charsets.US_ASCII))) return
                magic = WHPP.readMagic(input)
            }
            if (magic.contentEquals(WHPP.CAP_MAGIC)) {
                val nonce = ByteArray(32).also(din::readFully)
                WHPP.writeCapabilities(
                    output, instanceId, advertisedPeerName, nonce, deviceIdentity)
                return
            }
            if (magic.contentEquals(LanHintProtocol.MAGIC)) {
                val frame = ByteArray(LanHintProtocol.FRAME_SIZE - magic.size)
                    .also(din::readFully)
                LanHintProtocol.decode(magic + frame)?.let { hint ->
                    val source = conn.inetAddress?.hostAddress?.substringBefore('%')
                    if (!source.isNullOrBlank()) {
                        handleLanHint(source, hint.instanceId, hint.port)
                    }
                }
                return
            }
            if (!magic.contentEquals(WHPP.MAGIC)) return
            val header = WHPP.readHeaderAfterMagic(input)
            conn.soTimeout = RECV_IDLE_TIMEOUT_MS

            val safeName = ReceiveFiles.safeName(header.filename)
            receivedName = safeName
            val isFolder = header.kind == WHPP.FOLDER_KIND
            if (header.version != WHPP.PROTOCOL_VERSION || !header.wantAck ||
                header.kind !in setOf("file", WHPP.FOLDER_KIND) ||
                header.plainSize < 0 || header.plainSize > WHPP.MAX_FILE_SIZE ||
                (isFolder && header.plainSize < 8) || header.modifiedMs < 0 ||
                !validSha256(header.transferId) || !validSha256(header.sha256) ||
                !header.senderInstanceId.matches(Regex("[0-9a-f]{32}")) ||
                (header.encrypted && header.encMode != "chunked")) {
                listener.onStatus("拒收 $safeName：WHPP v3 传输声明非法")
                return
            }
            if (header.encrypted && secret.isEmpty()) {
                listener.onStatus("拒收 $safeName：对方启用了加密，本机未设口令")
                return
            }
            try {
                DeviceAuth.fingerprint(header.senderPublicKey)
            } catch (_: Exception) {
                listener.onStatus("拒收 $safeName：发送设备公钥非法")
                return
            }

            checkpointId = header.transferId
            if (!checkpointGate.acquire(checkpointId, RECV_IDLE_TIMEOUT_MS.toLong())) {
                listener.onStatus("拒收 $safeName：等待前一次传输结束超时")
                return
            }
            checkpointClaimed = true

            val base = File(inboxDir, ".inkhole-${header.transferId}")
            val part = File("${base.absolutePath}.part")
            val meta = File("${base.absolutePath}.json")
            val done = File("${base.absolutePath}.done.json")
            val commit = File("${base.absolutePath}.commit.json")
            val metadata = receiveMetadata(header, safeName)
            fun authenticateSender(offset: Long, nonce: ByteArray) {
                val signatureSize = din.readUnsignedShort()
                if (signatureSize !in 1..256) throw IOException("发送设备签名大小非法")
                val signature = ByteArray(signatureSize).also(din::readFully)
                    .toString(Charsets.US_ASCII)
                if (!DeviceAuth.verify(header.senderPublicKey,
                        DeviceAuth.transferMessage(nonce, header, offset), signature)) {
                    throw IOException("发送设备身份签名无效")
                }
            }
            fun sendReceiverChallenge(offset: Long): ByteArray {
                val nonce = ByteArray(32).also(SecureRandom()::nextBytes)
                val publicKey = deviceIdentity.publicKey.toByteArray(Charsets.US_ASCII)
                val signature = deviceIdentity.sign(DeviceAuth.receiverMessage(
                    nonce, header, offset, instanceId)).toByteArray(Charsets.US_ASCII)
                if (publicKey.size !in 1..MAX_IDENTITY_FIELD ||
                    signature.size !in 1..MAX_IDENTITY_FIELD) {
                    throw IOException("接收设备身份字段过大")
                }
                dout.writeByte(WHPP.RESUME)
                dout.writeLong(offset)
                dout.write(nonce)
                dout.write(instanceId.toByteArray(Charsets.US_ASCII))
                dout.writeShort(publicKey.size)
                dout.write(publicKey)
                dout.writeShort(signature.size)
                dout.write(signature)
                dout.flush()
                return nonce
            }
            val pendingCommit = readJson(commit)
            val recoveredDestination = ReceiveCommits.recover(
                inboxDir, pendingCommit, part, metadata, isFolder)
            if (recoveredDestination == null && commit.exists()) {
                commit.delete()
            }

            val completed = readJson(done)
            // Public Downloads export removes the private destination after commit.
            // The durable receipt, not that movable path, is the idempotency proof.
            if (WHPP.metadataMatches(completed, metadata) || recoveredDestination != null) {
                val nonce = sendReceiverChallenge(header.plainSize)
                authenticateSender(header.plainSize, nonce)
                if (din.readLong() != 0L) throw IOException("已完成传输仍收到数据")
                recoveredDestination?.let { destination ->
                    if (header.modifiedMs > 0) destination.setLastModified(header.modifiedMs)
                    writeJsonAtomic(done, pendingCommit!!)
                }
                if (isFolder) part.delete()
                meta.delete()
                commit.delete()
                dout.writeByte(WHPP.ACK_OK)
                dout.write(header.sha256.chunked(2).map { it.toInt(16).toByte() }.toByteArray())
                dout.flush()
                ackSent = true
                ok = true
                recoveredDestination?.let { destination ->
                    listener.onFileReceived(
                        destination.name, destination.absolutePath, header.transferId)
                    listener.onStatus("已恢复并校验：${destination.name}")
                }
                return
            }

            if (!WHPP.metadataMatches(readJson(meta), metadata)) {
                part.delete()
                meta.delete()
                writeJsonAtomic(meta, metadata)
            }
            var offset = if (part.isFile) part.length() else 0L
            if (offset > header.plainSize) {
                part.delete()
                offset = 0
            }

            val remainingSpace = header.plainSize - offset
            val requiredSpace = when {
                remainingSpace > Long.MAX_VALUE - DISK_MARGIN -> Long.MAX_VALUE
                isFolder && header.plainSize >
                    Long.MAX_VALUE - DISK_MARGIN - remainingSpace -> Long.MAX_VALUE
                isFolder -> remainingSpace + header.plainSize + DISK_MARGIN
                else -> remainingSpace + DISK_MARGIN
            }
            if (requiredSpace > inboxDir.usableSpace) {
                listener.onStatus("拒收 $safeName：存储空间不足")
                return
            }

            var lastReport = 0L
            fun report(doneBytes: Long) {
                val now = System.currentTimeMillis()
                if (doneBytes >= header.plainSize || now - lastReport >= PROGRESS_INTERVAL_MS) {
                    lastReport = now
                    listener.onProgress("recv", safeName, doneBytes, header.plainSize)
                }
            }
            transferStarted = true
            report(offset)
            val nonce = sendReceiverChallenge(offset)
            authenticateSender(offset, nonce)

            val remaining = header.plainSize - offset
            val bodySize = din.readLong()
            val expectedWire = if (header.encrypted && remaining > 0) {
                Crypto.chunkedWireSize(remaining)
            } else remaining
            if (bodySize != expectedWire) throw IOException("续传数据长度不一致")

            if (remaining > 0) {
                FileOutputStream(part, true).use { fileOutput ->
                    var appended = 0L
                    if (header.encrypted) {
                        val streamHeader = ByteArray(32).also(din::readFully)
                        val decryptor = Crypto.ChunkedDecryptor(secret, streamHeader)
                        var consumed = 32L
                        while (consumed < bodySize) {
                            val cipherSize = din.readInt()
                            if (cipherSize !in 16..Crypto.CHUNK_SIZE + 16 ||
                                consumed + 4 + cipherSize > bodySize) {
                                throw IOException("加密分块非法")
                            }
                            val ciphertext = ByteArray(cipherSize).also(din::readFully)
                            val plain = decryptor.decryptChunk(ciphertext)
                                ?: throw IOException("解密失败（两端口令不一致？）")
                            if (appended + plain.size > remaining) {
                                throw IOException("解密数据超过声明大小")
                            }
                            fileOutput.write(plain)
                            appended += plain.size
                            consumed += 4 + cipherSize
                            report(offset + appended)
                        }
                        if (consumed != bodySize || appended != remaining) {
                            throw EOFException("加密数据不完整")
                        }
                    } else {
                        val buffer = ByteArray(WHPP.BUFFER_SIZE)
                        while (appended < remaining) {
                            val count = input.read(
                                buffer, 0, minOf(buffer.size.toLong(), remaining - appended).toInt())
                            if (count < 0) throw EOFException("文件数据不完整")
                            fileOutput.write(buffer, 0, count)
                            appended += count
                            report(offset + appended)
                        }
                    }
                    fileOutput.flush()
                    fileOutput.fd.sync()
                }
            } else if (!part.exists() && !part.createNewFile()) {
                throw IOException("无法创建空文件检查点")
            }

            if (part.length() != header.plainSize) throw EOFException("文件数据不完整")
            val actualDigest = sha256(part)
            val expectedDigest = header.sha256.chunked(2)
                .map { it.toInt(16).toByte() }.toByteArray()
            if (!MessageDigest.isEqual(actualDigest, expectedDigest)) {
                part.delete()
                meta.delete()
                throw IOException("文件 SHA-256 校验失败，已丢弃检查点")
            }

            val destination: File
            val receipt: JSONObject
            if (isFolder) {
                val staging = File(inboxDir, ".inkhole-${UUID.randomUUID()}.folder.part")
                if (!staging.mkdir()) throw IOException("无法创建文件夹暂存目录")
                folderPart = staging
                try {
                    part.inputStream().buffered(WHPP.BUFFER_SIZE).use { payload ->
                        WHF1.receive(payload, header.plainSize, staging)
                    }
                } catch (error: Exception) {
                    part.delete()
                    meta.delete()
                    throw error
                }
                if (header.modifiedMs > 0) staging.setLastModified(header.modifiedMs)
                val committed = synchronized(receiveFileLock) {
                    val candidate = ReceiveFiles.uniqueDirectory(inboxDir, safeName)
                    val candidateReceipt = receiveMetadata(header, safeName).apply {
                        put("path", candidate.absolutePath)
                        put("completed_at", System.currentTimeMillis() / 1000)
                    }
                    writeJsonAtomic(commit, candidateReceipt)
                    if (!staging.renameTo(candidate)) throw IOException("落盘失败: $safeName")
                    candidate to candidateReceipt
                }
                destination = committed.first
                receipt = committed.second
                folderPart = null
            } else {
                val committed = synchronized(receiveFileLock) {
                    val candidate = ReceiveFiles.uniqueFile(inboxDir, safeName)
                    val candidateReceipt = receiveMetadata(header, safeName).apply {
                        put("path", candidate.absolutePath)
                        put("completed_at", System.currentTimeMillis() / 1000)
                    }
                    writeJsonAtomic(commit, candidateReceipt)
                    if (!part.renameTo(candidate)) throw IOException("落盘失败: $safeName")
                    candidate to candidateReceipt
                }
                destination = committed.first
                receipt = committed.second
                if (header.modifiedMs > 0) destination.setLastModified(header.modifiedMs)
            }

            writeJsonAtomic(done, receipt)
            if (isFolder) part.delete()
            meta.delete()
            commit.delete()
            dout.writeByte(WHPP.ACK_OK)
            dout.write(actualDigest)
            dout.flush()
            ackSent = true
            ok = true
            listener.onFileReceived(destination.name, destination.absolutePath, header.transferId)
            listener.onStatus("已接收并校验：${destination.name}")
        } catch (_: EOFException) {
            if (transferStarted) listener.onStatus("接收中断，已保留续传进度：$receivedName")
        } catch (_: SocketTimeoutException) {
            if (transferStarted) listener.onStatus("接收中断，已保留续传进度：$receivedName")
        } catch (_: java.net.SocketException) {
            if (transferStarted) listener.onStatus("接收中断，已保留续传进度：$receivedName")
        } catch (error: Exception) {
            if (running) listener.onStatus("接收失败: ${error.message ?: "未知错误"}")
        } finally {
            folderPart?.deleteRecursively()
            if (transferStarted && !ackSent) {
                try {
                    conn.getOutputStream().apply { write(WHPP.ACK_FAIL); flush() }
                } catch (_: IOException) {}
            }
            try { conn.close() } catch (_: IOException) {}
            if (checkpointClaimed) checkpointGate.release(checkpointId)
            if (transferStarted) listener.onTransferEnded("recv", receivedName, ok)
        }
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

    private fun hashInput(inputFactory: () -> InputStream,
                          cancellationRequested: () -> Boolean): String {
        val digest = MessageDigest.getInstance("SHA-256")
        useSendInput(inputFactory) { input ->
            val buffer = ByteArray(WHPP.BUFFER_SIZE)
            while (true) {
                if (cancellationRequested()) throw InterruptedIOException("发送已取消")
                val count = input.read(buffer)
                if (count < 0) break
                if (count > 0) digest.update(buffer, 0, count)
            }
        }
        return digest.digest().hex()
    }

    private fun outgoingTransferId(peer: Peer, name: String, size: Long,
                                   digest: String): Pair<String, String> {
        val identity = listOf(name, size.toString(), digest,
            peer.instanceId.ifEmpty { "${peer.host}:${peer.port}" }).joinToString("\u0000")
        val key = MessageDigest.getInstance("SHA-256")
            .digest(identity.toByteArray(Charsets.UTF_8)).hex()
        val prefs = context.getSharedPreferences("inkhole_outgoing_v3", Context.MODE_PRIVATE)
        var transferId = prefs.getString(key, "").orEmpty().lowercase()
        if (!validSha256(transferId)) {
            transferId = ByteArray(32).also(SecureRandom()::nextBytes).hex()
            prefs.edit().putString(key, transferId).commit()
        }
        return key to transferId
    }

    private fun completeOutgoingTransfer(key: String) {
        context.getSharedPreferences("inkhole_outgoing_v3", Context.MODE_PRIVATE)
            .edit().remove(key).apply()
    }

    private fun skipExactly(input: InputStream, size: Long,
                            cancellationRequested: () -> Boolean) {
        val buffer = ByteArray(WHPP.BUFFER_SIZE)
        var remaining = size
        while (remaining > 0) {
            if (cancellationRequested()) throw InterruptedIOException("发送已取消")
            val count = input.read(buffer, 0, minOf(buffer.size.toLong(), remaining).toInt())
            if (count < 0) throw EOFException("续传源数据不完整")
            remaining -= count
        }
    }

    private fun readControl(socket: Socket, input: InputStream, size: Int,
                            cancellationRequested: () -> Boolean,
                            timeoutMs: Long): ByteArray {
        val result = ByteArray(size)
        var offset = 0
        val deadline = System.currentTimeMillis() + timeoutMs
        while (offset < size) {
            if (cancellationRequested()) throw InterruptedIOException("发送已取消")
            val remaining = deadline - System.currentTimeMillis()
            if (remaining <= 0) throw SocketTimeoutException()
            socket.soTimeout = minOf(500L, remaining).toInt()
            try {
                val count = input.read(result, offset, size - offset)
                if (count < 0) throw EOFException("控制帧不完整")
                offset += count
            } catch (_: SocketTimeoutException) {
                // Re-check cancellation and the overall deadline.
            }
        }
        return result
    }

    /** Resumable WHPP v3 sender. Every retry encrypts only the remaining plaintext. */
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
        val peer = synchronized(peersLock) { peers.values.find { it.name == selected } }
            ?: run { listener.onStatus("目标设备已离线"); return false }
        val expectedReceiverFingerprint = peer.identityFingerprint
        if (peer.transport in setOf("lan", "tailscale") &&
            (!peer.instanceId.matches(Regex("[0-9a-f]{32}")) ||
                !validSha256(expectedReceiverFingerprint))) {
            listener.onStatus("接收设备身份尚未验证")
            return false
        }
        val transferName = ReceiveFiles.safeName(displayName)
        var completed = false

        fun cancellationRequested(): Boolean =
            sendCancelled.get() || shouldCancel?.invoke() == true

        sendCancelled.set(false)
        sendInProgress.set(true)
        listener.onSendingChanged(true)
        try {
            listener.onStatus("正在校验：$transferName")
            val digest = hashInput(inputFactory, ::cancellationRequested)
            val (outgoingKey, transferId) = synchronized(outgoingStateLock) {
                outgoingTransferId(peer, transferName, plainSize, digest)
            }
            val encrypted = secret.isNotEmpty()
            val header = WHPP.Header(
                filename = transferName,
                plainSize = plainSize,
                transferId = transferId,
                sha256 = digest,
                encrypted = encrypted,
                encMode = if (encrypted) "chunked" else "",
                senderInstanceId = instanceId,
                senderPublicKey = deviceIdentity.publicKey,
            )
            var lastError: Exception? = null

            repeat(3) { attempt ->
                if (cancellationRequested()) throw InterruptedIOException("发送已取消")
                var socket: Socket? = null
                try {
                    socket = connectToPeer(peer, attempt)
                    activeSendSocket.set(socket)
                    activeSockets.add(socket)
                    val output = BufferedOutputStream(socket.getOutputStream(), WHPP.BUFFER_SIZE)
                    val input = BufferedInputStream(socket.getInputStream(), WHPP.BUFFER_SIZE)
                    val dout = DataOutputStream(output)
                    WHPP.writeHeader(output, header)

                    val marker = readControl(socket, input, 1,
                        ::cancellationRequested, 60_000)[0].toInt() and 0xff
                    if (marker == WHPP.ACK_FAIL) throw ReceiverRejectedException("接收方拒绝了传输")
                    if (marker != WHPP.RESUME) throw IOException("接收方未返回 WHPP v3 续传状态")
                    val offset = ByteBuffer.wrap(readControl(
                        socket, input, 8, ::cancellationRequested, 60_000)).long
                    if (offset < 0 || offset > plainSize) throw IOException("接收方续传偏移非法")
                    val nonce = readControl(
                        socket, input, 32, ::cancellationRequested, 60_000)
                    val receiverInstanceId = readControl(
                        socket, input, 32, ::cancellationRequested, 60_000)
                        .toString(Charsets.US_ASCII).lowercase()
                    val receiverPublicSize = ByteBuffer.wrap(readControl(
                        socket, input, 2, ::cancellationRequested, 60_000))
                        .short.toInt() and 0xffff
                    if (receiverPublicSize !in 1..MAX_IDENTITY_FIELD) {
                        throw IOException("接收设备公钥大小非法")
                    }
                    val receiverPublic = readControl(
                        socket, input, receiverPublicSize,
                        ::cancellationRequested, 60_000).toString(Charsets.US_ASCII)
                    val receiverSignatureSize = ByteBuffer.wrap(readControl(
                        socket, input, 2, ::cancellationRequested, 60_000))
                        .short.toInt() and 0xffff
                    if (receiverSignatureSize !in 1..MAX_IDENTITY_FIELD) {
                        throw IOException("接收设备签名大小非法")
                    }
                    val receiverSignature = readControl(
                        socket, input, receiverSignatureSize,
                        ::cancellationRequested, 60_000).toString(Charsets.US_ASCII)
                    val receiverFingerprint = try {
                        DeviceAuth.fingerprint(receiverPublic)
                    } catch (error: Exception) {
                        throw IOException("接收设备公钥无效", error)
                    }
                    if (!receiverInstanceId.matches(Regex("[0-9a-f]{32}")) ||
                        (peer.instanceId.isNotEmpty() && receiverInstanceId != peer.instanceId) ||
                        (expectedReceiverFingerprint.isNotEmpty() &&
                            receiverFingerprint != expectedReceiverFingerprint) ||
                        !DeviceAuth.verify(receiverPublic, DeviceAuth.receiverMessage(
                            nonce, header, offset, receiverInstanceId), receiverSignature)) {
                        throw IOException("接收设备身份验证失败")
                    }
                    val signature = deviceIdentity.sign(
                        DeviceAuth.transferMessage(nonce, header, offset))
                        .toByteArray(Charsets.US_ASCII)
                    dout.writeShort(signature.size)
                    dout.write(signature)
                    if (offset > 0) {
                        listener.onStatus("正在续传 $transferName · ${offset * 100 / maxOf(1, plainSize)}%")
                        listener.onProgress("send", transferName, offset, plainSize)
                    }

                    val remaining = plainSize - offset
                    val wireSize = if (encrypted && remaining > 0) {
                        Crypto.chunkedWireSize(remaining)
                    } else remaining
                    dout.writeLong(wireSize)
                    if (remaining > 0) {
                        useSendInput(inputFactory) { source ->
                            skipExactly(source, offset, ::cancellationRequested)
                            if (encrypted) {
                                val encryptor = Crypto.ChunkedEncryptor(secret)
                                dout.write(encryptor.streamHeader)
                                var sentWire = encryptor.streamHeader.size.toLong()
                                var sentPlain = 0L
                                val buffer = ByteArray(Crypto.CHUNK_SIZE)
                                while (sentPlain < remaining) {
                                    if (cancellationRequested()) {
                                        throw InterruptedIOException("发送已取消")
                                    }
                                    val wanted = minOf(buffer.size.toLong(), remaining - sentPlain).toInt()
                                    val count = readFull(source, buffer, wanted)
                                    if (count <= 0) throw EOFException("文件读取不完整")
                                    val ciphertext = encryptor.encryptChunk(buffer, count)
                                    dout.writeInt(ciphertext.size)
                                    dout.write(ciphertext)
                                    sentPlain += count
                                    sentWire += 4 + ciphertext.size
                                    listener.onProgress(
                                        "send", transferName, offset + sentPlain, plainSize)
                                }
                                if (sentWire != wireSize) throw IOException("加密发送大小不一致")
                            } else {
                                val buffer = ByteArray(WHPP.BUFFER_SIZE)
                                var sent = 0L
                                while (sent < remaining) {
                                    if (cancellationRequested()) {
                                        throw InterruptedIOException("发送已取消")
                                    }
                                    val count = source.read(
                                        buffer, 0, minOf(buffer.size.toLong(), remaining - sent).toInt())
                                    if (count < 0) throw EOFException("文件读取不完整")
                                    dout.write(buffer, 0, count)
                                    sent += count
                                    listener.onProgress("send", transferName, offset + sent, plainSize)
                                }
                            }
                        }
                    }
                    dout.flush()

                    val ack = readControl(socket, input, 1,
                        ::cancellationRequested, RECV_IDLE_TIMEOUT_MS.toLong())[0].toInt() and 0xff
                    if (ack == WHPP.ACK_FAIL) {
                        throw ReceiverRejectedException("接收方校验或落盘失败")
                    }
                    if (ack != WHPP.ACK_OK) throw IOException("未收到接收成功回执")
                    val remoteDigest = readControl(
                        socket, input, WHPP.DIGEST_SIZE, ::cancellationRequested, 60_000)
                    val expectedDigest = digest.chunked(2)
                        .map { it.toInt(16).toByte() }.toByteArray()
                    if (!MessageDigest.isEqual(remoteDigest, expectedDigest)) {
                        throw IOException("接收方 SHA-256 回执不一致")
                    }
                    synchronized(outgoingStateLock) { completeOutgoingTransfer(outgoingKey) }
                    completed = true
                    return true
                } catch (error: ReceiverRejectedException) {
                    throw error
                } catch (error: Exception) {
                    lastError = error
                    if (cancellationRequested()) throw InterruptedIOException("发送已取消")
                    if (attempt < 2) {
                        listener.onStatus("连接中断，正在恢复传输（${attempt + 2}/3）")
                        Thread.sleep(500L * (attempt + 1))
                    }
                } finally {
                    socket?.let {
                        activeSendSocket.compareAndSet(it, null)
                        activeSockets.remove(it)
                        try { it.close() } catch (_: IOException) {}
                    }
                }
            }
            throw lastError ?: IOException("传输连接失败")
        } catch (error: Exception) {
            if (cancellationRequested()) listener.onStatus("已取消发送：$transferName")
            else listener.onStatus("发送失败: ${error.message}")
            return false
        } finally {
            activeSendSocket.set(null)
            activeSendInput.set(null)
            sendInProgress.set(false)
            sendCancelled.set(false)
            listener.onSendingChanged(false)
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

    private fun connectToPeer(peer: Peer, routeOffset: Int = 0): Socket {
        var lastError: Exception? = null
        val candidates = synchronized(peersLock) {
            transferRouteCandidates(peer, peers.values.toList())
        }
        val offset = if (candidates.size > 1) routeOffset % candidates.size else 0
        val routes = candidates.drop(offset) + candidates.take(offset)
        for (route in routes) {
            val targets = (listOf(route.host) + route.hosts)
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
                    socket.connect(java.net.InetSocketAddress(address, route.port), 15_000)
                    if (!running) {
                        socket.close()
                        throw IOException("墨洞节点已停止")
                    }
                    if (route.endpointToken.isNotEmpty()) {
                        socket.getOutputStream().apply {
                            write(AUTH_MAGIC)
                            write(route.endpointToken.toByteArray(Charsets.US_ASCII))
                            flush()
                        }
                    }
                    if (route !== peer) {
                        listener.onStatus(
                            "当前通道不可用，已切换至${transferRouteLabel(route)}")
                    }
                    return socket
                } catch (e: Exception) {
                    lastError = e
                    try { socket.close() } catch (_: IOException) {}
                }
            }
        }
        throw lastError ?: IOException("目标设备没有可用地址")
    }

    // ---- 对端管理 ----

    fun getPeers(): List<Peer> = synchronized(peersLock) { peers.values.toList().sortedBy { it.name } }

    /** 实际监听端口(0=尚未启动)。设置页展示"本机"信息用。 */
    fun getActualPort(): Int = actualPort

    /** Activity 回前台时用于确认厂商系统没有只终止前台服务的监听层。 */
    fun isReady(): Boolean = running && actualPort > 0 && serverSocket?.isClosed == false

    fun getInstanceId(): String = instanceId

    /** 仅交给同进程 Go 核心，用于认证其回注到接收端口的连接。 */
    fun getCoreIngressToken(): String = coreIngressToken

    fun selectPeer(name: String?) {
        selectedPeer = name
        // 智能保留：记住 serviceName，离线后重新上线能自动恢复选中
        val selected = if (name != null) {
            synchronized(peersLock) { peers.values.find { it.name == name } }
        } else null
        lastSelectedService = selected?.serviceName
        listener.onStatus(if (name != null) "目标: $name" else "未选择目标")
    }

    internal fun pendingCompletedTransfers(): List<CompletedTransfer> =
        CompletedTransfers.pending(inboxDir)


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
                        capabilities: Set<String> = emptySet(), manual: Boolean = false,
                        transport: String = if (manual) "tailscale" else "lan",
                        endpointToken: String = "", publicKey: String = "",
                        identityFingerprint: String = "") {
        var added = false
        var finalName: String
        synchronized(peersLock) {
            val baseName = ReceiveFiles.utf8Prefix(
                displayName.filterNot { it.isISOControl() }.trim(),
                200,
            ).ifBlank { host }
            fun uniqueName(ignoredKey: String = serviceName): String {
                var candidate = baseName
                var n = 2
                while (peers.any { (key, peer) ->
                        key != ignoredKey && peer.name == candidate
                    }) {
                    candidate = "$baseName (${n++})"
                }
                return candidate
            }
            val existingEntry = peers.entries.firstOrNull { it.key == serviceName }
                ?: if (instanceId.isNotEmpty() && !manual) {
                    peers.entries.firstOrNull { (_, peer) ->
                        peer.transport == "lan" && peer.instanceId == instanceId
                    }
                } else null
            val existingKey = existingEntry?.key
            val existing = existingEntry?.value
            if (existing != null && existingKey != null) {
                // 同一服务重新解析：同步地址和对端改名，并保持选中状态。
                finalName = uniqueName(existingKey)
                peers[existingKey] = existing.copy(
                    name = finalName,
                    host = host,
                    port = port,
                    hosts = hosts,
                    instanceId = instanceId,
                    capabilities = capabilities,
                    manual = manual,
                    transport = transport,
                    endpointToken = endpointToken,
                    publicKey = publicKey,
                    identityFingerprint = identityFingerprint,
                )
                if (selectedPeer == existing.name &&
                    lastSelectedService == existing.serviceName) {
                    selectedPeer = finalName
                }
            } else {
                // 不同设备撞了显示名：给后来者加 " (2)" 后缀
                finalName = uniqueName()
                peers[serviceName] = Peer(
                    finalName, host, port, serviceName, hosts,
                    instanceId, capabilities, manual, transport, endpointToken,
                    publicKey, identityFingerprint)
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

    /** 将共享传输核心暴露的已认证 loopback 端点加入普通发送目标列表。 */
    fun upsertExternalPeer(
        peerId: String,
        name: String,
        host: String,
        port: Int,
        transport: String,
        endpointToken: String,
        externalInstanceId: String = "",
    ): String {
        val normalizedId = peerId.trim()
        val normalizedTransport = transport.trim().lowercase()
        require(normalizedId.isNotEmpty() && normalizedTransport in setOf("wormhole", "ssh")) {
            "跨网设备标识或通道无效"
        }
        require(host in setOf("127.0.0.1", "::1", "localhost")) {
            "跨网核心端点必须位于本机"
        }
        require(port in 1..65535 && endpointToken.isNotEmpty()) { "跨网核心端点无效" }
        val serviceName = "external|$normalizedTransport|$normalizedId"
        addPeer(
            serviceName = serviceName,
            displayName = name.trim().ifEmpty {
                if (normalizedTransport == "wormhole") "一次性接收端" else "SSH 设备"
            },
            host = host,
            port = port,
            hosts = listOf(host),
            instanceId = externalInstanceId.lowercase(),
            capabilities = setOf(WHPP.FOLDER_KIND),
            manual = true,
            transport = normalizedTransport,
            endpointToken = endpointToken,
        )
        return synchronized(peersLock) { peers[serviceName]?.name.orEmpty() }
    }

    fun removeExternalPeer(peerId: String, transport: String) {
        removePeer("external|${transport.trim().lowercase()}|${peerId.trim()}")
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
                    val hosts = LanReachability.verifiedPeerCandidates(
                        (listOf(peer.host) + peer.hosts).distinct(),
                        currentLanLinks(),
                        peer.host,
                    )
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
    @Suppress("DEPRECATION")
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
                    val nonce = ByteArray(32).also(SecureRandom()::nextBytes)
                    it.getOutputStream().apply {
                        write(WHPP.CAP_MAGIC)
                        write(nonce)
                        flush()
                    }
                    val capabilities = WHPP.readCapabilities(it.getInputStream(), nonce)
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
                        capabilities.publicKey,
                        capabilities.fingerprint,
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
            setAttribute("identity", deviceIdentity.fingerprint)
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
        val attrs = try {
            info.attributes.mapValues { (_, value) -> value.toString(Charsets.UTF_8) }
        } catch (_: Exception) {
            emptyMap()
        }
        handleResolvedRecord(discoveryName, info.port, resolvedHosts.toList(), attrs)
    }

    private fun handleResolvedRecord(
        discoveryName: String,
        port: Int,
        resolvedValues: List<String>,
        attrs: Map<String, String>,
    ) {
        if (!running || port !in 1..65535) return
        val resolvedHosts = LinkedHashSet<String>()
        resolvedValues.map(String::trim).filter(String::isNotEmpty).let(resolvedHosts::addAll)
        val host = resolvedHosts.firstOrNull() ?: return
        val txtInstanceId = attrs["instance_id"]?.lowercase()
            ?.takeIf { it.matches(Regex("[0-9a-f]{32}")) } ?: return
        if (attrs["whpc"] != WHPP.CAP_VERSION.toString()) return
        if (txtInstanceId == instanceId) return
        val displayName = attrs["peer_name"]?.takeIf { it.isNotBlank() }
            ?: discoveryName
        // 兜底自我过滤：同名 + 地址是本机 IP，判定为自己的历史注册(旧 instanceId、
        // goodbye 丢包残留)，丢弃不显示。
        if (displayName == peerName && host in localIps()) return
        // 对端全部地址：TXT ips(桌面端宣告,多网卡/VPN 全覆盖) + API34 hostAddresses
        val hosts = LinkedHashSet<String>()
        hosts.addAll(resolvedHosts)
        attrs["ips"]?.split(",")
            ?.map { it.trim() }?.filter { it.isNotEmpty() }?.let { hosts.addAll(it) }
        val hostList = hosts.toList()
        // 发现与更新都先做 WHPC v3 身份验证。系统 mDNS 缓存(Android 13+ 常驻缓存,
        // 对端崩溃/断网不发 goodbye 时记录可存活几十分钟)会在重启发现时立即
        // 回灌陈旧记录——探活循环刚剔除的下线设备下一秒又被 resolve"复活",
        // 表现为对端明明关了却一直显示在线。可达性标准与探活循环一致:仅认
        // 当前 WiFi/以太网可达的地址,不给 Tailscale 等 VPN 路径兜底的机会。
        if (!pendingDiscoveryProbes.add(discoveryName)) return
        scope.launch {
            try {
                val candidates = LanReachability.discoveryCandidates(
                    resolvedHosts.toList(), hostList, currentLanLinks())
                val result = try {
                    probePeer(candidates, port, LOST_PROBE_TIMEOUT_MS, txtInstanceId)
                } catch (_: Exception) {
                    return@launch
                }
                if (running) addPeer(
                    discoveryName, displayName, result.connectedAddress, port,
                    hostList, result.instanceId, result.capabilities, manual = false,
                    publicKey = result.publicKey, identityFingerprint = result.fingerprint)
            } finally {
                pendingDiscoveryProbes.remove(discoveryName)
            }
        }
    }

    private fun handleLanAnnouncement(host: String, announcement: LanAnnouncement) {
        if (!running || announcement.instanceId == instanceId) return
        val alreadyKnown = synchronized(peersLock) {
            peers.values.any { peer ->
                peer.transport == "lan" && peer.instanceId == announcement.instanceId &&
                    peer.host == host && peer.port == announcement.port
            }
        }
        if (alreadyKnown) return
        // Limit unauthenticated broadcast hints to one in-flight probe per source address.
        val pendingKey = "broadcast-source|$host"
        if (!pendingDiscoveryProbes.add(pendingKey)) return
        scope.launch {
            try {
                val candidates = LanReachability.discoveryCandidates(
                    listOf(host), listOf(host), currentLanLinks())
                val result = try {
                    probePeer(
                        candidates,
                        announcement.port,
                        LOST_PROBE_TIMEOUT_MS,
                        announcement.instanceId,
                    )
                } catch (_: Exception) {
                    return@launch
                }
                if (running) addPeer(
                    "broadcast|${result.instanceId}",
                    result.peerName,
                    result.connectedAddress,
                    announcement.port,
                    listOf(result.connectedAddress),
                    result.instanceId,
                    result.capabilities,
                    manual = false,
                    publicKey = result.publicKey,
                    identityFingerprint = result.fingerprint,
                )
            } finally {
                pendingDiscoveryProbes.remove(pendingKey)
            }
        }
    }

    /** A peer that can already reach Android may still be invisible in the reverse
     * direction when a phone hotspot blocks multicast and broadcast forwarding.
     * Treat the hint only as an address candidate: the signed WHPC probe below must
     * succeed with the claimed instance ID before the peer reaches the UI. */
    private fun handleLanHint(host: String, hintedInstanceId: String, port: Int) {
        if (!running || hintedInstanceId == instanceId ||
            !LanReachability.isDirectLanAddress(host)) return
        val pendingKey = "hint|$hintedInstanceId|$host|$port"
        if (!pendingDiscoveryProbes.add(pendingKey)) return
        scope.launch {
            try {
                val result = try {
                    probePeer(
                        listOf(host),
                        port,
                        LOST_PROBE_TIMEOUT_MS,
                        hintedInstanceId,
                    )
                } catch (_: Exception) {
                    return@launch
                }
                if (running) addPeer(
                    "hint|${result.instanceId}",
                    result.peerName,
                    result.connectedAddress,
                    port,
                    listOf(result.connectedAddress),
                    result.instanceId,
                    result.capabilities,
                    manual = false,
                    publicKey = result.publicKey,
                    identityFingerprint = result.fingerprint,
                )
            } finally {
                pendingDiscoveryProbes.remove(pendingKey)
            }
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

    private fun lanNetworkInterfaces(): List<NetworkInterface> = try {
        val enumeration = NetworkInterface.getNetworkInterfaces()
        val interfaces = if (enumeration == null) emptyList()
            else java.util.Collections.list(enumeration)
        interfaces.filter { networkInterface ->
            try {
                networkInterface.isUp && !networkInterface.isLoopback &&
                    !networkInterface.isPointToPoint &&
                    LanReachability.isLanInterfaceName(networkInterface.name.orEmpty())
            } catch (_: Exception) {
                false
            }
        }
    } catch (_: Exception) {
        emptyList()
    }

    private fun currentJmDnsBindAddresses(): List<InetAddress> = lanNetworkInterfaces()
        .flatMap { networkInterface ->
            java.util.Collections.list(networkInterface.inetAddresses)
        }
        .filterIsInstance<Inet4Address>()
        .filter { address ->
            val host = address.hostAddress.orEmpty()
            !address.isAnyLocalAddress && !address.isLoopbackAddress &&
                !address.isMulticastAddress && !TailnetAddress.isTailnet(host)
        }
        .distinctBy { it.hostAddress }

    private fun restartJmDnsDiscovery(force: Boolean = false) {
        if (!running) return
        jmDnsDiscovery?.restart(currentJmDnsBindAddresses(), force)
    }

    /** 当前真正的局域网链路；排除蜂窝网络和 Tailscale 等 VPN transport。 */
    @Suppress("DEPRECATION")
    private fun currentLanLinks(): List<LanLink> {
        val connectivityLinks = try {
            val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as? ConnectivityManager
            if (manager == null) {
                emptyList()
            } else {
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
            }
        } catch (_: Exception) {
            emptyList()
        }
        // 热点提供者的 SoftAP 接口通常不出现在 ConnectivityManager.allNetworks
        // 中；从 NetworkInterface 补齐 wlan1/ap0 等真实本地接口。点对点、蜂窝
        // 和隧道接口必须排除，避免把 VPN/代理地址重新当成局域网路径。
        val interfaceLinks = try {
            lanNetworkInterfaces().flatMap { networkInterface ->
                networkInterface.interfaceAddresses.mapNotNull { binding ->
                    val address = binding.address ?: return@mapNotNull null
                    val host = address.hostAddress ?: return@mapNotNull null
                    if (address.isAnyLocalAddress || address.isLoopbackAddress ||
                        address.isMulticastAddress || TailnetAddress.isTailnet(host)) null
                    else LanLink(host, binding.networkPrefixLength.toInt())
                }
            }
        } catch (_: Exception) {
            emptyList()
        }
        return (connectivityLinks + interfaceLinks).distinct()
    }

    private fun lanLinkSignature(links: List<LanLink>): String = links
        .map { "${it.address.substringBefore('%')}/${it.prefixLength}" }
        .sorted()
        .joinToString("|")

    private fun discoverNsd() {
        if (!running) return
        val nsd = nsdManager ?: return
        val listener = object : NsdManager.DiscoveryListener {
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
        discoveryListener = listener
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            // Track every non-VPN network instead of only the current/default network. This is
            // the Android-recommended overload for WiFi reconnects and local-only networks.
            val request = NetworkRequest.Builder()
                .clearCapabilities()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
                .build()
            nsd.discoverServices(
                SERVICE_TYPE,
                NsdManager.PROTOCOL_DNS_SD,
                request,
                context.mainExecutor,
                listener,
            )
        } else {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        }
    }
}

internal class CheckpointGate {
    private val monitor = Object()
    private val active = HashSet<String>()

    fun acquire(transferId: String, timeoutMillis: Long): Boolean {
        val deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMillis)
        synchronized(monitor) {
            while (!active.add(transferId)) {
                val remainingNanos = deadline - System.nanoTime()
                if (remainingNanos <= 0) return false
                monitor.wait(
                    TimeUnit.NANOSECONDS.toMillis(remainingNanos).coerceAtLeast(1),
                )
            }
            return true
        }
    }

    fun release(transferId: String) {
        synchronized(monitor) {
            if (active.remove(transferId)) monitor.notifyAll()
        }
    }

    fun isActive(transferId: String): Boolean = synchronized(monitor) {
        transferId in active
    }
}
