package com.rexvane.inkhole.p2p

import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
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

    @Test
    fun failedDirectRouteCanFallBackToPairedSshForSameDevice() {
        val instanceId = "a".repeat(32)
        val direct = Peer(
            "Mac", "100.64.0.2", 9000,
            serviceName = "manual|100.64.0.2|9000",
            instanceId = instanceId,
            manual = true,
            transport = "tailscale",
            identityFingerprint = "b".repeat(64),
        )
        val ssh = Peer(
            "Mac (2)", "127.0.0.1", 24000,
            serviceName = "external|ssh|$instanceId",
            instanceId = instanceId,
            manual = true,
            transport = "ssh",
            endpointToken = "token",
        )

        val routes = transferRouteCandidates(direct, listOf(direct, ssh), emptyMap())

        assertEquals(2, routes.size)
        assertSame(direct, routes[0])
        assertSame(ssh, routes[1])
    }

    @Test
    fun sshDoesNotFallBackToUnpinnedDirectRoute() {
        val instanceId = "a".repeat(32)
        val ssh = Peer(
            "Mac", "127.0.0.1", 24000,
            serviceName = "external|ssh|$instanceId",
            instanceId = instanceId,
            manual = true,
            transport = "ssh",
            endpointToken = "token",
        )
        val direct = Peer(
            "Mac (2)", "192.168.1.2", 9000,
            serviceName = "Mac-$instanceId._inkhole._tcp.local.",
            instanceId = instanceId,
            identityFingerprint = "b".repeat(64),
        )

        val routes = transferRouteCandidates(ssh, listOf(ssh, direct), emptyMap())

        assertEquals(listOf(ssh), routes)
    }
}
