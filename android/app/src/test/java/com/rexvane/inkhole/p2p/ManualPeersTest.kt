package com.rexvane.inkhole.p2p

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ManualPeersTest {
    @Test
    fun continuousDigitsAreSegmented() {
        assertEquals("100.127.46.26", ManualPeers.maskTyping("1001274626"))
    }

    @Test
    fun hostInputIsNormalizedWithoutGuessingAmbiguousValues() {
        assertEquals("100.127.46.26", ManualPeers.normalizeHost("100127.46.26"))
        assertEquals("device.tailnet.ts.net", ManualPeers.normalizeHost("device.tailnet.ts.net"))
        assertEquals("device.tailnet.ts.net", ManualPeers.normalizeHost("DEVICE.TAILNET.TS.NET."))
        assertEquals("fd7a:115c:a1e0::1", ManualPeers.normalizeHost("FD7A:115C:A1E0::1"))
        assertEquals("2001:0:0::1", ManualPeers.maskTyping("2001:0:0::1"))
        assertNull(ManualPeers.normalizeHost("https://device.tailnet.ts.net"))
        assertNull(ManualPeers.normalizeHost("fd7a:115c:a1e0::gg"))
        assertNull(ManualPeers.normalizeHost("999.999.999.999"))
    }

    @Test
    fun pinnedIdentitySurvivesJsonRoundTrip() {
        val expected = listOf(ManualPeer(
            "工作电脑", "device.tailnet.ts.net", 41300,
            "0123456789abcdef0123456789abcdef",
        ))

        assertEquals(expected, ManualPeers.decode(ManualPeers.encode(expected)))
    }

    @Test
    fun staleSettingsDraftCannotEraseOrReplacePinnedIdentity() {
        val identity = "0123456789abcdef0123456789abcdef"
        val persisted = listOf(ManualPeer("旧备注", "100.64.0.2", 41300, identity))
        val drafts = listOf(
            ManualPeer("新备注", "100.64.0.2", 41300),
            ManualPeer(
                "伪造身份", "100.64.0.2", 41300,
                "fedcba9876543210fedcba9876543210"),
            ManualPeer("新地址", "100.64.0.3", 41300),
        )

        val merged = ManualPeers.preservePinnedIdentities(drafts, persisted)
        assertEquals(identity, merged[0].instanceId)
        assertEquals(identity, merged[1].instanceId)
        assertNull(merged[2].instanceId)
    }
}
