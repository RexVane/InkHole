package com.rexvane.inkhole.p2p

import org.junit.Assert.assertEquals
import org.junit.Test

class PeerTransportTest {
    @Test
    fun defaultsDistinguishLanAndTailscale() {
        assertEquals("lan", Peer("LAN", "192.168.1.2", 9000).transport)
        assertEquals("tailscale", Peer("VPN", "100.64.0.2", 9000, manual = true).transport)
    }

    @Test
    fun externalPeerCarriesCapabilityToken() {
        val peer = Peer(
            "短码", "127.0.0.1", 24000,
            serviceName = "external|wormhole|session",
            manual = true,
            transport = "wormhole",
            endpointToken = "token",
        )
        assertEquals("wormhole", peer.transport)
        assertEquals("token", peer.endpointToken)
    }
}
