package com.rexvane.inkhole.relay

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.PublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

const val RELAY_PROTOCOL_VERSION = 1
const val RELAY_FRAME_PLAIN_LIMIT = 64 * 1024

private val B64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

internal fun base64UrlEncode(data: ByteArray): String {
    val out = StringBuilder((data.size * 4 + 2) / 3)
    var i = 0
    while (i < data.size) {
        val a = data[i++].toInt() and 0xff
        val b = if (i < data.size) data[i++].toInt() and 0xff else -1
        val c = if (i < data.size) data[i++].toInt() and 0xff else -1
        out.append(B64_CHARS[a ushr 2])
        out.append(B64_CHARS[((a and 3) shl 4) or (if (b >= 0) b ushr 4 else 0)])
        if (b >= 0) out.append(B64_CHARS[((b and 15) shl 2) or (if (c >= 0) c ushr 6 else 0)])
        if (c >= 0) out.append(B64_CHARS[c and 63])
    }
    return out.toString()
}

internal fun base64UrlDecode(value: String): ByteArray {
    require(value.isNotEmpty()) { "empty base64 value" }
    val clean = value.trim().trimEnd('=')
    require(clean.length % 4 != 1) { "invalid base64 value" }
    val out = ByteArrayOutputStream(clean.length * 3 / 4)
    var buffer = 0
    var bits = 0
    clean.forEach { char ->
        val index = B64_CHARS.indexOf(char)
        require(index >= 0) { "invalid base64 value" }
        buffer = (buffer shl 6) or index
        bits += 6
        if (bits >= 8) {
            bits -= 8
            out.write((buffer ushr bits) and 0xff)
        }
    }
    return out.toByteArray()
}

private fun packed(value: String): ByteArray {
    val raw = value.toByteArray(Charsets.UTF_8)
    require(raw.size <= 65535) { "identity field too long" }
    return ByteBuffer.allocate(2 + raw.size).putShort(raw.size.toShort()).put(raw).array()
}

data class DeviceIdentity(val privateKey: PrivateKey) {
    companion object {
        fun generate(): DeviceIdentity {
            val generator = KeyPairGenerator.getInstance("EC")
            generator.initialize(ECGenParameterSpec("secp256r1"))
            return DeviceIdentity(generator.generateKeyPair().private)
        }

        fun fromPrivateB64(value: String): DeviceIdentity {
            val key = KeyFactory.getInstance("EC")
                .generatePrivate(PKCS8EncodedKeySpec(base64UrlDecode(value)))
            return DeviceIdentity(key)
        }
    }

    fun privateB64(): String = base64UrlEncode(privateKey.encoded)
}

data class DeviceKeyPair(val privateB64: String, val publicB64: String) {
    companion object {
        fun generate(): DeviceKeyPair {
            val generator = KeyPairGenerator.getInstance("EC")
            generator.initialize(ECGenParameterSpec("secp256r1"))
            val pair = generator.generateKeyPair()
            return DeviceKeyPair(
                base64UrlEncode(pair.private.encoded),
                base64UrlEncode(pair.public.encoded),
            )
        }
    }
}

private fun publicKey(value: String): PublicKey = KeyFactory.getInstance("EC")
    .generatePublic(X509EncodedKeySpec(base64UrlDecode(value)))

private fun hkdf(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(salt, "HmacSHA256"))
    val prk = mac.doFinal(ikm)
    val result = ByteArrayOutputStream(length)
    var previous = ByteArray(0)
    var counter = 1
    while (result.size() < length) {
        mac.init(SecretKeySpec(prk, "HmacSHA256"))
        mac.update(previous)
        mac.update(info)
        mac.update(counter.toByte())
        previous = mac.doFinal()
        result.write(previous, 0, minOf(previous.size, length - result.size()))
        counter++
    }
    return result.toByteArray()
}

fun deriveTransferKey(
    identity: DeviceIdentity,
    peerPublicB64: String,
    transferId: String,
    senderId: String,
    receiverId: String,
): ByteArray {
    val agreement = KeyAgreement.getInstance("ECDH")
    agreement.init(identity.privateKey)
    agreement.doPhase(publicKey(peerPublicB64), true)
    val shared = agreement.generateSecret()
    val transcript = ByteArrayOutputStream().apply {
        write("inkhole-relay-v1\u0000".toByteArray(Charsets.US_ASCII))
        write(packed(transferId)); write(packed(senderId)); write(packed(receiverId))
    }.toByteArray()
    val salt = MessageDigest.getInstance("SHA-256").digest(transcript)
    val info = "inkhole relay transfer key\u0000".toByteArray(Charsets.US_ASCII) + transcript
    return hkdf(shared, salt, info, 32)
}

class RelayCipher(
    key: ByteArray,
    transferId: String,
    senderId: String,
    receiverId: String,
) {
    private val secret = SecretKeySpec(key, "AES")
    private val aadPrefix = ByteArrayOutputStream().apply {
        write("IRF1".toByteArray(Charsets.US_ASCII))
        write(packed(transferId)); write(packed(senderId)); write(packed(receiverId))
    }.toByteArray()
    private val sendSeq = longArrayOf(0, 0)
    private val receiveSeq = longArrayOf(0, 0)

    @Synchronized
    fun seal(direction: Int, plain: ByteArray): ByteArray {
        require(direction in 0..1) { "invalid frame direction" }
        require(plain.size <= RELAY_FRAME_PLAIN_LIMIT) { "relay frame exceeds 64 KiB" }
        val sequence = sendSeq[direction]
        require(sequence >= 0 && sequence != Long.MAX_VALUE) { "relay sequence exhausted" }
        val header = ByteBuffer.allocate(10)
            .put(RELAY_PROTOCOL_VERSION.toByte()).put(direction.toByte()).putLong(sequence).array()
        val nonce = ByteBuffer.allocate(12)
            .put("IRF".toByteArray(Charsets.US_ASCII)).put(direction.toByte()).putLong(sequence).array()
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, secret, GCMParameterSpec(128, nonce))
        cipher.updateAAD(aadPrefix + header)
        sendSeq[direction]++
        return header + cipher.doFinal(plain)
    }

    @Synchronized
    fun open(frame: ByteArray, expectedDirection: Int): ByteArray {
        require(frame.size >= 26) { "truncated relay frame" }
        val input = ByteBuffer.wrap(frame)
        val version = input.get().toInt() and 0xff
        val direction = input.get().toInt() and 0xff
        val sequence = input.long
        require(version == RELAY_PROTOCOL_VERSION) { "unsupported relay frame version" }
        require(direction == expectedDirection) { "wrong relay frame direction" }
        require(sequence == receiveSeq[direction]) { "replayed or out-of-order relay frame" }
        val header = frame.copyOfRange(0, 10)
        val nonce = ByteBuffer.allocate(12)
            .put("IRF".toByteArray(Charsets.US_ASCII)).put(direction.toByte()).putLong(sequence).array()
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, secret, GCMParameterSpec(128, nonce))
        cipher.updateAAD(aadPrefix + header)
        val plain = cipher.doFinal(frame.copyOfRange(10, frame.size))
        receiveSeq[direction]++
        return plain
    }
}
