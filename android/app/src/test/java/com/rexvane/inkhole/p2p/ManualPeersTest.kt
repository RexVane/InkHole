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
        assertNull(ManualPeers.normalizeHost("https://device.tailnet.ts.net"))
        assertNull(ManualPeers.normalizeHost("999.999.999.999"))
    }
}
