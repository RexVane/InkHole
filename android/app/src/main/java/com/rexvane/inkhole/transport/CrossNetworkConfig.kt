package com.rexvane.inkhole.transport

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

data class WormholeConfig(
    val rendezvousUrl: String = "",
    val transitRelay: String = "",
)

data class SSHProfileConfig(
    val id: String = UUID.randomUUID().toString().replace("-", ""),
    val host: String = "",
    val port: Int = 22,
    val user: String = "",
    val privateKeyMode: String = "file",
    val privateKeyUri: String = "",
    val privateKeyLabel: String = "",
    val hostKeySha256: String = "",
)

data class SSHPeerConfig(
    val id: String,
    val name: String,
    val instanceId: String,
    val remotePort: Int,
    val noisePublic: String,
    val endToEnd: Boolean = true,
)

data class SSHRelayConfig(
    val enabled: Boolean = false,
    val profile: SSHProfileConfig = SSHProfileConfig(),
    val remotePort: Int = 0,
    val peers: List<SSHPeerConfig> = emptyList(),
)

data class CrossNetworkConfig(
    val wormhole: WormholeConfig = WormholeConfig(),
    val ssh: SSHRelayConfig = SSHRelayConfig(),
)

object CrossNetworkStore {
    private const val PREFS = "inkhole"
    private const val KEY = "cross_network_v1"

    fun load(context: Context): CrossNetworkConfig {
        val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY, null) ?: return CrossNetworkConfig()
        return try {
            decode(JSONObject(raw))
        } catch (_: Exception) {
            CrossNetworkConfig()
        }
    }

    fun save(context: Context, config: CrossNetworkConfig) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(KEY, encode(config).toString()).apply()
    }

    fun encode(config: CrossNetworkConfig): JSONObject = JSONObject().apply {
        put("wormhole", JSONObject().apply {
            put("rendezvous_url", config.wormhole.rendezvousUrl.trim())
            put("transit_relay", config.wormhole.transitRelay.trim())
        })
        put("ssh", JSONObject().apply {
            put("enabled", config.ssh.enabled)
            put("remote_port", config.ssh.remotePort)
            put("profile", JSONObject().apply {
                val profile = config.ssh.profile
                put("id", profile.id)
                put("host", profile.host.trim())
                put("port", profile.port)
                put("user", profile.user.trim())
                put("private_key_mode", profile.privateKeyMode)
                put("private_key_uri", profile.privateKeyUri)
                put("private_key_label", profile.privateKeyLabel)
                put("host_key_sha256", profile.hostKeySha256)
            })
            put("peers", JSONArray().apply {
                config.ssh.peers.forEach { peer ->
                    put(JSONObject().apply {
                        put("id", peer.id)
                        put("name", peer.name)
                        put("instance_id", peer.instanceId)
                        put("remote_port", peer.remotePort)
                        put("noise_public", peer.noisePublic)
                        put("end_to_end", peer.endToEnd)
                    })
                }
            })
        })
    }

    fun decode(root: JSONObject): CrossNetworkConfig {
        val wormhole = root.optJSONObject("wormhole") ?: JSONObject()
        val ssh = root.optJSONObject("ssh") ?: JSONObject()
        val profile = ssh.optJSONObject("profile") ?: JSONObject()
        val profileId = profile.optString("id").trim()
            .ifEmpty { UUID.randomUUID().toString().replace("-", "") }
        val profilePort = profile.optInt("port", 22).takeIf { it in 1..65535 } ?: 22
        val remotePort = ssh.optInt("remote_port", 0).takeIf { it in 0..65535 } ?: 0
        val peers = ArrayList<SSHPeerConfig>()
        val rawPeers = ssh.optJSONArray("peers") ?: JSONArray()
        for (index in 0 until rawPeers.length()) {
            val value = rawPeers.optJSONObject(index) ?: continue
            val instanceId = value.optString("instance_id").trim()
            val noisePublic = value.optString("noise_public").trim()
            val port = value.optInt("remote_port", 0)
            if (instanceId.isEmpty() || noisePublic.isEmpty() || port !in 1..65535) continue
            peers += SSHPeerConfig(
                id = value.optString("id", instanceId).ifEmpty { instanceId },
                name = value.optString("name", "SSH 设备").ifEmpty { "SSH 设备" },
                instanceId = instanceId,
                remotePort = port,
                noisePublic = noisePublic,
                endToEnd = value.optBoolean("end_to_end", true),
            )
        }
        return CrossNetworkConfig(
            wormhole = WormholeConfig(
                rendezvousUrl = wormhole.optString("rendezvous_url").trim(),
                transitRelay = wormhole.optString("transit_relay").trim(),
            ),
            ssh = SSHRelayConfig(
                enabled = ssh.optBoolean("enabled", false),
                profile = SSHProfileConfig(
                    id = profileId,
                    host = profile.optString("host").trim(),
                    port = profilePort,
                    user = profile.optString("user").trim(),
                    privateKeyMode = if (profile.optString("private_key_mode") == "paste") {
                        "paste"
                    } else {
                        "file"
                    },
                    privateKeyUri = profile.optString("private_key_uri"),
                    privateKeyLabel = profile.optString("private_key_label"),
                    hostKeySha256 = profile.optString("host_key_sha256").trim(),
                ),
                remotePort = remotePort,
                peers = peers,
            ),
        )
    }
}
