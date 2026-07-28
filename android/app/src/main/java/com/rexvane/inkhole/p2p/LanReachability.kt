package com.rexvane.inkhole.p2p

import java.net.InetAddress

internal data class LanLink(val address: String, val prefixLength: Int)

/** Keeps mDNS-discovered peers tied to the active LAN instead of VPN fallback paths. */
internal object LanReachability {
    fun linkSignature(links: List<LanLink>): String = links
        .map { "${it.address.substringBefore('%')}/${it.prefixLength}" }
        .distinct()
        .sorted()
        .joinToString("|")

    /** Subnets present before a network event but absent afterwards. */
    fun departedLinks(before: List<LanLink>, after: List<LanLink>): List<LanLink> {
        val remaining = after.mapNotNull(::subnetKey).toSet()
        return before.distinctBy(::linkKey).filter { link ->
            subnetKey(link)?.let { it !in remaining } ?: false
        }
    }

    /** Every usable address of this peer lived exclusively on a departed subnet. */
    fun peerStrandedBy(hosts: List<String>, departed: List<LanLink>): Boolean {
        val addresses = hosts.map { it.substringBefore('%') }.filter { it.isNotBlank() }
            .distinct()
        return departed.isNotEmpty() && addresses.isNotEmpty() && addresses.all { host ->
            departed.any { link -> sameSubnet(host, link.address, link.prefixLength) }
        }
    }

    fun hostsOnCurrentLan(hosts: List<String>, links: List<LanLink>): List<String> =
        hosts.distinct().filter { host ->
            links.any { link -> sameSubnet(host, link.address, link.prefixLength) }
        }

    /**
     * NSD responses are received from a local multicast link. Android does not expose the
     * tethering/SoftAP subnet through ConnectivityManager on many devices, so fall back to
     * private addresses that came from the resolved NSD record itself when no known link
     * matches. TXT-only addresses are never trusted by this fallback.
     */
    fun discoveryCandidates(
        resolvedHosts: List<String>,
        advertisedHosts: List<String>,
        links: List<LanLink>,
    ): List<String> {
        val matched = hostsOnCurrentLan(advertisedHosts, links)
        if (matched.isNotEmpty()) return matched
        return resolvedHosts.distinct().filter(::isDirectLanAddress)
    }

    /** Keep a previously WHPC-verified LAN endpoint alive on SoftAP links hidden from Android. */
    fun verifiedPeerCandidates(
        hosts: List<String>,
        links: List<LanLink>,
        verifiedHost: String,
    ): List<String> {
        val matched = hostsOnCurrentLan(hosts, links)
        if (matched.isNotEmpty()) return matched
        return listOf(verifiedHost).filter(::isDirectLanAddress)
    }

    /** Excludes cellular and tunnel interfaces while retaining wlan/ap/ethernet variants. */
    fun isLanInterfaceName(name: String): Boolean {
        val normalized = name.trim().lowercase()
        if (normalized.isEmpty()) return false
        return listOf(
            "lo", "tun", "tap", "ppp", "vpn", "wg", "tailscale",
            "rmnet", "ccmni", "pdp", "wwan", "dummy", "clat",
        ).none(normalized::startsWith)
    }

    fun isDirectLanAddress(raw: String): Boolean {
        if (TailnetAddress.isTailnet(raw)) return false
        val address = try {
            InetAddress.getByName(raw.substringBefore('%'))
        } catch (_: Exception) {
            return false
        }
        if (address.isAnyLocalAddress || address.isLoopbackAddress ||
            address.isMulticastAddress) return false
        if (address.isLinkLocalAddress || address.isSiteLocalAddress) return true
        val bytes = address.address
        // java.net does not consistently classify IPv6 ULA (fc00::/7) as site-local.
        return bytes.size == 16 && (bytes[0].toInt() and 0xfe) == 0xfc
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

    private fun linkKey(link: LanLink): String =
        "${link.address.substringBefore('%')}/${link.prefixLength}"

    private fun subnetKey(link: LanLink): String? {
        val bytes = addressBytes(link.address)?.copyOf() ?: return null
        val prefix = link.prefixLength.coerceIn(0, bytes.size * 8)
        val wholeBytes = prefix / 8
        val remainingBits = prefix % 8
        for (index in bytes.indices) {
            when {
                index < wholeBytes -> Unit
                index == wholeBytes && remainingBits > 0 -> {
                    val mask = (0xff shl (8 - remainingBits)) and 0xff
                    bytes[index] = (bytes[index].toInt() and mask).toByte()
                }
                else -> bytes[index] = 0
            }
        }
        val network = bytes.joinToString("") { byte ->
            (byte.toInt() and 0xff).toString(16).padStart(2, '0')
        }
        return "$network/$prefix"
    }

    private fun addressBytes(raw: String): ByteArray? = try {
        InetAddress.getByName(raw.substringBefore('%')).address
    } catch (_: Exception) {
        null
    }
}
