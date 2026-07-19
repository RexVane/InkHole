package com.rexvane.inkhole.p2p

import java.net.Inet4Address
import java.net.Inet6Address
import java.net.InetAddress

/** Numeric Tailnet address classification shared by probing and transfers. */
internal object TailnetAddress {
    fun isTailnet(raw: String): Boolean {
        val address = numericAddress(raw) ?: return false
        return when (address) {
            is Inet4Address -> {
                val bytes = address.address
                (bytes[0].toInt() and 0xff) == 100 &&
                    (bytes[1].toInt() and 0xc0) == 64
            }
            is Inet6Address -> {
                val bytes = address.address
                (bytes[0].toInt() and 0xff) == 0xfd &&
                    (bytes[1].toInt() and 0xff) == 0x7a &&
                    (bytes[2].toInt() and 0xff) == 0x11 &&
                    (bytes[3].toInt() and 0xff) == 0x5c &&
                    (bytes[4].toInt() and 0xff) == 0xa1 &&
                    (bytes[5].toInt() and 0xff) == 0xe0
            }
            else -> false
        }
    }

    fun numericAddress(raw: String): InetAddress? {
        val value = raw.substringBefore('%')
        val looksV4 = value.isNotEmpty() && value.all { it.isDigit() || it == '.' }
        val looksV6 = ':' in value && value.all {
            it.isDigit() || it.lowercaseChar() in 'a'..'f' || it == ':' || it == '.'
        }
        if (!looksV4 && !looksV6) return null
        return try {
            InetAddress.getByName(value)
        } catch (_: Exception) {
            null
        }
    }

    fun order(addresses: List<String>): List<String> =
        addresses.distinct().sortedBy { if (isTailnet(it)) 1 else 0 }
}
