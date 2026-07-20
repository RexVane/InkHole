package com.rexvane.inkhole.transport

import android.content.Context
import android.net.Uri
import android.net.ConnectivityManager
import com.rexvane.inkhole.p2p.InkHoleNode
import mobile.Mobile
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.net.URI
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

class TransportException(message: String, cause: Throwable? = null) : Exception(message, cause)

internal fun formatHTTPProxyURL(host: String?, port: Int): String {
    val cleanHost = host?.trim().orEmpty()
    if (cleanHost.isEmpty() || port !in 1..65535) return ""
    val authority = if (cleanHost.contains(':') &&
        !(cleanHost.startsWith("[") && cleanHost.endsWith("]"))) {
        "[$cleanHost]"
    } else {
        cleanHost
    }
    return "http://$authority:$port"
}

fun interface TransportEventListener {
    fun onTransportEvent(event: String, data: JSONObject)
}

/** Owns the in-process Go core and keeps SSH relay endpoints attached to the active node. */
object TransportManager {
    private const val MAX_PRIVATE_KEY_BYTES = 1024 * 1024
    private val lifecycleLock = Any()
    private val callLock = Any()
    private val requestCounter = AtomicInteger()
    private val generation = AtomicInteger()
    private val sshRuntimePeers = ConcurrentHashMap<String, JSONObject>()
    private val wormholePeers = ConcurrentHashMap<String, String>()

    @Volatile private var appContext: Context? = null
    @Volatile private var node: InkHoleNode? = null
    @Volatile private var sshSessionId = ""
    @Volatile private var sshStartGeneration = 0
    @Volatile var listener: TransportEventListener? = null

    fun attach(context: Context, currentNode: InkHoleNode, deviceName: String, instanceId: String) {
        val currentGeneration: Int
        synchronized(lifecycleLock) {
            appContext = context.applicationContext
            node = currentNode
            currentGeneration = generation.incrementAndGet()
            sshStartGeneration++
            sshSessionId = ""
            sshRuntimePeers.clear()
            wormholePeers.clear()
            synchronized(callLock) {
                Mobile.touch()
                Mobile.reset()
                requestLocked("start", JSONObject().apply {
                    put("local_target", "127.0.0.1:${currentNode.getActualPort()}")
                    put("local_token", currentNode.getCoreIngressToken())
                    put("device_name", deviceName)
                    put("instance_id", instanceId)
                })
            }
        }
        startPollLoop(currentGeneration)
        if (config().ssh.enabled) startSSH()
    }

    fun detach() {
        synchronized(lifecycleLock) {
            generation.incrementAndGet()
            sshStartGeneration++
            sshSessionId = ""
            sshRuntimePeers.clear()
            wormholePeers.clear()
            node = null
            synchronized(callLock) { Mobile.reset() }
        }
    }

    fun config(): CrossNetworkConfig = appContext?.let(CrossNetworkStore::load)
        ?: CrossNetworkConfig()

    fun hasPastedKey(profileId: String): Boolean = appContext?.let {
        SecureStore.contains(it, secretName(profileId, "private_key"))
    } ?: false

    fun hasPassphrase(profileId: String): Boolean = appContext?.let {
        SecureStore.contains(it, secretName(profileId, "passphrase"))
    } ?: false

    fun isSSHReady(): Boolean = sshSessionId.isNotEmpty()

    fun saveConfig(
        settings: CrossNetworkConfig,
        pastedPrivateKey: String? = null,
        passphrase: String? = null,
    ) {
        val context = requireContext()
        val normalized = normalize(settings)
        val profile = normalized.ssh.profile
        if (profile.privateKeyMode == "paste" && pastedPrivateKey != null) {
            if (pastedPrivateKey.isBlank() && normalized.ssh.enabled) {
                throw TransportException("请粘贴已有 SSH 私钥")
            }
            SecureStore.put(context, secretName(profile.id, "private_key"), pastedPrivateKey)
        }
        if (profile.privateKeyMode == "file") {
            SecureStore.delete(context, secretName(profile.id, "private_key"))
        }
        if (passphrase != null) {
            SecureStore.put(context, secretName(profile.id, "passphrase"), passphrase)
        }
        if (normalized.ssh.enabled) {
            if (profile.hostKeySha256.isBlank()) {
                throw TransportException("请先验证并确认 VPS 主机指纹")
            }
            sshProfilePayload(profile)
        }
        CrossNetworkStore.save(context, normalized)
        if (normalized.ssh.enabled) restartSSH() else stopSSH()
    }

    fun checkSSH(
        profile: SSHProfileConfig,
        pastedPrivateKey: String? = null,
        passphrase: String? = null,
    ) {
        async("ssh.check.error") {
            val result = request("ssh.check", JSONObject().apply {
                put("profile", sshProfilePayload(profile, pastedPrivateKey, passphrase))
            })
            emit("ssh.check.result", result)
        }
    }

    fun createOneTime(summary: JSONObject) {
        async("wormhole.error") {
            val result = request("wormhole.create", JSONObject().apply {
                put("summary", summary)
                put("settings", wormholePayload())
            })
            emit("wormhole.code", result)
        }
    }

    fun joinOneTime(code: String) {
        async("wormhole.error") {
            val result = request("wormhole.join.start", JSONObject().apply {
                put("code", code.trim())
                put("settings", wormholePayload())
            })
            emit("wormhole.join.started", result)
        }
    }

    fun acceptOneTime(sessionId: String) = async("wormhole.error", sessionId) {
        request("wormhole.accept", JSONObject().put("session_id", sessionId))
    }

    fun rejectOneTime(sessionId: String) = async("wormhole.error", sessionId) {
        request("wormhole.reject", JSONObject().put("session_id", sessionId))
    }

    fun cancelSession(sessionId: String) {
        if (sessionId.isBlank()) return
        wormholePeers.remove(sessionId)?.let { peerId ->
            node?.removeExternalPeer(peerId, "wormhole")
        }
        async("core.error") {
            request("session.cancel", JSONObject().put("session_id", sessionId))
        }
    }

    fun createSSHPairing() {
        val session = sshSessionId
        if (session.isEmpty()) {
            emit("ssh.pair.error", JSONObject().put("error", "SSH 中继尚未连接"))
            return
        }
        async("ssh.pair.error") {
            emit("ssh.pair.code", request(
                "ssh.pair.create", JSONObject().put("session_id", session)))
        }
    }

    fun joinSSHPairing(code: String) {
        val session = sshSessionId
        if (session.isEmpty()) {
            emit("ssh.pair.error", JSONObject().put("error", "SSH 中继尚未连接"))
            return
        }
        async("ssh.pair.error") {
            val result = request("ssh.pair.join", JSONObject().apply {
                put("session_id", session)
                put("code", code.trim())
            })
            result.optJSONObject("peer")?.let(::rememberSSHPeer)
            emit("ssh.pair.joined", result)
        }
    }

    fun removeSSHPeer(instanceId: String) {
        val context = requireContext()
        val current = config()
        val updated = current.copy(ssh = current.ssh.copy(
            peers = current.ssh.peers.filterNot { it.instanceId == instanceId }))
        CrossNetworkStore.save(context, updated)
        sshRuntimePeers.remove(instanceId)
        node?.removeExternalPeer(instanceId, "ssh")
        if (updated.ssh.enabled) restartSSH()
    }

    fun setSSHPeerEncryption(instanceId: String, enabled: Boolean) {
        val context = requireContext()
        val current = config()
        val updated = current.copy(ssh = current.ssh.copy(peers = current.ssh.peers.map {
            if (it.instanceId == instanceId) it.copy(endToEnd = enabled) else it
        }))
        CrossNetworkStore.save(context, updated)
        if (updated.ssh.enabled) restartSSH()
    }

    private fun startPollLoop(currentGeneration: Int) {
        Thread({
            while (generation.get() == currentGeneration) {
                try {
                    val raw = Mobile.poll(1000)
                    if (raw.isBlank() || generation.get() != currentGeneration) continue
                    val event = JSONObject(raw)
                    handleCoreEvent(
                        event.optString("event"),
                        event.optJSONObject("data") ?: JSONObject(),
                    )
                } catch (error: Throwable) {
                    if (generation.get() == currentGeneration) {
                        emit("core.error", JSONObject().put(
                            "error", error.message ?: "跨网核心事件读取失败"))
                    }
                }
            }
        }, "inkhole-transport-events").apply { isDaemon = true }.start()
    }

    private fun handleCoreEvent(event: String, data: JSONObject) {
        when (event) {
            "wormhole.ready" -> if (data.optString("role") == "sender") {
                val sessionId = data.optString("session_id")
                try {
                    val endpoint = parseLoopbackEndpoint(data.getString("local_endpoint"))
                    val peerName = node?.upsertExternalPeer(
                        sessionId,
                        "一次性接收端",
                        endpoint.first,
                        endpoint.second,
                        "wormhole",
                        data.getString("endpoint_token"),
                    ).orEmpty()
                    if (peerName.isNotEmpty()) node?.selectPeer(peerName)
                    wormholePeers[sessionId] = sessionId
                } catch (error: Exception) {
                    data.put("error", "短码通道建立失败：${error.message}")
                    emit("wormhole.error", data)
                    return
                }
            }
            "wormhole.error" -> {
                val sessionId = data.optString("session_id")
                wormholePeers.remove(sessionId)?.let {
                    node?.removeExternalPeer(it, "wormhole")
                }
            }
            "ssh.paired" -> data.optJSONObject("peer")?.let(::rememberSSHPeer)
        }
        emit(event, data)
    }

    private fun restartSSH() {
        val restart = ++sshStartGeneration
        val session = sshSessionId
        sshSessionId = ""
        sshRuntimePeers.keys.forEach { node?.removeExternalPeer(it, "ssh") }
        sshRuntimePeers.clear()
        Thread({
            try {
                if (session.isNotEmpty()) {
                    request("session.cancel", JSONObject().put("session_id", session))
                }
            } catch (error: Exception) {
                if (restart == sshStartGeneration) {
                    emit("core.error", JSONObject().put(
                        "error", error.message ?: "无法停止旧 SSH 中继"))
                }
            }
            if (restart == sshStartGeneration && config().ssh.enabled) startSSH()
        }, "inkhole-ssh-restart").apply { isDaemon = true }.start()
    }

    private fun startSSH() {
        val context = requireContext()
        val current = config()
        if (!current.ssh.enabled) return
        val start = ++sshStartGeneration
        Thread({
            try {
                val profileId = current.ssh.profile.id
                val noisePrivate = SecureStore.get(
                    context, secretName(profileId, "noise_private")).orEmpty()
                val result = request("ssh.listen", JSONObject().apply {
                    put("profile", sshProfilePayload(current.ssh.profile))
                    put("remote_port", current.ssh.remotePort)
                    put("noise_private", noisePrivate)
                    put("peers", JSONArray().apply {
                        current.ssh.peers.forEach { put(sshPeerPayload(it)) }
                    })
                })
                if (start != sshStartGeneration || !config().ssh.enabled) {
                    request("session.cancel", JSONObject().put(
                        "session_id", result.optString("session_id")))
                    return@Thread
                }
                result.optString("noise_private").takeIf { it.isNotEmpty() }?.let {
                    SecureStore.put(context, secretName(profileId, "noise_private"), it)
                }
                sshSessionId = result.getString("session_id")
                val fresh = config()
                CrossNetworkStore.save(context, fresh.copy(ssh = fresh.ssh.copy(
                    remotePort = result.getInt("remote_port"))))
                val peers = result.optJSONArray("peers") ?: JSONArray()
                for (index in 0 until peers.length()) {
                    peers.optJSONObject(index)?.let(::rememberSSHPeer)
                }
                emit("ssh.ready", result)
            } catch (error: Exception) {
                if (start == sshStartGeneration) {
                    emit("ssh.config.error", JSONObject().put(
                        "error", error.message ?: "SSH 中继启动失败"))
                }
            }
        }, "inkhole-ssh-start").apply { isDaemon = true }.start()
    }

    private fun stopSSH() {
        sshStartGeneration++
        val session = sshSessionId
        sshSessionId = ""
        sshRuntimePeers.keys.forEach { node?.removeExternalPeer(it, "ssh") }
        sshRuntimePeers.clear()
        if (session.isNotEmpty()) async("core.error") {
            request("session.cancel", JSONObject().put("session_id", session))
        }
    }

    private fun rememberSSHPeer(peer: JSONObject) {
        val context = requireContext()
        val instanceId = peer.optString("instance_id")
        if (instanceId.isEmpty()) return
        if (peer.has("endpoint") && peer.has("endpoint_token")) {
            val endpoint = parseLoopbackEndpoint(peer.getString("endpoint"))
            node?.upsertExternalPeer(
                instanceId,
                peer.optString("name", "SSH 设备"),
                endpoint.first,
                endpoint.second,
                "ssh",
                peer.getString("endpoint_token"),
                instanceId,
            )
            sshRuntimePeers[instanceId] = JSONObject(peer.toString())
        }
        val saved = SSHPeerConfig(
            id = peer.optString("id", instanceId).ifEmpty { instanceId },
            name = peer.optString("name", "SSH 设备").ifEmpty { "SSH 设备" },
            instanceId = instanceId,
            remotePort = peer.optInt("remote_port"),
            noisePublic = peer.optString("noise_public"),
            endToEnd = peer.optBoolean("end_to_end", true),
        )
        if (saved.remotePort !in 1..65535 || saved.noisePublic.isEmpty()) return
        val current = config()
        val peers = current.ssh.peers.filterNot { it.instanceId == instanceId } + saved
        CrossNetworkStore.save(context, current.copy(ssh = current.ssh.copy(peers = peers)))
    }

    private fun request(method: String, params: JSONObject): JSONObject =
        synchronized(callLock) { requestLocked(method, params) }

    private fun requestLocked(method: String, params: JSONObject): JSONObject {
        val request = JSONObject().apply {
            put("id", "android-${requestCounter.incrementAndGet()}")
            put("method", method)
            put("params", params)
        }
        val response = try {
            JSONObject(Mobile.call(request.toString()))
        } catch (error: Throwable) {
            throw TransportException("跨网核心调用失败：${error.message}", error)
        }
        if (!response.optBoolean("ok")) {
            throw TransportException(response.optString("error", "跨网操作失败"))
        }
        return response.optJSONObject("result") ?: JSONObject()
    }

    private fun sshProfilePayload(
        profile: SSHProfileConfig,
        pastedOverride: String? = null,
        passphraseOverride: String? = null,
    ): JSONObject {
        val context = requireContext()
        if (profile.host.isBlank() || profile.user.isBlank() || profile.port !in 1..65535) {
            throw TransportException("请填写有效的 SSH 主机、端口和用户名")
        }
        val privateKey = if (profile.privateKeyMode == "paste") {
            pastedOverride ?: SecureStore.get(
                context, secretName(profile.id, "private_key"))
                ?: throw TransportException("请粘贴已有 SSH 私钥")
        } else {
            if (profile.privateKeyUri.isBlank()) throw TransportException("请选择 SSH 私钥文件")
            readPrivateKey(context, Uri.parse(profile.privateKeyUri))
        }
        val passphrase = passphraseOverride ?: SecureStore.get(
            context, secretName(profile.id, "passphrase")).orEmpty()
        return JSONObject().apply {
            put("id", profile.id)
            put("host", profile.host.trim())
            put("port", profile.port)
            put("user", profile.user.trim())
            put("private_key", privateKey)
            put("private_key_label", profile.privateKeyLabel)
            put("passphrase", passphrase)
            put("host_key_sha256", profile.hostKeySha256.trim())
        }
    }

    private fun sshPeerPayload(peer: SSHPeerConfig): JSONObject = JSONObject().apply {
        put("id", peer.id)
        put("name", peer.name)
        put("instance_id", peer.instanceId)
        put("remote_port", peer.remotePort)
        put("noise_public", peer.noisePublic)
        put("end_to_end", peer.endToEnd)
    }

    private fun wormholePayload(): JSONObject {
        val settings = config().wormhole
        return JSONObject().apply {
            put("rendezvous_url", settings.rendezvousUrl)
            put("transit_relay", settings.transitRelay)
            systemHTTPProxyURL().takeIf { it.isNotEmpty() }?.let {
                put("proxy_url", it)
            }
            put("timeout_minutes", 10)
        }
    }

    private fun systemHTTPProxyURL(): String {
        val context = appContext ?: return ""
        return try {
            val connectivity = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as ConnectivityManager
            val proxy = connectivity.getLinkProperties(connectivity.activeNetwork)?.httpProxy
            formatHTTPProxyURL(proxy?.host, proxy?.port ?: 0)
        } catch (_: Exception) {
            ""
        }
    }

    private fun normalize(config: CrossNetworkConfig): CrossNetworkConfig {
        val profile = config.ssh.profile.copy(
            host = config.ssh.profile.host.trim(),
            port = config.ssh.profile.port.takeIf { it in 1..65535 } ?: 22,
            user = config.ssh.profile.user.trim(),
            privateKeyMode = if (config.ssh.profile.privateKeyMode == "paste") {
                "paste"
            } else {
                "file"
            },
            hostKeySha256 = config.ssh.profile.hostKeySha256.trim(),
        )
        return config.copy(
            wormhole = config.wormhole.copy(
                rendezvousUrl = config.wormhole.rendezvousUrl.trim(),
                transitRelay = config.wormhole.transitRelay.trim(),
            ),
            ssh = config.ssh.copy(
                profile = profile,
                remotePort = config.ssh.remotePort.takeIf { it in 0..65535 } ?: 0,
            ),
        )
    }

    private fun readPrivateKey(context: Context, uri: Uri): String {
        val input = context.contentResolver.openInputStream(uri)
            ?: throw TransportException("无法读取 SSH 私钥文件")
        input.use {
            val output = ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            while (true) {
                val count = it.read(buffer)
                if (count < 0) break
                if (output.size() + count > MAX_PRIVATE_KEY_BYTES) {
                    throw TransportException("SSH 私钥文件过大")
                }
                output.write(buffer, 0, count)
            }
            return output.toString(Charsets.UTF_8.name())
        }
    }

    private fun parseLoopbackEndpoint(value: String): Pair<String, Int> {
        val parsed = try { URI("tcp://$value") } catch (error: Exception) {
            throw IOException("跨网核心端点无效", error)
        }
        val host = parsed.host ?: throw IOException("跨网核心端点无效")
        if (host !in setOf("127.0.0.1", "::1", "localhost") || parsed.port !in 1..65535) {
            throw IOException("跨网核心端点无效")
        }
        return host to parsed.port
    }

    private fun async(errorEvent: String, sessionId: String = "", action: () -> Unit) {
        Thread({
            try {
                action()
            } catch (error: Exception) {
                emit(errorEvent, JSONObject().apply {
                    if (sessionId.isNotEmpty()) put("session_id", sessionId)
                    put("error", error.message ?: "跨网操作失败")
                })
            }
        }, "inkhole-transport-call").apply { isDaemon = true }.start()
    }

    private fun emit(event: String, data: JSONObject) {
        listener?.onTransportEvent(event, data)
    }

    private fun requireContext(): Context = appContext
        ?: throw TransportException("跨网核心尚未启动")

    private fun secretName(profileId: String, kind: String) = "ssh:$profileId:$kind"
}
