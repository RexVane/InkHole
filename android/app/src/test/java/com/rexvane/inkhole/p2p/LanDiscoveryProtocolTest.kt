package com.rexvane.inkhole.p2p

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class LanDiscoveryProtocolTest {
    @Test
    fun announcementRoundTrips() {
        val id = "0123456789abcdef0123456789abcdef"
        assertEquals(
            LanAnnouncement(id, 41300),
            LanDiscoveryProtocol.decode(LanDiscoveryProtocol.encode(id, 41300)),
        )
    }

    @Test
    fun malformedOrWrongVersionAnnouncementIsRejected() {
        assertNull(LanDiscoveryProtocol.decode("{}".toByteArray()))
        assertNull(LanDiscoveryProtocol.decode(
            """{"magic":"inkhole-lan-v1","version":2,"instance_id":"0123456789abcdef0123456789abcdef","port":41300}"""
                .toByteArray(),
        ))
    }

    @Test
    fun hotspotSubnetProducesDirectedBroadcast() {
        assertEquals(
            listOf("255.255.255.255", "10.237.115.255", "192.168.7.15"),
            LanDiscoveryProtocol.broadcastTargets(
                listOf(LanLink("10.237.115.7", 24), LanLink("192.168.7.9", 29))),
        )
    }

    @Test
    fun reverseLanHintMatchesDesktopFrame() {
        val id = "0123456789abcdef0123456789abcdef"
        val frame = LanHintProtocol.encode(id, 41300)
        assertEquals(39, frame.size)
        assertEquals(LanHint(id, 41300), LanHintProtocol.decode(frame))
        frame[4] = 2
        assertNull(LanHintProtocol.decode(frame))
    }
}
