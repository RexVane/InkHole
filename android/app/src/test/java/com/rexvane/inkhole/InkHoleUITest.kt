package com.rexvane.inkhole

import com.rexvane.inkhole.p2p.Peer
import org.junit.Assert.assertEquals
import org.junit.Test

class InkHoleUITest {
    private val instanceId = "0123456789abcdef0123456789abcdef"

    @Test
    fun lanSubtitleUsesInstanceIdForEveryDiscoveryPath() {
        val mdns = Peer(
            "Mac", "192.168.1.2", 41300,
            serviceName = "Mac-01234567._inkhole._tcp.local.",
            instanceId = instanceId,
        )
        val reverseHint = mdns.copy(serviceName = "hint|$instanceId")

        assertEquals("01234567", deviceSubline(mdns))
        assertEquals("01234567", deviceSubline(reverseHint))
    }

    @Test
    fun crossNetworkSubtitleKeepsTransportSpecificLabel() {
        assertEquals(
            "100.64.0.2:41300",
            deviceSubline(Peer(
                "Phone", "100.64.0.2", 41300,
                instanceId = instanceId, manual = true,
            )),
        )
        assertEquals(
            "SSH 中继",
            deviceSubline(Peer(
                "Phone", "127.0.0.1", 41000,
                instanceId = instanceId, manual = true, transport = "ssh",
            )),
        )
        assertEquals(
            "一次性短码",
            deviceSubline(Peer(
                "Receiver", "127.0.0.1", 41001,
                instanceId = instanceId, manual = true, transport = "wormhole",
            )),
        )
    }
}
