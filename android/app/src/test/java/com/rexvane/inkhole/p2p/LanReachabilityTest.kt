package com.rexvane.inkhole.p2p

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LanReachabilityTest {
    @Test
    fun sameSubnetMatchesOnlyTheActiveLanPrefix() {
        assertTrue(LanReachability.sameSubnet("192.168.8.20", "192.168.8.5", 24))
        assertFalse(LanReachability.sameSubnet("192.168.9.20", "192.168.8.5", 24))
        assertTrue(LanReachability.sameSubnet("10.25.56.228", "10.25.48.9", 20))
        assertFalse(LanReachability.sameSubnet("100.96.1.8", "10.25.48.9", 20))
    }

    @Test
    fun automaticPeersCannotStayAliveThroughTailscaleFallback() {
        val advertised = listOf("192.168.8.20", "100.96.1.8", "127.0.0.1")
        val links = listOf(LanLink("192.168.8.5", 24))

        assertEquals(
            listOf("192.168.8.20"),
            LanReachability.hostsOnCurrentLan(advertised, links),
        )
        assertEquals(emptyList<String>(), LanReachability.hostsOnCurrentLan(advertised, emptyList()))
    }

    @Test
    fun ipv6PrefixComparisonSupportsScopedAddresses() {
        assertTrue(LanReachability.sameSubnet("fe80::1234%wlan0", "fe80::5678%wlan0", 64))
        assertFalse(LanReachability.sameSubnet("fd00:1::2", "fd00:2::3", 64))
    }
}
