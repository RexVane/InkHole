package com.rexvane.inkhole.relay

import android.content.Context
import java.util.UUID

data class AndroidHostKey(val algorithm: String, val fingerprint: String)

data class AndroidSshCredentials(
    val host: String,
    val username: String,
    val port: Int,
    val privateKey: CharArray,
    val passphrase: CharArray,
) {
    fun clear() {
        privateKey.fill('\u0000')
        passphrase.fill('\u0000')
    }
}

data class SshRelaySession(
    val credentials: AndroidSshCredentials,
    val registryKey: ByteArray,
)

object SshRelayRuntime {
    @Volatile private var current: SshRelaySession? = null

    @Synchronized
    fun install(session: SshRelaySession) {
        current?.credentials?.clear()
        current?.registryKey?.fill(0)
        current = session
    }

    fun get(): SshRelaySession? = current

    @Synchronized
    fun clear() {
        current?.credentials?.clear()
        current?.registryKey?.fill(0)
        current = null
    }
}

data class RelaySettings(
    val host: String,
    val username: String,
    val port: Int,
    val hostKey: AndroidHostKey,
    val deviceId: String,
    val privateKey: String,
    val publicKey: String,
) {
    fun save(context: Context) {
        context.getSharedPreferences("inkhole", Context.MODE_PRIVATE).edit()
            .putString("ssh_relay_host", host)
            .putString("ssh_relay_username", username)
            .putInt("ssh_relay_port", port)
            .putString("ssh_relay_host_key_algorithm", hostKey.algorithm)
            .putString("ssh_relay_host_key_fingerprint", hostKey.fingerprint)
            .putString("ssh_relay_device_id", deviceId)
            .putString("ssh_relay_identity_private", privateKey)
            .putString("ssh_relay_identity_public", publicKey)
            .apply()
    }

    fun sameServer(host: String, username: String, port: Int,
                   hostKey: AndroidHostKey): Boolean =
        this.host == host && this.username == username && this.port == port &&
            this.hostKey == hostKey

    companion object {
        fun loadProfile(context: Context): RelaySettings? {
            val prefs = context.getSharedPreferences("inkhole", Context.MODE_PRIVATE)
            val host = prefs.getString("ssh_relay_host", null) ?: return null
            val username = prefs.getString("ssh_relay_username", null) ?: return null
            val algorithm = prefs.getString("ssh_relay_host_key_algorithm", null) ?: return null
            val fingerprint = prefs.getString("ssh_relay_host_key_fingerprint", null) ?: return null
            val deviceId = prefs.getString("ssh_relay_device_id", null) ?: return null
            val privateKey = prefs.getString("ssh_relay_identity_private", null) ?: return null
            val publicKey = prefs.getString("ssh_relay_identity_public", null) ?: return null
            val port = prefs.getInt("ssh_relay_port", 22)
            if (port !in 1..65535 || !deviceId.matches(Regex("[0-9a-f]{32}"))) return null
            return RelaySettings(host, username, port,
                AndroidHostKey(algorithm, fingerprint), deviceId, privateKey, publicKey)
        }

        fun load(context: Context): RelaySettings? {
            val profile = loadProfile(context) ?: return null
            val runtime = SshRelayRuntime.get() ?: return null
            return profile.takeIf {
                runtime.credentials.host == it.host &&
                    runtime.credentials.username == it.username &&
                    runtime.credentials.port == it.port
            }
        }

        fun create(
            context: Context,
            host: String,
            username: String,
            port: Int,
            hostKey: AndroidHostKey,
        ): RelaySettings {
            val existing = loadProfile(context)
            val identity = if (existing?.sameServer(host, username, port, hostKey) == true) {
                DeviceKeyPair(existing.privateKey, existing.publicKey)
            } else {
                DeviceKeyPair.generate()
            }
            val deviceId = if (existing?.sameServer(host, username, port, hostKey) == true) {
                existing.deviceId
            } else {
                UUID.randomUUID().toString().replace("-", "")
            }
            return RelaySettings(host, username, port, hostKey, deviceId,
                identity.privateB64, identity.publicB64).also { it.save(context) }
        }

        fun clear(context: Context) {
            SshRelayRuntime.clear()
            context.getSharedPreferences("inkhole", Context.MODE_PRIVATE).edit()
                .remove("ssh_relay_host")
                .remove("ssh_relay_username")
                .remove("ssh_relay_port")
                .remove("ssh_relay_host_key_algorithm")
                .remove("ssh_relay_host_key_fingerprint")
                .remove("ssh_relay_device_id")
                .remove("ssh_relay_identity_private")
                .remove("ssh_relay_identity_public")
                .apply()
        }
    }
}
