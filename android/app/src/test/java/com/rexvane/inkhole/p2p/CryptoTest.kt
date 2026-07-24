package com.rexvane.inkhole.p2p

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

class CryptoTest {
    @Test
    fun decryptsPythonGeneratedWhe1Vector() {
        // Generated once with src/inkhole/crypto.py to lock cross-language compatibility.
        val encrypted = (
            "57484531d71fac321d5b1f6eb8c8dc25e9f6a2c28a120b7868d89c408b64f4f2" +
                "90b1b25823f9dd04da4e0d849acbbd243d5e1f43120f19a9e3d39f512e20fb703" +
                "74addc2a662"
            ).chunked(2).map { it.toInt(16).toByte() }.toByteArray()
        assertArrayEquals(
            "InkHole cross-language".toByteArray(),
            Crypto.decrypt("vector-secret", encrypted),
        )
    }

    @Test
    fun whe1RoundTripAndWrongPassword() {
        val plain = "inkhole-test".toByteArray()
        val encrypted = Crypto.encrypt("secret", plain)
        assertArrayEquals(plain, Crypto.decrypt("secret", encrypted))
        assertNull(Crypto.decrypt("wrong", encrypted))
    }

    @Test
    fun whe3RoundTripAndTamperDetection() {
        val first = ByteArray(1024) { (it % 251).toByte() }
        val second = "last-block".toByteArray()
        val encryptor = Crypto.ChunkedEncryptor("secret")
        val firstCiphertext = encryptor.encryptChunk(first)
        val secondCiphertext = encryptor.encryptChunk(second)
        val decryptor = Crypto.ChunkedDecryptor("secret", encryptor.streamHeader)

        assertEquals("WHE3", String(encryptor.streamHeader.copyOfRange(0, 4)))
        assertArrayEquals(first, decryptor.decryptChunk(firstCiphertext))
        assertArrayEquals(second, decryptor.decryptChunk(secondCiphertext))
        assertEquals(
            32L + first.size + second.size + 20L,
            Crypto.chunkedWireSize((first.size + second.size).toLong()),
        )

        val tampered = firstCiphertext.copyOf().also { it[it.lastIndex] = (it.last() + 1).toByte() }
        assertNull(Crypto.ChunkedDecryptor("secret", encryptor.streamHeader).decryptChunk(tampered))
    }

    @Test
    fun decryptsPythonGeneratedWhe2AndWhe3Vectors() {
        val plain = (
            "496e6b486f6c6520574845332063726f73732d6c616e6775616765"
            ).hexBytes()
        val legacyHeader = (
            "57484532000102030405060708090a0b0c0d0e0f101112131415161718191a1b"
            ).hexBytes()
        val legacyCiphertext = (
            "df85fc7844c8737be422dba9a27b866ab7f5854dc15e3291fb3bb4150cbaa8e7" +
                "cc58fb5af701253590bc08"
            ).hexBytes()
        val currentHeader = (
            "57484533000102030405060708090a0b0c0d0e0f101112131415161718191a1b"
            ).hexBytes()
        val currentCiphertext = (
            "25dffd16279c5bfadec69cd5cd6f661f8c77eb5461898456d174c1914705256b" +
                "134f05ba3060d520b7ef28"
            ).hexBytes()

        assertArrayEquals(
            plain,
            Crypto.ChunkedDecryptor("vector-secret", legacyHeader)
                .decryptChunk(legacyCiphertext),
        )
        assertArrayEquals(
            plain,
            Crypto.ChunkedDecryptor("vector-secret", currentHeader)
                .decryptChunk(currentCiphertext),
        )
        assertThrows(IllegalArgumentException::class.java) {
            Crypto.ChunkedDecryptor("vector-secret", ByteArray(3))
        }
    }

    private fun String.hexBytes(): ByteArray =
        chunked(2).map { it.toInt(16).toByte() }.toByteArray()
}
