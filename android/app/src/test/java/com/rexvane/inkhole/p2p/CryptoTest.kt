package com.rexvane.inkhole.p2p

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
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
    fun whe2RoundTripAndTamperDetection() {
        val first = ByteArray(1024) { (it % 251).toByte() }
        val second = "last-block".toByteArray()
        val encryptor = Crypto.ChunkedEncryptor("secret")
        val firstCiphertext = encryptor.encryptChunk(first)
        val secondCiphertext = encryptor.encryptChunk(second)
        val decryptor = Crypto.ChunkedDecryptor("secret", encryptor.streamHeader)

        assertArrayEquals(first, decryptor.decryptChunk(firstCiphertext))
        assertArrayEquals(second, decryptor.decryptChunk(secondCiphertext))
        assertEquals(
            32L + first.size + second.size + 20L,
            Crypto.chunkedWireSize((first.size + second.size).toLong()),
        )

        val tampered = firstCiphertext.copyOf().also { it[it.lastIndex] = (it.last() + 1).toByte() }
        assertNull(Crypto.ChunkedDecryptor("secret", encryptor.streamHeader).decryptChunk(tampered))
    }
}
