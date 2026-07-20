package com.rexvane.inkhole.transport

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CrossNetworkConfigTest {
    @Test
    fun roundTripPreservesNonSecretRelayConfiguration() {
        val source = CrossNetworkConfig(
            wormhole = WormholeConfig(" wss://mailbox.example/v1 ", "tcp://relay:4001"),
            ssh = SSHRelayConfig(
                enabled = true,
                profile = SSHProfileConfig(
                    id = "profile-1",
                    host = " relay.example ",
                    port = 2222,
                    user = "inkhole",
                    privateKeyMode = "paste",
                    privateKeyLabel = "已安全保存",
                    hostKeySha256 = "SHA256:example",
                ),
                remotePort = 24567,
                peers = listOf(
                    SSHPeerConfig(
                        id = "peer-1",
                        name = "Mac",
                        instanceId = "a".repeat(32),
                        remotePort = 24568,
                        noisePublic = "noise-public",
                        endToEnd = false,
                    ),
                ),
            ),
        )

        val encoded = CrossNetworkStore.encode(source)
        val decoded = CrossNetworkStore.decode(JSONObject(encoded.toString()))

        assertEquals("wss://mailbox.example/v1", decoded.wormhole.rendezvousUrl)
        assertEquals("relay.example", decoded.ssh.profile.host)
        assertEquals(2222, decoded.ssh.profile.port)
        assertEquals(24567, decoded.ssh.remotePort)
        assertEquals(1, decoded.ssh.peers.size)
        assertFalse(decoded.ssh.peers.single().endToEnd)
        assertFalse(encoded.getJSONObject("ssh").getJSONObject("profile").has("private_key"))
        assertFalse(encoded.toString().contains("passphrase"))
        assertFalse(encoded.toString().contains("noise_private"))
    }

    @Test
    fun invalidPeersAndPortsAreDroppedOrNormalized() {
        val decoded = CrossNetworkStore.decode(JSONObject("""
            {
              "ssh": {
                "remote_port": 99999,
                "profile": {"port": -2, "private_key_mode": "other"},
                "peers": [
                  {"instance_id":"", "remote_port":12, "noise_public":"x"},
                  {"instance_id":"peer", "remote_port":0, "noise_public":"x"}
                ]
              }
            }
        """.trimIndent()))

        assertEquals(22, decoded.ssh.profile.port)
        assertEquals(0, decoded.ssh.remotePort)
        assertEquals("file", decoded.ssh.profile.privateKeyMode)
        assertTrue(decoded.ssh.peers.isEmpty())
    }

    @Test
    fun systemProxyIsFormattedForTheGoCore() {
        assertEquals("http://proxy.example:8080", formatHTTPProxyURL(" proxy.example ", 8080))
        assertEquals("http://[2001:db8::1]:7890", formatHTTPProxyURL("2001:db8::1", 7890))
        assertEquals("", formatHTTPProxyURL("", 8080))
        assertEquals("", formatHTTPProxyURL("proxy.example", 70000))
    }
}
