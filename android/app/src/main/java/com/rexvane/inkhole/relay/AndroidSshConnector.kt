package com.rexvane.inkhole.relay

import net.schmizz.sshj.SSHClient
import net.schmizz.sshj.common.Buffer
import net.schmizz.sshj.connection.channel.forwarded.ConnectListener
import net.schmizz.sshj.connection.channel.forwarded.RemotePortForwarder
import net.schmizz.sshj.transport.verification.HostKeyVerifier
import net.schmizz.sshj.userauth.UserAuthException
import net.schmizz.sshj.userauth.password.PasswordUtils
import java.security.MessageDigest
import java.security.PublicKey

class AndroidSshConnector {
    private val commonUsers = listOf("root", "ubuntu", "debian", "ec2-user", "admin")

    fun probeHostKey(host: String, port: Int): AndroidHostKey {
        var result: AndroidHostKey? = null
        SSHClient().use { ssh ->
            ssh.connectTimeout = 10_000
            ssh.addHostKeyVerifier(object : HostKeyVerifier {
                override fun verify(hostname: String, remotePort: Int, key: PublicKey): Boolean {
                    result = AndroidHostKey(key.algorithm, sshFingerprint(key))
                    return true
                }

                override fun findExistingAlgorithms(hostname: String, port: Int) =
                    emptyList<String>()
            })
            ssh.connect(host, port)
            ssh.disconnect()
        }
        return checkNotNull(result) { "服务器未提供 SSH 主机密钥" }
    }

    fun validate(credentials: AndroidSshCredentials,
                 confirmed: AndroidHostKey): AndroidSshCredentials {
        var ssh: SSHClient? = null
        try {
            val connected = connect(credentials, confirmed)
            ssh = connected.first
            val actual = connected.second
            ssh.newSFTPClient().use { it.canonicalize(".") }
            val forwarder = ssh.remotePortForwarder
            val forward = forwarder.bind(
                RemotePortForwarder.Forward("127.0.0.1", 0),
                ConnectListener { channel ->
                    channel.confirm()
                    channel.close()
                },
            )
            try {
                check(forward.port > 0) { "服务器未分配 SSH 反向转发端口" }
            } finally {
                forwarder.cancel(forward)
            }
            return credentials.copy(username = actual)
        } catch (e: Exception) {
            val message = e.message.orEmpty().lowercase()
            if ("administratively prohibited" in message || "port forwarding" in message) {
                throw IllegalStateException(
                    "服务器禁止 SSH TCP 转发，请启用 AllowTcpForwarding", e)
            }
            throw e
        } finally {
            runCatching { ssh?.disconnect() }
            runCatching { ssh?.close() }
        }
    }

    internal fun authenticated(credentials: AndroidSshCredentials,
                               confirmed: AndroidHostKey): SSHClient =
        connect(credentials, confirmed).first

    private fun connect(credentials: AndroidSshCredentials,
                        confirmed: AndroidHostKey): Pair<SSHClient, String> {
        val candidates = if (credentials.username.isBlank()) commonUsers
            else listOf(credentials.username)
        var lastAuth: Exception? = null
        candidates.distinct().forEach { username ->
            val ssh = SSHClient()
            ssh.connectTimeout = 12_000
            ssh.timeout = 0
            ssh.addHostKeyVerifier(object : HostKeyVerifier {
                override fun verify(hostname: String, port: Int, key: PublicKey): Boolean =
                    key.algorithm == confirmed.algorithm &&
                        sshFingerprint(key) == confirmed.fingerprint

                override fun findExistingAlgorithms(hostname: String, port: Int) =
                    emptyList<String>()
            })
            try {
                ssh.connect(credentials.host, credentials.port)
                val finder = credentials.passphrase.takeIf { it.isNotEmpty() }
                    ?.let { PasswordUtils.createOneOff(it) }
                val provider = ssh.loadKeys(String(credentials.privateKey), null, finder)
                ssh.authPublickey(username, provider)
                ssh.connection.keepAlive.keepAliveInterval = 30
                return ssh to username
            } catch (e: UserAuthException) {
                lastAuth = e
                runCatching { ssh.close() }
            } catch (e: Exception) {
                runCatching { ssh.close() }
                throw e
            }
        }
        throw IllegalStateException(
            "SSH 密钥鉴权失败，请在高级设置中确认用户名", lastAuth)
    }

    private fun sshFingerprint(key: PublicKey): String {
        val blob = Buffer.PlainBuffer().putPublicKey(key).compactData
        val digest = MessageDigest.getInstance("SHA-256").digest(blob)
        return "SHA256:" + java.util.Base64.getEncoder().withoutPadding()
            .encodeToString(digest)
    }
}
