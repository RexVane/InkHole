package com.rexvane.inkhole.p2p

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TailnetAddressTest {
    @Test
    fun classifiesTailnetAddressRangesExactly() {
        assertTrue(TailnetAddress.isTailnet("100.64.0.1"))
        assertTrue(TailnetAddress.isTailnet("100.127.255.255"))
        assertFalse(TailnetAddress.isTailnet("100.128.0.1"))
        assertTrue(TailnetAddress.isTailnet("fd7a:115c:a1e0::1"))
        assertFalse(TailnetAddress.isTailnet("2001:db8::1"))
        assertFalse(TailnetAddress.isTailnet("device.tailnet.ts.net"))
    }

    @Test
    fun directAddressesArePreferredOverTailnetFallbacks() {
        assertEquals(
            listOf("192.168.1.20", "100.64.0.1", "fd7a:115c:a1e0::1"),
            TailnetAddress.order(listOf(
                "100.64.0.1", "192.168.1.20", "fd7a:115c:a1e0::1")),
        )
    }
}
