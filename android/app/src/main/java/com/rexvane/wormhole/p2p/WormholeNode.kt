package com.rexvane.wormhole.p2p

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import kotlinx.coroutines.*
import java.io.*
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap

/** 检测到设备/收到文件/状态变化时的回调。 */
interface WormholeListener {
    fun onPeerChanged(peers: List<Peer>)
    fun onFileReceived(filename: String, path: String)
    fun onStatus(msg: String)
}

data class Peer(
    val name: String,
    val host: String,
    val port: Int,
)

/**
 * 虫洞 P2P 引擎 (Android 版)。
 *
 * - NSD (NsdManager) 注册/发现 _wormhole._tcp 服务, 与桌面版 zeroconf 互通。
 * - TCP ServerSocket 接收文件, WHPP 协议与桌面版一致。
 * - 可选 AES-256-GCM 端到端加密 (与桌面版 crypto.py 兼容)。
 */
class WormholeNode(
    private val context: Context,
    private val peerName: String,
    private val inboxDir: File,
    private val secret: String = "",
    private val listener: WormholeListener,
) {
    companion object {
        private const val SERVICE_TYPE = "_wormhole._tcp."
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var nsdManager: NsdManager? = null
    private var serverSocket: ServerSocket? = null
    private var actualPort = 0
    private val peers = ConcurrentHashMap<String, Peer>()
    @Volatile private var selectedPeer: String? = null
    @Volatile private var running = false

    // ---- 生命周期 ----

    fun start() {
        if (running) return
        running = true
        inboxDir.mkdirs()
        startTcpServer()
        registerNsd()
        discoverNsd()
        listener.onStatus("虫洞已开启 · $peerName")
    }

    fun stop() {
        running = false
        nsdManager?.apply {
            stopServiceDiscovery(discoveryListener)
            unregisterService(registrationListener)
        }
        serverSocket?.close()
        scope.cancel()
        peers.clear()
        listener.onPeerChanged(emptyList())
    }

    // ---- TCP 服务器 ----

    private fun startTcpServer() {
        try {
            serverSocket = ServerSocket(0) // OS 自动分配端口
            actualPort = serverSocket!!.localPort
        } catch (e: IOException) {
            listener.onStatus("TCP 启动失败: ${e.message}")
            return
        }
        scope.launch {
            while (running) {
                try {
                    val conn = serverSocket!!.accept()
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
        try {
            val input = BufferedInputStream(conn.getInputStream())
            val header = WHPP.readHeader(input)

            // basename 防路径穿越
            val safeName = File(header.filename).name
            val dst = File(inboxDir, safeName)
            partFile = File(inboxDir, "$safeName.part")

            // 写 .part, 完成后原子改名
            FileOutputStream(partFile).use { fout ->
                val buf = ByteArray(WHPP.BUFFER_SIZE)
                var remaining = header.size
                while (remaining > 0) {
                    val toRead = minOf(buf.size.toLong(), remaining).toInt()
                    val n = input.read(buf, 0, toRead)
                    if (n < 0) break
                    fout.write(buf, 0, n)
                    remaining -= n
                }
            }

            // 解密
            if (header.encrypted && secret.isNotEmpty()) {
                val blob = partFile.readBytes()
                val plain = Crypto.decrypt(secret, blob)
                if (plain != null) partFile.writeBytes(plain)
                else listener.onStatus("解密失败: $safeName")
            } else if (header.encrypted && secret.isEmpty()) {
                listener.onStatus("收到加密文件但未设口令: $safeName")
            }

            // 原子改名 (覆盖同名)
            if (partFile.exists()) partFile.renameTo(dst)
            partFile = null

            listener.onFileReceived(safeName, dst.absolutePath)
            listener.onStatus("收到: $safeName")
        } catch (e: Exception) {
            listener.onStatus("接收失败: ${e.message}")
        } finally {
            conn.close()
            partFile?.delete()
        }
    }

    // ---- 发送文件 ----

    fun sendFile(filePath: String): Boolean {
        val file = File(filePath)
        if (!file.isFile) {
            listener.onStatus("文件不存在")
            return false
        }
        val peerName = selectedPeer ?: run {
            listener.onStatus("请先选择目标设备")
            return false
        }
        val peer = peers[peerName] ?: run {
            listener.onStatus("目标设备已离线")
            return false
        }

        scope.launch {
            try {
                val socket = Socket()
                socket.connect(java.net.InetSocketAddress(peer.host, peer.port), 15_000)
                socket.use { s ->
                    val out = BufferedOutputStream(s.getOutputStream())

                    if (secret.isNotEmpty()) {
                        // 加密: 整块读入内存加密
                        val plain = file.readBytes()
                        val enc = Crypto.encrypt(secret, plain)
                        WHPP.writeFrame(
                            out, file.name, enc.size.toLong(), true,
                            ByteArrayInputStream(enc)
                        )
                    } else {
                        // 明文: 流式
                        WHPP.writeFrame(
                            out, file.name, file.length(), false,
                            file.inputStream()
                        )
                    }
                }
                listener.onStatus("已发送: ${file.name}")
            } catch (e: Exception) {
                listener.onStatus("发送失败: ${e.message}")
            }
        }
        return true
    }

    // ---- 对端管理 ----

    fun getPeers(): List<Peer> = peers.values.toList().sortedBy { it.name }

    fun selectPeer(name: String?) {
        selectedPeer = name
        listener.onStatus(if (name != null) "目标: $name" else "未选择目标")
    }

    fun getSelectedPeer(): String? = selectedPeer

    // ---- NSD 注册 ----

    private var registrationListener: NsdManager.RegistrationListener? = null
    private var discoveryListener: NsdManager.DiscoveryListener? = null

    private fun registerNsd() {
        nsdManager = context.getSystemService(Context.NSD_SERVICE) as NsdManager
        val info = NsdServiceInfo().apply {
            serviceName = peerName
            serviceType = SERVICE_TYPE
            port = actualPort
        }
        registrationListener = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(serviceInfo: NsdServiceInfo) {}
            override fun onRegistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                listener.onStatus("NSD 注册失败: $errorCode")
            }
            override fun onServiceUnregistered(serviceInfo: NsdServiceInfo) {}
            override fun onUnregistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {}
        }
        nsdManager!!.registerService(info, NsdManager.PROTOCOL_DNS_SD, registrationListener)
    }

    // ---- NSD 发现 ----

    private fun discoverNsd() {
        discoveryListener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onDiscoveryStopped(serviceType: String) {}

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                val name = serviceInfo.serviceName
                if (name == peerName) return // 跳过自己
                nsdManager!!.resolveService(serviceInfo, object : NsdManager.ResolveListener {
                    override fun onServiceResolved(info: NsdServiceInfo) {
                        val host = info.host?.hostAddress ?: return
                        peers[name] = Peer(name, host, info.port)
                        listener.onPeerChanged(getPeers())
                        listener.onStatus("发现: $name")
                    }
                    override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {}
                })
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                val name = serviceInfo.serviceName
                peers.remove(name)
                if (selectedPeer == name) selectedPeer = null
                listener.onPeerChanged(getPeers())
                listener.onStatus("$name 离线")
            }

            override fun onStartDiscoveryFailed(serviceType: String?, errorCode: Int) {
                listener.onStatus("NSD 发现启动失败: $errorCode")
            }
            override fun onStopDiscoveryFailed(serviceType: String?, errorCode: Int) {
                listener.onStatus("NSD 停止发现失败: $errorCode")
            }
        }
        nsdManager!!.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener)
    }
}
