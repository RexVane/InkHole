package com.rexvane.inkhole.p2p

import java.net.InetAddress

internal data class LanLink(val address: String, val prefixLength: Int)

/** Keeps mDNS-discovered peers tied to the active LAN instead of VPN fallback paths. */
internal object LanReachability {
    fun hostsOnCurrentLan(hosts: List<String>, links: List<LanLink>): List<String> =
        hosts.distinct().filter { host ->
            links.any { link -> sameSubnet(host, link.address, link.prefixLength) }
        }

    fun sameSubnet(peerAddress: String, localAddress: String, prefixLength: Int): Boolean {
        val peer = addressBytes(peerAddress) ?: return false
        val local = addressBytes(localAddress) ?: return false
        if (peer.size != local.size) return false

        val prefix = prefixLength.coerceIn(0, peer.size * 8)
        val wholeBytes = prefix / 8
        val remainingBits = prefix % 8
        for (index in 0 until wholeBytes) {
            if (peer[index] != local[index]) return false
        }
        if (remainingBits == 0) return true
        val mask = (0xff shl (8 - remainingBits)) and 0xff
        return (peer[wholeBytes].toInt() and mask) ==
            (local[wholeBytes].toInt() and mask)
    }

    private fun addressBytes(raw: String): ByteArray? = try {
        InetAddress.getByName(raw.substringBefore('%')).address
    } catch (_: Exception) {
        null
    }
}
