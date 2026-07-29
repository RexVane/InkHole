package com.rexvane.inkhole.p2p

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketException
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

internal data class LanAnnouncement(
    val instanceId: String,
    val port: Int,
    val isReply: Boolean = false,
    /**
     * 离开通知：节点正常退出或切换网络时发出。协议 v3 的旧端不认识这个字段，
     * 会按普通通告解码并回到原有探测路径，因此发送它对老版本没有副作用。
     */
    val isBye: Boolean = false,
)

internal data class LanHint(val instanceId: String, val port: Int)

/** Fixed-size TCP hint used only to request a signed WHPC callback probe. */
internal object LanHintProtocol {
    val MAGIC = "IKLD".toByteArray(Charsets.US_ASCII)
    const val VERSION = 1
    const val FRAME_SIZE = 39

    fun encode(instanceId: String, port: Int): ByteArray {
        require(instanceId.matches(Regex("[0-9a-fA-F]{32}")) && port in 1..65535)
        return ByteBuffer.allocate(FRAME_SIZE).apply {
            put(MAGIC)
            put(VERSION.toByte())
            put(instanceId.lowercase().toByteArray(Charsets.US_ASCII))
            putShort(port.toShort())
        }.array()
    }

    fun decode(frame: ByteArray): LanHint? {
        if (frame.size != FRAME_SIZE || !frame.copyOfRange(0, 4).contentEquals(MAGIC)) {
            return null
        }
        val buffer = ByteBuffer.wrap(frame)
        buffer.position(4)
        if ((buffer.get().toInt() and 0xff) != VERSION) return null
        val instanceId = ByteArray(32).also { buffer.get(it) }
            .toString(Charsets.US_ASCII).lowercase()
        val port = buffer.short.toInt() and 0xffff
        if (!instanceId.matches(Regex("[0-9a-f]{32}")) || port !in 1..65535) return null
        return LanHint(instanceId, port)
    }
}

/** Small address hint for Android hotspots that do not expose mDNS to tethered clients. */
internal object LanDiscoveryProtocol {
    const val PORT = 41301
    private const val MAGIC = "inkhole-lan-v1"
    private const val VERSION = 3
    const val MAX_PACKET = 2048

    fun encode(instanceId: String, port: Int, reply: Boolean = false,
               bye: Boolean = false): ByteArray {
        require(instanceId.matches(Regex("[0-9a-fA-F]{32}")) && port in 1..65535)
        val json = JSONObject()
            .put("magic", MAGIC)
            .put("version", VERSION)
            .put("instance_id", instanceId.lowercase())
            .put("port", port)
            .put("reply", reply)
        // 普通通告不带这个键，保证与旧版本逐字节一致。
        if (bye) json.put("bye", true)
        return json
            .toString()
            .toByteArray(Charsets.US_ASCII)
    }

    fun decode(payload: ByteArray, length: Int = payload.size): LanAnnouncement? {
        if (length !in 1..MAX_PACKET || length > payload.size) return null
        val json = try {
            JSONObject(String(payload, 0, length, Charsets.US_ASCII))
        } catch (_: Exception) {
            return null
        }
        val instanceId = json.optString("instance_id").lowercase()
        val rawPort = json.opt("port")
        val port = (rawPort as? Number)?.toInt() ?: return null
        val rawReply = json.opt("reply")
        val reply = when (rawReply) {
            null -> false
            is Boolean -> rawReply
            else -> return null
        }
        val rawBye = json.opt("bye")
        val bye = when (rawBye) {
            null -> false
            is Boolean -> rawBye
            else -> return null
        }
        if (json.optString("magic") != MAGIC || json.optInt("version") != VERSION ||
            !instanceId.matches(Regex("[0-9a-f]{32}")) || port !in 1..65535) return null
        return LanAnnouncement(instanceId, port, reply, bye)
    }

    fun broadcastTargets(links: List<LanLink>): List<String> {
        val targets = linkedSetOf("255.255.255.255")
        links.forEach { link ->
            val address = try {
                InetAddress.getByName(link.address.substringBefore('%')) as? Inet4Address
            } catch (_: Exception) {
                null
            } ?: return@forEach
            if (link.prefixLength !in 1..31) return@forEach
            val raw = address.address.fold(0) { value, byte ->
                (value shl 8) or (byte.toInt() and 0xff)
            }
            val mask = -1 shl (32 - link.prefixLength)
            val broadcast = raw or mask.inv()
            targets += listOf(
                (broadcast ushr 24) and 0xff,
                (broadcast ushr 16) and 0xff,
                (broadcast ushr 8) and 0xff,
                broadcast and 0xff,
            ).joinToString(".")
        }
        return targets.toList()
    }
}

internal class LanBroadcastDiscovery(
    private val scope: CoroutineScope,
    private val instanceId: String,
    private val listenPort: Int,
    private val links: () -> List<LanLink>,
    private val onAnnouncement: (host: String, announcement: LanAnnouncement) -> Unit,
    private val onError: (String) -> Unit,
    private val onGoodbye: (host: String, announcement: LanAnnouncement) -> Unit = { _, _ -> },
) {
    companion object {
        private const val ANNOUNCE_INTERVAL_MS = 2_000L
        private const val RECEIVE_TIMEOUT_MS = 500
        /**
         * 一次 bump 连发的包数与间隔。路由器普遍限速或直接丢弃广播帧，
         * 启动或切网时只发一包等于赌运气；五包总量不到 1KB，代价可以忽略。
         */
        private const val BURST_COUNT = 5
        private const val BURST_GAP_MS = 150L
    }

    @Volatile private var socket: DatagramSocket? = null
    // Number of packets left in the current burst, including the next send.
    // Atomic updates prevent a UI/network-change bump racing with the run loop
    // from being overwritten by its decrement.
    private val burstRemaining = AtomicInteger(0)
    private val burstRequested = AtomicBoolean(false)

    fun start() {
        if (socket != null) return
        val opened = try {
            DatagramSocket(null).apply {
                reuseAddress = true
                broadcast = true
                bind(InetSocketAddress(LanDiscoveryProtocol.PORT))
                soTimeout = RECEIVE_TIMEOUT_MS
            }
        } catch (error: Exception) {
            onError(error.message ?: "UDP 端口不可用")
            return
        }
        socket = opened
        burstRemaining.set(BURST_COUNT)
        scope.launch { run(opened) }
    }

    /** 请求立即连发一轮通告：启动、切换网络、回到前台时调用。 */
    fun bump() {
        val opened = socket ?: return
        burstRemaining.set(BURST_COUNT)
        burstRequested.set(true)
        // receive() may otherwise keep the old quiet-period schedule for up to
        // two seconds. An invalid loopback datagram wakes it without exposing a
        // discovery packet to the LAN; the next iteration starts the burst.
        // bump() is called by Activity callbacks, so network I/O must stay on
        // the discovery scope instead of Android's main thread.
        scope.launch {
            if (socket !== opened) return@launch
            try {
                DatagramSocket().use { wakeSocket ->
                    val wake = byteArrayOf(0)
                    wakeSocket.send(DatagramPacket(
                        wake,
                        wake.size,
                        InetAddress.getByName("127.0.0.1"),
                        LanDiscoveryProtocol.PORT,
                    ))
                }
            } catch (_: Exception) {
                // The existing receive timeout remains a bounded fallback.
            }
        }
    }

    /**
     * 告诉同网段本机要走了，让对端不必耗完探活容忍就能移除。尽力而为——
     * WiFi 已经断掉时根本发不出去，所以探活循环仍是移除的最终依据。
     */
    fun sayGoodbye() {
        val opened = socket ?: return
        val payload = try {
            LanDiscoveryProtocol.encode(instanceId, listenPort, bye = true)
        } catch (_: Exception) {
            return
        }
        sendToAll(opened, payload)
    }

    fun stop() {
        val opened = socket
        socket = null
        try {
            opened?.close()
        } catch (_: Exception) {
        }
    }

    private fun sendToAll(opened: DatagramSocket, payload: ByteArray) {
        LanDiscoveryProtocol.broadcastTargets(links()).forEach { target ->
            try {
                opened.send(DatagramPacket(
                    payload,
                    payload.size,
                    InetAddress.getByName(target),
                    LanDiscoveryProtocol.PORT,
                ))
            } catch (_: Exception) {
            }
        }
    }

    private suspend fun run(opened: DatagramSocket) {
        val announcement = LanDiscoveryProtocol.encode(instanceId, listenPort)
        val reply = LanDiscoveryProtocol.encode(instanceId, listenPort, reply = true)
        var nextAnnouncement = 0L
        while (scope.isActive && socket === opened) {
            if (burstRequested.getAndSet(false)) nextAnnouncement = 0L
            val now = System.currentTimeMillis()
            if (now >= nextAnnouncement) {
                sendToAll(opened, announcement)
                val remaining = burstRemaining.updateAndGet { current ->
                    if (current > 0) current - 1 else 0
                }
                nextAnnouncement = if (remaining > 0) {
                    now + BURST_GAP_MS
                } else {
                    now + ANNOUNCE_INTERVAL_MS
                }
            }
            // 突发期间不能睡满接收超时，否则间隔被拉长就不再是突发。
            val wait = (nextAnnouncement - System.currentTimeMillis())
                .coerceIn(1L, RECEIVE_TIMEOUT_MS.toLong())
            opened.soTimeout = wait.toInt()
            val buffer = ByteArray(LanDiscoveryProtocol.MAX_PACKET + 1)
            val packet = DatagramPacket(buffer, buffer.size)
            try {
                opened.receive(packet)
                val decoded = LanDiscoveryProtocol.decode(buffer, packet.length) ?: continue
                val host = packet.address?.hostAddress?.substringBefore('%') ?: continue
                if (decoded.isBye) {
                    onGoodbye(host, decoded)
                    continue
                }
                if (!decoded.isReply) {
                    try {
                        opened.send(DatagramPacket(
                            reply,
                            reply.size,
                            packet.address,
                            LanDiscoveryProtocol.PORT,
                        ))
                    } catch (_: Exception) {
                    }
                }
                onAnnouncement(host, decoded)
            } catch (_: java.net.SocketTimeoutException) {
                delay(1)
            } catch (_: SocketException) {
                return
            } catch (_: Exception) {
            }
        }
    }
}
