package com.rexvane.wormhole.p2p

import javax.crypto.Cipher
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.PBEKeySpec
import javax.crypto.spec.SecretKeySpec
import java.security.SecureRandom

/**
 * AES-256-GCM 端到端加密, 与桌面版 Python crypto.py 完全兼容。
 *
 * 格式: [4B magic "WHE1"] [16B salt] [12B nonce] [ciphertext + 16B GCM tag]
 * 密钥派生: PBKDF2-HMAC-SHA256, 100000 次迭代, 256-bit。
 */
object Crypto {
    private val MAGIC = "WHE1".toByteArray(Charsets.US_ASCII)
    private const val SALT_LEN = 16
    private const val NONCE_LEN = 12
    private const val ITERATIONS = 100_000
    private const val KEY_LEN = 256     // bits
    private const val TAG_LEN = 128     // bits (GCM auth tag)

    fun isEncrypted(blob: ByteArray): Boolean {
        return blob.size >= 4 + SALT_LEN + NONCE_LEN && blob.copyOfRange(0, 4).contentEquals(MAGIC)
    }

    fun encrypt(secret: String, plain: ByteArray): ByteArray {
        val sr = SecureRandom()
        val salt = ByteArray(SALT_LEN).also { sr.nextBytes(it) }
        val nonce = ByteArray(NONCE_LEN).also { sr.nextBytes(it) }
        val key = deriveKey(secret, salt)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(TAG_LEN, nonce))
        val ct = cipher.doFinal(plain)
        // 拼接: magic + salt + nonce + ciphertext(含 tag)
        return MAGIC + salt + nonce + ct
    }

    fun decrypt(secret: String, blob: ByteArray): ByteArray? {
        if (!isEncrypted(blob)) return null
        val salt = blob.copyOfRange(4, 4 + SALT_LEN)
        val nonce = blob.copyOfRange(4 + SALT_LEN, 4 + SALT_LEN + NONCE_LEN)
        val ct = blob.copyOfRange(4 + SALT_LEN + NONCE_LEN, blob.size)
        val key = deriveKey(secret, salt)
        return try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(TAG_LEN, nonce))
            cipher.doFinal(ct)
        } catch (e: Exception) {
            null
        }
    }

    private fun deriveKey(secret: String, salt: ByteArray): ByteArray {
        val spec = PBEKeySpec(secret.toCharArray(), salt, ITERATIONS, KEY_LEN)
        val factory = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
        return factory.generateSecret(spec).encoded
    }
}
