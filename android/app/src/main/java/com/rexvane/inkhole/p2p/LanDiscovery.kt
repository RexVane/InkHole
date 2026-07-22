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

internal data class LanAnnouncement(
    val instanceId: String,
    val port: Int,
    val isReply: Boolean = false,
)

/** Small address hint for Android hotspots that do not expose mDNS to tethered clients. */
internal object LanDiscoveryProtocol {
    const val PORT = 41301
    private const val MAGIC = "inkhole-lan-v1"
    private const val VERSION = 3
    const val MAX_PACKET = 2048

    fun encode(instanceId: String, port: Int, reply: Boolean = false): ByteArray {
        require(instanceId.matches(Regex("[0-9a-fA-F]{32}")) && port in 1..65535)
        return JSONObject()
            .put("magic", MAGIC)
            .put("version", VERSION)
            .put("instance_id", instanceId.lowercase())
            .put("port", port)
            .put("reply", reply)
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
        if (json.optString("magic") != MAGIC || json.optInt("version") != VERSION ||
            !instanceId.matches(Regex("[0-9a-f]{32}")) || port !in 1..65535) return null
        return LanAnnouncement(instanceId, port, reply)
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
) {
    companion object {
        private const val ANNOUNCE_INTERVAL_MS = 2_000L
        private const val RECEIVE_TIMEOUT_MS = 500
    }

    @Volatile private var socket: DatagramSocket? = null

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
        scope.launch { run(opened) }
    }

    fun stop() {
        val opened = socket
        socket = null
        try {
            opened?.close()
        } catch (_: Exception) {
        }
    }

    private suspend fun run(opened: DatagramSocket) {
        val announcement = LanDiscoveryProtocol.encode(instanceId, listenPort)
        val reply = LanDiscoveryProtocol.encode(instanceId, listenPort, reply = true)
        var nextAnnouncement = 0L
        while (scope.isActive && socket === opened) {
            val now = System.currentTimeMillis()
            if (now >= nextAnnouncement) {
                LanDiscoveryProtocol.broadcastTargets(links()).forEach { target ->
                    try {
                        val packet = DatagramPacket(
                            announcement,
                            announcement.size,
                            InetAddress.getByName(target),
                            LanDiscoveryProtocol.PORT,
                        )
                        opened.send(packet)
                    } catch (_: Exception) {
                    }
                }
                nextAnnouncement = now + ANNOUNCE_INTERVAL_MS
            }
            val buffer = ByteArray(LanDiscoveryProtocol.MAX_PACKET + 1)
            val packet = DatagramPacket(buffer, buffer.size)
            try {
                opened.receive(packet)
                val decoded = LanDiscoveryProtocol.decode(buffer, packet.length) ?: continue
                val host = packet.address?.hostAddress?.substringBefore('%') ?: continue
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
