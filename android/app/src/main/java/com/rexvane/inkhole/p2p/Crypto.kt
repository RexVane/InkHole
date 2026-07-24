package com.rexvane.inkhole.p2p

import javax.crypto.Cipher
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.PBEKeySpec
import javax.crypto.spec.SecretKeySpec
import java.nio.ByteBuffer
import java.security.SecureRandom

/**
 * AES-256-GCM 端到端加密, 与桌面版 Python crypto.py 逐字节兼容。
 *
 * 整块格式 WHE1(小文件): [4B "WHE1"] [16B salt] [12B nonce] [ct + 16B tag]
 * 分块格式 WHE2/WHE3(大文件): [4B magic] [16B salt] [12B base_nonce] + 帧*
 *   帧 = [4B 密文长度(BE)] + 密文
 *   第 i 块: nonce_i = base[0:4] + BE64(BE64(base[4:12]) + i)，AAD = BE64(i)
 *   4MB 分块——收发内存峰值恒定，特大文件不再整块进内存。
 * 密钥派生:兼容 WHE1/WHE2 使用 100000 次，新传输 WHE3 使用 600000 次。
 */
object Crypto {
    private val MAGIC = "WHE1".toByteArray(Charsets.US_ASCII)
    private val MAGIC2 = "WHE2".toByteArray(Charsets.US_ASCII)
    private val MAGIC3 = "WHE3".toByteArray(Charsets.US_ASCII)
    private const val SALT_LEN = 16
    private const val NONCE_LEN = 12
    private const val LEGACY_ITERATIONS = 100_000
    private const val ITERATIONS = 600_000
    private const val KEY_LEN = 256     // bits
    private const val TAG_LEN = 128     // bits (GCM auth tag)
    const val CHUNK_SIZE = 4 * 1024 * 1024
    private const val CHUNK_OVERHEAD = 20L   // 每帧: 4B 长度 + 16B tag

    fun isEncrypted(blob: ByteArray): Boolean {
        return blob.size >= 4 + SALT_LEN + NONCE_LEN && blob.copyOfRange(0, 4).contentEquals(MAGIC)
    }

    fun encrypt(secret: String, plain: ByteArray): ByteArray {
        val sr = SecureRandom()
        val salt = ByteArray(SALT_LEN).also { sr.nextBytes(it) }
        val nonce = ByteArray(NONCE_LEN).also { sr.nextBytes(it) }
        val key = deriveKey(secret, salt, LEGACY_ITERATIONS)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(TAG_LEN, nonce))
        val ct = cipher.doFinal(plain)
        return MAGIC + salt + nonce + ct
    }

    fun decrypt(secret: String, blob: ByteArray): ByteArray? {
        if (!isEncrypted(blob)) return null
        val salt = blob.copyOfRange(4, 4 + SALT_LEN)
        val nonce = blob.copyOfRange(4 + SALT_LEN, 4 + SALT_LEN + NONCE_LEN)
        val ct = blob.copyOfRange(4 + SALT_LEN + NONCE_LEN, blob.size)
        val key = deriveKey(secret, salt, LEGACY_ITERATIONS)
        return try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(TAG_LEN, nonce))
            cipher.doFinal(ct)
        } catch (e: Exception) {
            null
        }
    }

    private fun deriveKey(secret: String, salt: ByteArray,
                          iterations: Int = ITERATIONS): ByteArray {
        val spec = PBEKeySpec(secret.toCharArray(), salt, iterations, KEY_LEN)
        val factory = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
        return factory.generateSecret(spec).encoded
    }

    // ---------- 分块格式 WHE2/WHE3 ----------

    /** 给定明文大小，返回分块加密后的线上字节总数(发送方写进 header)。 */
    fun chunkedWireSize(plainSize: Long): Long {
        val n = (plainSize + CHUNK_SIZE - 1) / CHUNK_SIZE
        return 32 + plainSize + n * CHUNK_OVERHEAD
    }

    /** 第 idx 块的 nonce：前 4 字节固定，后 8 字节计数器 + idx(mod 2^64，与 Python 一致)。 */
    private fun chunkNonce(base: ByteArray, idx: Long): ByteArray {
        val ctr = ByteBuffer.wrap(base, 4, 8).long + idx   // Long 溢出环绕 == mod 2^64
        return ByteBuffer.allocate(12).put(base, 0, 4).putLong(ctr).array()
    }

    private fun aadOf(idx: Long): ByteArray = ByteBuffer.allocate(8).putLong(idx).array()

    /** 流式分块加密：先取 streamHeader 发出，再逐块 encryptChunk。 */
    class ChunkedEncryptor(secret: String) {
        private val salt = ByteArray(SALT_LEN).also { SecureRandom().nextBytes(it) }
        private val base = ByteArray(NONCE_LEN).also { SecureRandom().nextBytes(it) }
        private val key = deriveKey(secret, salt)
        private var idx = 0L
        val streamHeader: ByteArray = MAGIC3 + salt + base

        fun encryptChunk(plain: ByteArray, len: Int = plain.size): ByteArray {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"),
                GCMParameterSpec(TAG_LEN, chunkNonce(base, idx)))
            cipher.updateAAD(aadOf(idx))
            idx++
            return cipher.doFinal(plain, 0, len)
        }
    }

    /** 按序解密 WHE2/WHE3 分块流；口令不对/被篡改/被重排返回 null。 */
    class ChunkedDecryptor(secret: String, streamHeader: ByteArray) {
        private val base: ByteArray
        private val key: ByteArray
        private var idx = 0L

        init {
            require(streamHeader.size == 32) { "bad chunked encryption header" }
            val magic = streamHeader.copyOfRange(0, 4)
            require(magic.contentEquals(MAGIC2) || magic.contentEquals(MAGIC3)) {
                "bad chunked encryption header"
            }
            base = streamHeader.copyOfRange(20, 32)
            val iterations = if (magic.contentEquals(MAGIC2)) LEGACY_ITERATIONS else ITERATIONS
            key = deriveKey(secret, streamHeader.copyOfRange(4, 20), iterations)
        }

        fun decryptChunk(ct: ByteArray): ByteArray? = try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"),
                GCMParameterSpec(TAG_LEN, chunkNonce(base, idx)))
            cipher.updateAAD(aadOf(idx))
            val plain = cipher.doFinal(ct)
            idx++
            plain
        } catch (e: Exception) {
            null
        }
    }
}
