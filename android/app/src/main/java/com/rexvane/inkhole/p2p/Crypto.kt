package com.rexvane.inkhole.p2p

import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.PBEKeySpec
import javax.crypto.spec.SecretKeySpec
import java.nio.ByteBuffer
import java.security.SecureRandom
import java.util.concurrent.ConcurrentHashMap

/**
 * AES-256-GCM 端到端加密, 与桌面版 Python crypto.py、Go whe.go 逐字节兼容。
 *
 * 整块格式 WHE1(小文件): [4B "WHE1"] [16B salt] [12B nonce] [ct + 16B tag]
 * 分块格式 WHE2/WHE3/WHE4(大文件): [4B magic] [16B salt] [12B base_nonce] + 帧*
 *   帧 = [4B 密文长度(BE)] + 密文
 *   第 i 块: nonce_i = base[0:4] + BE64(BE64(base[4:12]) + i)，AAD = BE64(i)
 *   4MB 分块——收发内存峰值恒定，特大文件不再整块进内存。
 * 密钥派生:兼容 WHE1/WHE2 使用 100000 次，WHE3 每流 600000 次。
 * WHE4(经 WHPC "whe4" 能力协商):口令对固定应用盐做一次 600000 次 PBKDF2
 * 得主密钥(进程内缓存)，每流用 HKDF-SHA256(master, salt=流盐, info) 派生
 * 流密钥——每次传输省下秒级派生开销，流间隔离不变。
 */
object Crypto {
    private val MAGIC = "WHE1".toByteArray(Charsets.US_ASCII)
    private val MAGIC2 = "WHE2".toByteArray(Charsets.US_ASCII)
    private val MAGIC3 = "WHE3".toByteArray(Charsets.US_ASCII)
    private val MAGIC4 = "WHE4".toByteArray(Charsets.US_ASCII)
    private val WHE4_MASTER_SALT = "INKHOLE-WHE4-MASTER-V1".toByteArray(Charsets.US_ASCII)
    private val WHE4_STREAM_INFO = "INKHOLE-WHE4-STREAM-V1".toByteArray(Charsets.US_ASCII)
    private const val SALT_LEN = 16
    private const val NONCE_LEN = 12
    private const val LEGACY_ITERATIONS = 100_000
    private const val ITERATIONS = 600_000
    private const val KEY_LEN = 256     // bits
    private const val TAG_LEN = 128     // bits (GCM auth tag)
    const val CHUNK_SIZE = 4 * 1024 * 1024
    private const val CHUNK_OVERHEAD = 20L   // 每帧: 4B 长度 + 16B tag

    private val masterCache = ConcurrentHashMap<String, ByteArray>()
    private const val MASTER_CACHE_MAX = 8  // 进程一般只有一两个口令；上限防病态增长

    // 进程随机 HMAC 键：缓存键是口令摘要而非明文口令，堆转储/诊断里
    // 不会长期留存换掉的旧口令本身(与 Go masterCache 相同的设计)。
    private val masterCacheKey = ByteArray(32).also { SecureRandom().nextBytes(it) }

    private fun masterCacheId(secret: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(masterCacheKey, "HmacSHA256"))
        return mac.doFinal(secret.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
    }

    /** WHE4 主密钥：每口令一次 60 万次 PBKDF2，进程内缓存。 */
    private fun masterKey(secret: String): ByteArray {
        val cacheId = masterCacheId(secret)
        masterCache[cacheId]?.let { return it }
        val derived = deriveKey(secret, WHE4_MASTER_SALT)
        if (masterCache.size >= MASTER_CACHE_MAX) masterCache.clear()
        masterCache[cacheId] = derived
        return derived
    }

    /** RFC 5869 HKDF-SHA256；32 字节输出恰好一轮 expand。 */
    private fun streamKeyWhe4(secret: String, salt: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(salt, "HmacSHA256"))
        val prk = mac.doFinal(masterKey(secret))
        mac.init(SecretKeySpec(prk, "HmacSHA256"))
        mac.update(WHE4_STREAM_INFO)
        mac.update(1.toByte())
        return mac.doFinal()
    }

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

    /** 流式分块加密：先取 streamHeader 发出，再逐块 encryptChunk。
     *  useWhe4 仅当对端在 WHPC 中声明 "whe4" 能力时为 true；否则发 WHE3。 */
    class ChunkedEncryptor(secret: String, useWhe4: Boolean = false) {
        private val salt = ByteArray(SALT_LEN).also { SecureRandom().nextBytes(it) }
        private val base = ByteArray(NONCE_LEN).also { SecureRandom().nextBytes(it) }
        private val key = if (useWhe4) streamKeyWhe4(secret, salt) else deriveKey(secret, salt)
        private var idx = 0L
        val streamHeader: ByteArray = (if (useWhe4) MAGIC4 else MAGIC3) + salt + base

        fun encryptChunk(plain: ByteArray, len: Int = plain.size): ByteArray {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"),
                GCMParameterSpec(TAG_LEN, chunkNonce(base, idx)))
            cipher.updateAAD(aadOf(idx))
            idx++
            return cipher.doFinal(plain, 0, len)
        }
    }

    /** 按序解密 WHE2/WHE3/WHE4 分块流；口令不对/被篡改/被重排返回 null。 */
    class ChunkedDecryptor(secret: String, streamHeader: ByteArray) {
        private val base: ByteArray
        private val key: ByteArray
        private var idx = 0L

        init {
            require(streamHeader.size == 32) { "bad chunked encryption header" }
            val magic = streamHeader.copyOfRange(0, 4)
            val salt = streamHeader.copyOfRange(4, 20)
            base = streamHeader.copyOfRange(20, 32)
            key = when {
                magic.contentEquals(MAGIC4) -> streamKeyWhe4(secret, salt)
                magic.contentEquals(MAGIC3) -> deriveKey(secret, salt, ITERATIONS)
                magic.contentEquals(MAGIC2) -> deriveKey(secret, salt, LEGACY_ITERATIONS)
                else -> throw IllegalArgumentException("bad chunked encryption header")
            }
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
