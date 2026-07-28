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

    @Test
    fun hotspotDiscoveryFallsBackOnlyToResolvedPrivateAddresses() {
        val resolved = listOf("10.237.115.39", "100.66.227.31", "127.0.0.1")
        val advertised = resolved + "192.168.50.8"
        val unrelatedUpstream = listOf(LanLink("192.168.8.5", 24))

        assertEquals(
            listOf("10.237.115.39"),
            LanReachability.discoveryCandidates(resolved, advertised, unrelatedUpstream),
        )
        assertEquals(
            listOf("10.237.115.39"),
            LanReachability.verifiedPeerCandidates(
                advertised, unrelatedUpstream, "10.237.115.39"),
        )
        assertFalse(LanReachability.isDirectLanAddress("8.8.8.8"))
        assertFalse(LanReachability.isDirectLanAddress("224.0.0.251"))
    }

    @Test
    fun normalWifiStillUsesStrictSubnetFiltering() {
        val resolved = listOf("192.168.8.20", "192.168.9.20")
        val links = listOf(LanLink("192.168.8.5", 24))

        assertEquals(
            listOf("192.168.8.20"),
            LanReachability.discoveryCandidates(resolved, resolved, links),
        )
    }

    @Test
    fun interfaceFilterRetainsWifiAndSoftApButRejectsTunnelsAndCellular() {
        assertTrue(LanReachability.isLanInterfaceName("wlan0"))
        assertTrue(LanReachability.isLanInterfaceName("ap0"))
        assertTrue(LanReachability.isLanInterfaceName("eth0"))
        assertFalse(LanReachability.isLanInterfaceName("tun0"))
        assertFalse(LanReachability.isLanInterfaceName("rmnet_data0"))
        assertFalse(LanReachability.isLanInterfaceName("ppp0"))
    }

    @Test
    fun networkSignatureIsStableAcrossOrderingAndDuplicates() {
        val first = listOf(
            LanLink("192.168.8.5", 24),
            LanLink("10.0.0.4", 24),
            LanLink("192.168.8.5", 24),
        )
        val second = listOf(LanLink("10.0.0.4", 24), LanLink("192.168.8.5", 24))
        assertEquals(
            LanReachability.linkSignature(first),
            LanReachability.linkSignature(second),
        )
    }

    @Test
    fun departedLinksCompareSubnetsInsteadOfDhcpAddresses() {
        val before = listOf(
            LanLink("192.168.8.5", 24),
            LanLink("10.0.0.4", 24),
        )
        val sameWifiWithNewAddress = listOf(
            LanLink("192.168.8.99", 24),
            LanLink("172.16.1.3", 24),
        )
        assertEquals(
            listOf(LanLink("10.0.0.4", 24)),
            LanReachability.departedLinks(before, sameWifiWithNewAddress),
        )
    }

    @Test
    fun onlyPeersConfinedToDepartedSubnetAreStranded() {
        val departed = listOf(LanLink("192.168.8.5", 24))
        assertTrue(LanReachability.peerStrandedBy(
            listOf("192.168.8.20"), departed))
        assertFalse(LanReachability.peerStrandedBy(
            listOf("192.168.8.20", "100.96.1.8"), departed))
        assertFalse(LanReachability.peerStrandedBy(
            listOf("100.96.1.8"), departed))
    }
}
