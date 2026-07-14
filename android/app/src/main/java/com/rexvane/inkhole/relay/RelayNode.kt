package com.rexvane.inkhole.relay

import android.content.Context
import com.rexvane.inkhole.p2p.InkHoleListener
import com.rexvane.inkhole.p2p.Peer
import com.rexvane.inkhole.p2p.TransportNode
import com.rexvane.inkhole.p2p.WHPP
import net.schmizz.sshj.SSHClient
import net.schmizz.sshj.connection.channel.Channel
import net.schmizz.sshj.connection.channel.forwarded.ConnectListener
import net.schmizz.sshj.connection.channel.forwarded.RemotePortForwarder
import net.schmizz.sshj.sftp.OpenMode
import net.schmizz.sshj.sftp.RenameFlags
import net.schmizz.sshj.sftp.SFTPClient
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.nio.ByteBuffer
import java.util.EnumSet
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean

private const val REGISTRY_DIR = ".cache/inkhole/peers"
private const val LEASE_SECONDS = 75L
private const val HEARTBEAT_MS = 20_000L
private const val POLL_MS = 5_000L
private const val MAX_PEERS = 256
private const val DISK_MARGIN = 256L * 1024 * 1024
private const val PROGRESS_INTERVAL_MS = 250L

private data class RelayPeer(
    val peer: Peer,
    val deviceId: String,
    val publicKey: String,
)

class RelayNode(
    @Suppress("UNUSED_PARAMETER") private val context: Context,
    peerName: String,
    private val inboxDir: File,
    val settings: RelaySettings,
    private val listener: InkHoleListener,
) : TransportNode {
    private val runtime = checkNotNull(SshRelayRuntime.get()) { "SSH 私钥尚未输入" }
    private val identity = DeviceIdentity.fromPrivateB64(settings.privateKey)
    private val connector = AndroidSshConnector()
    private val running = AtomicBoolean(false)
    private val peersLock = Any()
    private val stateLock = Any()
    private val peers = LinkedHashMap<String, RelayPeer>()
    private val channels = ConcurrentHashMap.newKeySet<Channel>()
    private val seenTransfers = ConcurrentHashMap<String, Long>()
    @Volatile private var currentName = peerName
    @Volatile private var selectedPeer: String? = null
    @Volatile private var selectedService: String? = null
    @Volatile private var ssh: SSHClient? = null
    @Volatile private var sftp: SFTPClient? = null
    @Volatile private var forward: RemotePortForwarder.Forward? = null
    private var connectionThread: Thread? = null

    override fun start() {
        if (!running.compareAndSet(false, true)) return
        inboxDir.mkdirs()
        connectionThread = Thread { connectionLoop() }.apply {
            isDaemon = true
            name = "InkHole-SSH"
            start()
        }
    }

    override fun stop() {
        running.set(false)
        val currentSsh = ssh
        val currentForward = forward
        if (currentSsh != null && currentForward != null) {
            runCatching { currentSsh.remotePortForwarder.cancel(currentForward) }
        }
        channels.toList().forEach { runCatching { it.close() } }
        channels.clear()
        runCatching { sftp?.close() }
        runCatching { currentSsh?.disconnect() }
        runCatching { currentSsh?.close() }
        connectionThread?.interrupt()
        if (connectionThread !== Thread.currentThread()) {
            runCatching { connectionThread?.join(4_000) }
        }
        synchronized(peersLock) { peers.clear() }
        selectedPeer = null
        listener.onPeerChanged(emptyList())
    }

    private fun connectionLoop() {
        var delay = 1_000L
        while (running.get()) {
            var currentSsh: SSHClient? = null
            var currentSftp: SFTPClient? = null
            var currentForward: RemotePortForwarder.Forward? = null
            try {
                listener.onStatus("正在连接 SSH 服务器")
                currentSsh = connector.authenticated(runtime.credentials, settings.hostKey)
                val forwarder = currentSsh.remotePortForwarder
                currentForward = forwarder.bind(
                    RemotePortForwarder.Forward("127.0.0.1", 0),
                    ConnectListener { channel -> acceptChannel(channel) },
                )
                check(currentForward.port > 0) { "服务器未分配 SSH 反向转发端口" }
                currentSftp = currentSsh.newSFTPClient()
                ensureRegistry(currentSftp)
                synchronized(stateLock) {
                    ssh = currentSsh
                    sftp = currentSftp
                    forward = currentForward
                }
                writeRegistry(currentSftp, currentForward.port)
                pollRegistry(currentSftp)
                listener.onStatus("SSH 远程通道已连接")
                delay = 1_000L
                var lastHeartbeat = System.currentTimeMillis()
                while (running.get() && currentSsh.isConnected && currentSsh.isAuthenticated) {
                    Thread.sleep(POLL_MS)
                    val now = System.currentTimeMillis()
                    if (now - lastHeartbeat >= HEARTBEAT_MS) {
                        writeRegistry(currentSftp, currentForward.port)
                        lastHeartbeat = now
                    }
                    pollRegistry(currentSftp)
                }
                if (running.get()) throw IOException("SSH 连接已断开")
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
            } catch (e: Exception) {
                if (running.get()) {
                    val detail = if (e.message.orEmpty().contains(
                            "administratively prohibited", ignoreCase = true)) {
                        "服务器禁止 TCP 转发，请启用 AllowTcpForwarding"
                    } else e.message.orEmpty().ifBlank { "连接失败" }
                    listener.onStatus("SSH 远程通道断开: $detail")
                }
            } finally {
                runCatching { currentSftp?.rm("$REGISTRY_DIR/${settings.deviceId}.json") }
                runCatching { currentSftp?.close() }
                if (currentSsh != null && currentForward != null) {
                    runCatching { currentSsh.remotePortForwarder.cancel(currentForward) }
                }
                runCatching { currentSsh?.disconnect() }
                runCatching { currentSsh?.close() }
                synchronized(stateLock) {
                    if (ssh === currentSsh) {
                        ssh = null
                        sftp = null
                        forward = null
                    }
                }
                synchronized(peersLock) { peers.clear() }
                selectedPeer = null
                listener.onPeerChanged(emptyList())
            }
            if (running.get()) {
                try {
                    Thread.sleep(delay)
                } catch (_: InterruptedException) {
                    Thread.currentThread().interrupt()
                }
                delay = minOf(delay * 2, 15_000L)
            }
        }
    }

    private fun acceptChannel(channel: Channel.Forwarded) {
        if (!running.get()) {
            runCatching { channel.close() }
            return
        }
        channel.confirm()
        channels.add(channel)
        Thread { receiveChannel(channel) }.apply {
            isDaemon = true
            name = "InkHole-SSH-receive"
            start()
        }
    }

    private fun ensureRegistry(client: SFTPClient) {
        var current = ""
        REGISTRY_DIR.split('/').forEach { part ->
            current = if (current.isEmpty()) part else "$current/$part"
            if (client.statExistence(current) == null) client.mkdir(current)
            runCatching { client.chmod(current, 448) } // 0700
        }
    }

    private fun writeRegistry(client: SFTPClient, remotePort: Int) {
        val record = RegistryRecord(
            settings.deviceId, currentName, remotePort, settings.publicKey)
        val raw = encodeRegistryRecord(record, runtime.registryKey)
        val target = "$REGISTRY_DIR/${settings.deviceId}.json"
        val temporary = "$target.${UUID.randomUUID().toString().take(8)}.tmp"
        client.open(temporary, EnumSet.of(OpenMode.CREAT, OpenMode.TRUNC, OpenMode.WRITE)).use {
            it.write(0, raw, 0, raw.size)
        }
        runCatching { client.chmod(temporary, 384) } // 0600
        try {
            client.rename(temporary, target,
                EnumSet.of(RenameFlags.OVERWRITE, RenameFlags.ATOMIC))
        } catch (_: Exception) {
            runCatching { client.rm(target) }
            client.rename(temporary, target)
        }
    }

    private fun readRemoteFile(client: SFTPClient, path: String, size: Int): ByteArray {
        require(size in 1..SSH_REGISTRY_LIMIT)
        val result = ByteArray(size)
        client.open(path, EnumSet.of(OpenMode.READ)).use { file ->
            var offset = 0
            while (offset < size) {
                val count = file.read(offset.toLong(), result, offset, size - offset)
                if (count <= 0) throw IOException("SSH 登记读取中断")
                offset += count
            }
        }
        return result
    }

    private fun pollRegistry(client: SFTPClient) {
        val now = System.currentTimeMillis() / 1_000
        val records = mutableListOf<RegistryRecord>()
        client.ls(REGISTRY_DIR).take(MAX_PEERS + 32).forEach { item ->
            if (!item.isRegularFile || !item.name.matches(Regex("[0-9a-f]{32}\\.json"))) {
                return@forEach
            }
            val path = "$REGISTRY_DIR/${item.name}"
            if (now - item.attributes.mtime > LEASE_SECONDS) {
                runCatching { client.rm(path) }
                return@forEach
            }
            val size = item.attributes.size.toInt()
            if (size !in 1..SSH_REGISTRY_LIMIT) return@forEach
            runCatching {
                decodeRegistryRecord(readRemoteFile(client, path, size), runtime.registryKey)
            }.getOrNull()?.takeIf { it.deviceId != settings.deviceId }?.let(records::add)
        }
        val duplicateNames = records.groupingBy { it.name }.eachCount()
        val updated = LinkedHashMap<String, RelayPeer>()
        records.take(MAX_PEERS).forEach { record ->
            val display = if ((duplicateNames[record.name] ?: 0) > 1) {
                "${record.name} · ${record.deviceId.take(4)}"
            } else record.name
            updated[record.deviceId] = RelayPeer(
                Peer(display, settings.host, record.port, record.deviceId),
                record.deviceId, record.publicKey)
        }
        val changed: Boolean
        synchronized(peersLock) {
            changed = peers.values.map { listOf(it.deviceId, it.peer.name,
                it.peer.port.toString(), it.publicKey) }.toSet() !=
                updated.values.map { listOf(it.deviceId, it.peer.name,
                    it.peer.port.toString(), it.publicKey) }.toSet()
            peers.clear()
            peers.putAll(updated)
            selectedService?.let { service -> selectedPeer = peers[service]?.peer?.name }
        }
        if (changed) listener.onPeerChanged(getPeers())
    }

    override fun getPeers(): List<Peer> = synchronized(peersLock) {
        peers.values.map { it.peer }.sortedBy { it.name }
    }

    override fun selectPeer(name: String?) {
        selectedPeer = name
        selectedService = synchronized(peersLock) {
            peers.values.firstOrNull { it.peer.name == name }?.deviceId
        }
        listener.onStatus(if (name == null) "未选择目标" else "目标: $name")
    }

    override fun getSelectedPeer(): String? = selectedPeer
    override fun getSelectedServiceName(): String? = selectedService
    override fun restoreSelectedService(serviceName: String?) { selectedService = serviceName }

    fun rename(name: String) {
        require(name.trim().length in 1..80) { "设备名称不能为空且不能超过 80 个字符" }
        currentName = name.trim()
        val client = sftp
        val remotePort = forward?.port
        if (client != null && remotePort != null) writeRegistry(client, remotePort)
    }

    override fun sendFile(filePath: String): Boolean {
        val file = File(filePath)
        if (!file.isFile) return false.also { listener.onStatus("文件不存在") }
        val target = synchronized(peersLock) {
            peers.values.firstOrNull { it.peer.name == selectedPeer }
        } ?: return false.also { listener.onStatus("请先选择 SSH 远程目标设备") }
        val currentSsh = ssh
            ?: return false.also { listener.onStatus("SSH 远程通道尚未连接") }
        val transferId = UUID.randomUUID().toString()
        var channel: Channel? = null
        return try {
            val direct = currentSsh.newDirectConnection("127.0.0.1", target.peer.port)
            channel = direct
            channels.add(direct)
            val input = direct.inputStream
            val output = direct.outputStream
            val offer = encodeOffer(TransferOffer(
                transferId, settings.deviceId, target.deviceId, settings.publicKey),
                runtime.registryKey)
            require(offer.size <= SSH_REGISTRY_LIMIT) { "SSH 传输握手过大" }
            output.write(SSH_HANDSHAKE_MAGIC)
            output.write(ByteBuffer.allocate(4).putInt(offer.size).array())
            output.write(offer)
            output.flush()
            require(input.readExact(1).contentEquals(byteArrayOf(WHPP.ACK_OK.toByte()))) {
                "目标设备拒绝 SSH 传输握手"
            }
            val key = deriveTransferKey(identity, target.publicKey, transferId,
                settings.deviceId, target.deviceId)
            val stream = SshFrameStream(input, output,
                RelayCipher(key, transferId, settings.deviceId, target.deviceId))
            val header = JSONObject()
                .put("filename", file.name)
                .put("size", file.length())
                .put("encrypted", true)
                .put("enc_mode", "ssh-aead")
                .put("want_ack", true)
                .toString().toByteArray(Charsets.UTF_8)
            require(header.size <= WHPP.MAX_HEADER) { "文件名过长" }
            stream.send(0, WHPP.MAGIC + ByteBuffer.allocate(4)
                .putInt(header.size).array() + header)
            var sent = 0L
            var lastReport = 0L
            file.inputStream().use { source ->
                val buffer = ByteArray(RELAY_FRAME_PLAIN_LIMIT)
                while (true) {
                    val count = source.read(buffer)
                    if (count < 0) break
                    stream.send(0, buffer.copyOf(count))
                    sent += count
                    val now = System.currentTimeMillis()
                    if (sent >= file.length() || now - lastReport >= PROGRESS_INTERVAL_MS) {
                        lastReport = now
                        listener.onProgress("send", file.name, sent, file.length())
                    }
                }
            }
            require(stream.receive(1).contentEquals(byteArrayOf(WHPP.ACK_OK.toByte()))) {
                "目标设备未确认文件落盘"
            }
            listener.onStatus("已发送: ${file.name}")
            true
        } catch (e: Exception) {
            listener.onStatus("SSH 远程发送失败: ${e.message}")
            false
        } finally {
            channel?.let {
                channels.remove(it)
                runCatching { it.close() }
            }
        }
    }

    private fun receiveChannel(channel: Channel.Forwarded) {
        var part: File? = null
        var stream: SshFrameStream? = null
        var success = false
        try {
            val input = channel.inputStream
            val output = channel.outputStream
            require(input.readExact(4).contentEquals(SSH_HANDSHAKE_MAGIC)) {
                "SSH 传输握手标识无效"
            }
            val offerSize = ByteBuffer.wrap(input.readExact(4)).int
            require(offerSize in 1..SSH_REGISTRY_LIMIT) { "SSH 传输握手长度无效" }
            val offer = decodeOffer(input.readExact(offerSize), runtime.registryKey,
                settings.deviceId)
            val now = System.currentTimeMillis()
            seenTransfers.entries.removeIf { now - it.value > 600_000 }
            require(seenTransfers.putIfAbsent(offer.transferId, now) == null) {
                "重复的 SSH 传输握手"
            }
            val known = synchronized(peersLock) { peers[offer.senderId] }
            require(known == null || known.publicKey == offer.publicKey) {
                "发送设备公钥与在线登记不一致"
            }
            output.write(byteArrayOf(WHPP.ACK_OK.toByte()))
            output.flush()
            val key = deriveTransferKey(identity, offer.publicKey, offer.transferId,
                offer.senderId, settings.deviceId)
            stream = SshFrameStream(input, output,
                RelayCipher(key, offer.transferId, offer.senderId, settings.deviceId))
            val reader = SshFrameReader(stream, 0)
            require(reader.readExact(4).contentEquals(WHPP.MAGIC)) { "WHPP magic 非法" }
            val headerSize = ByteBuffer.wrap(reader.readExact(4)).int
            require(headerSize in 1..WHPP.MAX_HEADER) { "WHPP 头长度非法" }
            val header = JSONObject(String(reader.readExact(headerSize), Charsets.UTF_8))
            val filename = safeFilename(header.getString("filename"))
            val size = header.getLong("size")
            require(size in 0..WHPP.MAX_FILE_SIZE) { "文件大小声明非法" }
            inboxDir.mkdirs()
            require(inboxDir.usableSpace == 0L || inboxDir.usableSpace >= size + DISK_MARGIN) {
                "收件箱磁盘空间不足"
            }
            var destination = uniqueFile(inboxDir, filename)
            part = File(destination.absolutePath + ".${UUID.randomUUID().toString().take(8)}.part")
            var received = 0L
            var lastReport = 0L
            FileOutputStream(part).use { target ->
                while (received < size) {
                    val chunk = reader.readExact(minOf(
                        RELAY_FRAME_PLAIN_LIMIT.toLong(), size - received).toInt())
                    target.write(chunk)
                    received += chunk.size
                    val reportAt = System.currentTimeMillis()
                    if (received >= size || reportAt - lastReport >= PROGRESS_INTERVAL_MS) {
                        lastReport = reportAt
                        listener.onProgress("recv", filename, received, size)
                    }
                }
                target.fd.sync()
            }
            destination = uniqueFile(inboxDir, filename)
            if (!part.renameTo(destination)) {
                part.copyTo(destination, overwrite = false)
                part.delete()
            }
            part = null
            success = true
            listener.onFileReceived(filename, destination.absolutePath)
        } catch (e: Exception) {
            listener.onStatus("SSH 远程接收失败: ${e.message}")
        } finally {
            part?.delete()
            stream?.let {
                runCatching { it.send(1, byteArrayOf(
                    (if (success) WHPP.ACK_OK else WHPP.ACK_FAIL).toByte())) }
            }
            channels.remove(channel)
            runCatching { channel.close() }
        }
    }

    private fun safeFilename(value: String): String {
        val invalid = "<>:\"/\\|?*"
        return File(value.replace('\\', '/')).name.map { char ->
            if (char.code < 32 || char in invalid) '_' else char
        }.joinToString("").trim().trimEnd('.', ' ').take(240).ifBlank { "unnamed" }
    }

    private fun uniqueFile(directory: File, name: String): File {
        var candidate = File(directory, name)
        if (!candidate.exists()) return candidate
        val dot = name.lastIndexOf('.')
        val stem = if (dot > 0) name.substring(0, dot) else name
        val suffix = if (dot > 0) name.substring(dot) else ""
        var index = 2
        while (candidate.exists()) candidate = File(directory, "$stem ($index)$suffix")
            .also { index++ }
        return candidate
    }
}
