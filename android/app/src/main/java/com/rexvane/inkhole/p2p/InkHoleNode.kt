package com.rexvane.inkhole.p2p

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import kotlinx.coroutines.*
import java.io.*
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.UUID
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicBoolean

/** 检测到设备/收到文件/状态变化时的回调。 */
interface InkHoleListener {
    fun onPeerChanged(peers: List<Peer>)
    fun onFileReceived(filename: String, path: String)
    fun onStatus(msg: String)
    /** 传输进度。kind = "send"/"recv"；节流后最多约 4 次/秒。 */
    fun onProgress(kind: String, filename: String, done: Long, total: Long) {}
}

data class Peer(
    val name: String,           // 显示名(重名设备带 " (2)" 后缀)
    val host: String,
    val port: Int,
    val serviceName: String = "",  // NSD 服务实例名(唯一，用于离线精确匹配)
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
    private val listener: InkHoleListener,
) {
    companion object {
        private const val SERVICE_TYPE = "_inkhole._tcp."
        private const val DISK_MARGIN = 256L * 1024 * 1024   // 收完至少还要剩这么多
        private const val PROGRESS_INTERVAL_MS = 250L
        private const val CHUNK_ENC_THRESHOLD = 32L * 1024 * 1024  // 超过走 WHE2 分块
        private const val DRAIN_CAP = 8L * 1024 * 1024       // 拒收时最多帮对端消化的字节数
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var nsdManager: NsdManager? = null
    private var serverSocket: ServerSocket? = null
    private var actualPort = 0
    // 唯一实例 ID 持久化到 SharedPreferences：同一台设备无论 App/前台服务重启
    // 多少次都用同一个服务名，避免旧注册变成永不消失的"幽灵设备"。
    private val instanceId = loadOrCreateInstanceId(context)

    private fun loadOrCreateInstanceId(ctx: Context): String {
        val prefs = ctx.getSharedPreferences("inkhole", Context.MODE_PRIVATE)
        prefs.getString("instance_id", null)?.takeIf { it.isNotEmpty() }?.let { return it }
        val id = UUID.randomUUID().toString().replace("-", "").take(8)
        prefs.edit().putString("instance_id", id).apply()
        return id
    }

    // serviceName(唯一) -> Peer
    private val peers = LinkedHashMap<String, Peer>()
    private val peersLock = Any()
    @Volatile private var selectedPeer: String? = null   // 显示名
    @Volatile private var running = false
    /** 系统实际注册下来的服务名(冲突时可能被系统改名)，用于"不发现自己"。 */
    @Volatile private var registeredName: String? = null

    // ---- 生命周期 ----

    fun start() {
        if (running) return
        running = true
        inboxDir.mkdirs()
        startTcpServer()
        registerNsd()
        discoverNsd()
        listener.onStatus("墨洞已开启 · $peerName")
    }

    fun stop() {
        running = false
        // 注册/发现可能从未成功，注销时系统会抛 IllegalArgumentException——不能让退出流程崩掉
        nsdManager?.let { nsd ->
            try { discoveryListener?.let { nsd.stopServiceDiscovery(it) } } catch (_: Exception) {}
            try { registrationListener?.let { nsd.unregisterService(it) } } catch (_: Exception) {}
        }
        try { serverSocket?.close() } catch (_: IOException) {}
        scope.cancel()
        synchronized(peersLock) { peers.clear() }
        listener.onPeerChanged(emptyList())
    }

    // ---- TCP 服务器 ----

    private fun startTcpServer() {
        val server = try {
            ServerSocket(0).also { actualPort = it.localPort } // OS 自动分配端口
        } catch (e: IOException) {
            listener.onStatus("TCP 启动失败: ${e.message}")
            return
        }
        serverSocket = server
        scope.launch {
            while (running) {
                try {
                    val conn = server.accept()
                    launch { handleConnection(conn) }
                } catch (e: IOException) {
                    if (running) listener.onStatus("接收连接失败: ${e.message}")
                    break
                }
            }
        }
    }

    private fun handleConnection(conn: Socket) {
        var partFile: File? = null
        var wantAck = false
        var ok = false
        try {
            // 仅接收目标设备：来源 IP 不是当前选中设备就直接拒
            if (trustedOnly) {
                val src = conn.inetAddress?.hostAddress
                val sel = selectedPeer?.let { s ->
                    synchronized(peersLock) { peers.values.find { it.name == s } }
                }
                if (sel == null || src != sel.host) {
                    listener.onStatus("已拒收 ${src ?: "?"} 的连接（仅接收目标设备）")
                    try {
                        conn.getOutputStream().apply { write(WHPP.ACK_FAIL); flush() }
                    } catch (_: IOException) {}
                    drain(conn.getInputStream(), DRAIN_CAP)
                    return
                }
            }

            val input = BufferedInputStream(conn.getInputStream())
            val header = WHPP.readHeader(input)
            wantAck = header.wantAck

            // basename 防路径穿越
            val safeName = File(header.filename).name.ifEmpty { "unknown" }

            // size 来自网络，不可信
            if (header.size < 0 || header.size > WHPP.MAX_FILE_SIZE) {
                listener.onStatus("拒收 $safeName：文件大小非法")
                return
            }
            if (header.size + DISK_MARGIN > inboxDir.usableSpace) {
                listener.onStatus("拒收 $safeName：存储空间不足")
                drain(input, minOf(header.size, DRAIN_CAP))
                return
            }
            if (header.encrypted && secret.isEmpty()) {
                listener.onStatus("拒收 $safeName：对方启用了加密，本机未设口令")
                drain(input, minOf(header.size, DRAIN_CAP))
                return
            }

            val dst = File(inboxDir, safeName)
            partFile = File(inboxDir, "$safeName.part")
            var lastReport = 0L
            fun report(done: Long) {
                val now = System.currentTimeMillis()
                if (done >= header.size || now - lastReport >= PROGRESS_INTERVAL_MS) {
                    lastReport = now
                    listener.onProgress("recv", safeName, done, header.size)
                }
            }

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
                FileOutputStream(partFile).use { fout ->
                    while (consumed < header.size) {
                        val ctLen = try { din.readInt() } catch (_: IOException) { intact = false; break }
                        if (ctLen < 16 || ctLen > Crypto.CHUNK_SIZE + 16) { intact = false; break }
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
                FileOutputStream(partFile).use { fout ->
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
                    // 对端中途断连：半截文件绝不能顶着完整文件名落盘
                    if (remaining > 0) {
                        listener.onStatus("接收中断: $safeName")
                        return
                    }
                }

                // WHE1 整块加密：解密成功才算收到
                if (header.encrypted) {
                    val blob = partFile.readBytes()
                    val plain = Crypto.decrypt(secret, blob)
                    if (plain == null) {
                        listener.onStatus("解密失败: $safeName（两端口令不一致？）")
                        return
                    }
                    partFile.writeBytes(plain)
                }
            }

            // 原子改名 (覆盖同名)
            if (dst.exists()) dst.delete()
            if (!partFile.renameTo(dst)) {
                listener.onStatus("落盘失败: $safeName")
                return
            }
            partFile = null
            ok = true

            listener.onFileReceived(safeName, dst.absolutePath)
            listener.onStatus("收到: $safeName")
        } catch (e: Exception) {
            listener.onStatus("接收失败: ${e.message}")
        } finally {
            if (wantAck) {
                try {
                    conn.getOutputStream().apply {
                        write(if (ok) WHPP.ACK_OK else WHPP.ACK_FAIL)
                        flush()
                    }
                } catch (_: IOException) {}
            }
            try { conn.close() } catch (_: IOException) {}
            partFile?.delete()
        }
    }

    /** 拒收时把对端已发出的最多 n 字节读掉再关连接——
     *  不读就 close 会触发 RST，可能冲掉已排队的失败回执。 */
    private fun drain(input: InputStream, n: Long) {
        try {
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

    fun sendFile(filePath: String): Boolean {
        val file = File(filePath)
        if (!file.isFile) {
            listener.onStatus("文件不存在")
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

        return try {
            val socket = Socket()
            socket.connect(java.net.InetSocketAddress(peer.host, peer.port), 15_000)
            socket.use { s ->
                val out = BufferedOutputStream(s.getOutputStream())
                var lastReport = 0L
                val progress: (Long, Long) -> Unit = { done, total ->
                    val now = System.currentTimeMillis()
                    if (done >= total || now - lastReport >= PROGRESS_INTERVAL_MS) {
                        lastReport = now
                        listener.onProgress("send", file.name, done, total)
                    }
                }

                if (secret.isNotEmpty() && file.length() > CHUNK_ENC_THRESHOLD) {
                    // 大文件走 WHE2 分块流式加密：内存峰值 4MB
                    val wireSize = Crypto.chunkedWireSize(file.length())
                    WHPP.writeHeader(out, WHPP.Header(
                        file.name, wireSize, encrypted = true, wantAck = true,
                        encMode = "chunked"))
                    val enc = Crypto.ChunkedEncryptor(secret)
                    out.write(enc.streamHeader)
                    var sent = enc.streamHeader.size.toLong()
                    val dout = DataOutputStream(out)
                    file.inputStream().use { fin ->
                        val buf = ByteArray(Crypto.CHUNK_SIZE)
                        while (true) {
                            val n = readFull(fin, buf)
                            if (n <= 0) break
                            val ct = enc.encryptChunk(buf, n)
                            dout.writeInt(ct.size)
                            dout.write(ct)
                            sent += 4 + ct.size
                            progress(sent, wireSize)
                        }
                    }
                    dout.flush()
                } else if (secret.isNotEmpty()) {
                    // 小文件 WHE1 整块(与所有旧版本互通)
                    val plain = file.readBytes()
                    val enc = Crypto.encrypt(secret, plain)
                    WHPP.writeFrame(
                        out, file.name, enc.size.toLong(), true,
                        ByteArrayInputStream(enc)
                    ) { progress(it, enc.size.toLong()) }
                } else {
                    // 明文: 流式
                    WHPP.writeFrame(
                        out, file.name, file.length(), false,
                        file.inputStream()
                    ) { progress(it, file.length()) }
                }

                // 等接收方回执。老版本对端读完即关连接 -> read 返回 -1，按成功；超时也不误报
                s.soTimeout = 60_000
                val resp = try { s.getInputStream().read() } catch (_: SocketTimeoutException) { -1 }
                if (resp == WHPP.ACK_FAIL) {
                    listener.onStatus("${peer.name} 接收失败（口令不一致、被拒收或存储问题）")
                    return false
                }
            }
            listener.onStatus("已发送: ${file.name}")
            true
        } catch (e: Exception) {
            listener.onStatus("发送失败: ${e.message}")
            false
        }
    }

    /** 尽量读满 buf(文件尾可能不足)，返回实际读到的字节数；EOF 返回 -1。 */
    private fun readFull(input: InputStream, buf: ByteArray): Int {
        var off = 0
        while (off < buf.size) {
            val n = input.read(buf, off, buf.size - off)
            if (n < 0) break
            off += n
        }
        return if (off == 0) -1 else off
    }

    // ---- 对端管理 ----

    fun getPeers(): List<Peer> = synchronized(peersLock) { peers.values.toList().sortedBy { it.name } }

    fun selectPeer(name: String?) {
        selectedPeer = name
        listener.onStatus(if (name != null) "目标: $name" else "未选择目标")
    }

    fun getSelectedPeer(): String? = selectedPeer

    private fun addPeer(serviceName: String, displayName: String, host: String, port: Int) {
        var added = false
        synchronized(peersLock) {
            val existing = peers[serviceName]
            if (existing != null) {
                // 同一服务重新解析(IP 变化)：原地更新
                peers[serviceName] = existing.copy(host = host, port = port)
            } else {
                // 不同设备撞了显示名：给后来者加 " (2)" 后缀
                var name = displayName
                var n = 2
                while (peers.values.any { it.name == name }) {
                    name = "$displayName (${n++})"
                }
                peers[serviceName] = Peer(name, host, port, serviceName)
                added = true
            }
        }
        if (added) listener.onStatus("发现: $displayName")
        listener.onPeerChanged(getPeers())
    }

    private fun removePeer(serviceName: String) {
        val removed = synchronized(peersLock) { peers.remove(serviceName) } ?: return
        if (selectedPeer == removed.name) selectedPeer = null
        listener.onPeerChanged(getPeers())
        listener.onStatus("${removed.name} 离线")
    }

    // ---- NSD 注册 ----

    private var registrationListener: NsdManager.RegistrationListener? = null
    private var discoveryListener: NsdManager.DiscoveryListener? = null

    private fun registerNsd() {
        nsdManager = context.getSystemService(Context.NSD_SERVICE) as NsdManager
        val info = NsdServiceInfo().apply {
            // 服务名带唯一后缀：两台同名手机(如同型号 Build.MODEL)不再撞名
            serviceName = "${peerName.replace(".", "-")}-$instanceId"
            serviceType = SERVICE_TYPE
            port = actualPort
            // 与桌面版 zeroconf 互通的 TXT 属性
            setAttribute("peer_name", peerName)
            setAttribute("instance_id", instanceId)
        }
        registrationListener = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(serviceInfo: NsdServiceInfo) {
                // 系统冲突改名后这里才是真实注册名，自我过滤必须用它
                registeredName = serviceInfo.serviceName
            }
            override fun onRegistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                listener.onStatus("NSD 注册失败: $errorCode")
            }
            override fun onServiceUnregistered(serviceInfo: NsdServiceInfo) {}
            override fun onUnregistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {}
        }
        nsdManager!!.registerService(info, NsdManager.PROTOCOL_DNS_SD, registrationListener)
    }

    // ---- NSD 发现 ----
    // Android 同一时刻只允许一个 resolveService 在跑，并发会 FAILURE_ALREADY_ACTIVE
    // (设备"时而发现不了"的经典原因)。这里排队逐个解析。

    private val resolveQueue = ConcurrentLinkedQueue<NsdServiceInfo>()
    private val resolving = AtomicBoolean(false)

    private fun enqueueResolve(serviceInfo: NsdServiceInfo) {
        resolveQueue.add(serviceInfo)
        drainResolveQueue()
    }

    private fun drainResolveQueue() {
        if (!resolving.compareAndSet(false, true)) return
        val next = resolveQueue.poll()
        if (next == null) {
            resolving.set(false)
            return
        }
        // 用"发现时"的服务名做对端表的 key：resolve 返回的名字转义可能不一致
        // (含空格等字符时)，而 onServiceLost 给的是发现时的名字
        val discoveryName = next.serviceName
        nsdManager?.resolveService(next, object : NsdManager.ResolveListener {
            override fun onServiceResolved(info: NsdServiceInfo) {
                handleResolved(discoveryName, info)
                resolving.set(false)
                drainResolveQueue()
            }
            override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
                if (errorCode == NsdManager.FAILURE_ALREADY_ACTIVE) {
                    resolveQueue.add(next)   // 稍后重试
                }
                resolving.set(false)
                drainResolveQueue()
            }
        }) ?: resolving.set(false)
    }

    private fun handleResolved(discoveryName: String, info: NsdServiceInfo) {
        val host = info.host?.hostAddress ?: return
        val attrs = try { info.attributes } catch (_: Exception) { emptyMap<String, ByteArray>() }
        val txtInstanceId = attrs["instance_id"]?.toString(Charsets.UTF_8)
        // 不添加自己：优先按实例 ID(可靠)；老版本对端无此属性，回退按注册名
        if (txtInstanceId == instanceId) return
        val displayName = attrs["peer_name"]?.toString(Charsets.UTF_8)?.takeIf { it.isNotBlank() }
            ?: discoveryName
        // 兜底自我过滤：同名 + 地址是本机 IP，判定为自己的历史注册(旧 instanceId、
        // goodbye 丢包残留)，丢弃不显示。
        if (displayName == peerName && host in localIps()) return
        addPeer(discoveryName, displayName, host, info.port)
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

    private fun discoverNsd() {
        discoveryListener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onDiscoveryStopped(serviceType: String) {}

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                // 跳过自己(用系统实际注册名比对；冲突改名场景由 resolve 后的 instance_id 兜底)
                if (serviceInfo.serviceName == (registeredName ?: "${peerName.replace(".", "-")}-$instanceId")) return
                enqueueResolve(serviceInfo)
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                removePeer(serviceInfo.serviceName)
            }

            override fun onStartDiscoveryFailed(serviceType: String?, errorCode: Int) {
                listener.onStatus("NSD 发现启动失败: $errorCode")
            }
            override fun onStopDiscoveryFailed(serviceType: String?, errorCode: Int) {}
        }
        nsdManager!!.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener)
    }
}
