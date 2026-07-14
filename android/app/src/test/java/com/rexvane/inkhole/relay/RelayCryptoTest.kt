package com.rexvane.inkhole.relay

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class RelayCryptoTest {
    private val vector: JSONObject = JSONObject(
        checkNotNull(javaClass.getResourceAsStream("/relay_crypto_v1.json"))
            .bufferedReader().use { it.readText() })

    private fun hex(value: String): ByteArray = value.chunked(2)
        .map { it.toInt(16).toByte() }.toByteArray()

    @Test
    fun sharedVectorMatchesPython() {
        val sender = DeviceIdentity.fromPrivateB64(vector.getString("sender_private"))
        val receiver = DeviceIdentity.fromPrivateB64(vector.getString("receiver_private"))
        val a = deriveTransferKey(sender, vector.getString("receiver_public"),
            vector.getString("transfer_id"), vector.getString("sender_id"),
            vector.getString("receiver_id"))
        val b = deriveTransferKey(receiver, vector.getString("sender_public"),
            vector.getString("transfer_id"), vector.getString("sender_id"),
            vector.getString("receiver_id"))
        assertArrayEquals(a, b)
        assertEquals(vector.getString("key_hex"), a.joinToString("") { "%02x".format(it) })
        val cipher = RelayCipher(a, vector.getString("transfer_id"),
            vector.getString("sender_id"), vector.getString("receiver_id"))
        assertArrayEquals(hex(vector.getString("direction0_frame_hex")),
            cipher.seal(0, hex(vector.getString("direction0_plain_hex"))))
        assertArrayEquals(hex(vector.getString("direction1_frame_hex")),
            cipher.seal(1, hex(vector.getString("direction1_plain_hex"))))
    }

    @Test
    fun tamperReplayAndOrderAreRejected() {
        val key = hex(vector.getString("key_hex"))
        val sender = RelayCipher(key, vector.getString("transfer_id"),
            vector.getString("sender_id"), vector.getString("receiver_id"))
        val receiver = RelayCipher(key, vector.getString("transfer_id"),
            vector.getString("sender_id"), vector.getString("receiver_id"))
        val first = sender.seal(0, "first".toByteArray())
        val second = sender.seal(0, "second".toByteArray())
        assertThrows(IllegalArgumentException::class.java) { receiver.open(second, 0) }
        assertArrayEquals("first".toByteArray(), receiver.open(first, 0))
        assertThrows(IllegalArgumentException::class.java) { receiver.open(first, 0) }
        second[second.lastIndex] = (second.last() + 1).toByte()
        assertThrows(Exception::class.java) { receiver.open(second, 0) }
    }

    @Test
    fun registryGroupKeyNormalizesCrossPlatformKeyText() {
        val lf = """-----BEGIN OPENSSH PRIVATE KEY-----
example
-----END OPENSSH PRIVATE KEY-----
"""
        val expected = hex("5ad1e1195b0c60a335bf0aa2710174d816ca2670493f35671cf548a8189e88d9")
        assertArrayEquals(expected, registryGroupKey(lf.toCharArray()))
        assertArrayEquals(expected, registryGroupKey(("\uFEFF" + lf.replace("\n", "\r\n")).toCharArray()))
    }
}
