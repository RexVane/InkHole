package com.rexvane.inkhole.p2p

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.math.BigInteger
import java.security.AlgorithmParameters
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.MessageDigest
import java.security.Signature
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECParameterSpec
import java.security.spec.ECPoint
import java.security.spec.ECPublicKeySpec

/** Hardware-backed when available; private key material never leaves Android Keystore. */
class DeviceIdentity(@Suppress("UNUSED_PARAMETER") context: Context) {
    companion object {
        private const val ALIAS = "inkhole-lan-identity-v1"
    }

    private val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    init {
        if (!store.containsAlias(ALIAS)) {
            KeyPairGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore").apply {
                initialize(KeyGenParameterSpec.Builder(
                    ALIAS,
                    KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY,
                ).setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .build())
                generateKeyPair()
            }
        }
    }

    private val entry: KeyStore.PrivateKeyEntry
        get() = store.getEntry(ALIAS, null) as KeyStore.PrivateKeyEntry

    val publicBytes: ByteArray
        get() = DeviceAuth.encodePublicKey(entry.certificate.publicKey as ECPublicKey)

    val publicKey: String
        get() = Base64.encodeToString(publicBytes, Base64.NO_WRAP)

    val fingerprint: String
        get() = DeviceAuth.hex(MessageDigest.getInstance("SHA-256").digest(publicBytes))

    fun sign(message: ByteArray): String {
        val signature = Signature.getInstance("SHA256withECDSA").apply {
            initSign(entry.privateKey)
            update(message)
        }.sign()
        return Base64.encodeToString(signature, Base64.NO_WRAP)
    }
}

object DeviceAuth {
    private val CAP_DOMAIN = "INKHOLE-WHPC3\u0000".toByteArray(Charsets.US_ASCII)
    private val TRANSFER_DOMAIN = "INKHOLE-WHPP3-AUTH\u0000".toByteArray(Charsets.US_ASCII)
    private val RECEIVER_DOMAIN =
        "INKHOLE-WHPP3-RECEIVER\u0000".toByteArray(Charsets.US_ASCII)

    fun capabilityMessage(nonce: ByteArray, instanceId: String, peerName: String,
                          version: Int, capabilities: Collection<String>): ByteArray {
        require(nonce.size == 32)
        require(version in 0..0xffff)
        val name = peerName.toByteArray(Charsets.UTF_8)
        require(name.size <= 0xffff)
        val caps = capabilities.toSortedSet().map { value ->
            value.toByteArray(Charsets.UTF_8).also { require(it.size <= 0xffff) }
        }
        require(caps.size <= 0xffff)
        return ByteArrayOutputStream().also { bytes ->
            DataOutputStream(bytes).use { out ->
                out.write(CAP_DOMAIN)
                out.write(nonce)
                out.writeShort(version)
                out.write(instanceId.lowercase().toByteArray(Charsets.US_ASCII))
                out.writeShort(name.size)
                out.write(name)
                out.writeShort(caps.size)
                caps.forEach { capability ->
                    out.writeShort(capability.size)
                    out.write(capability)
                }
            }
        }.toByteArray()
    }

    fun transferMessage(nonce: ByteArray, header: WHPP.Header, offset: Long): ByteArray {
        require(nonce.size == 32)
        return ByteArrayOutputStream().also { bytes ->
            DataOutputStream(bytes).use { out ->
                out.write(TRANSFER_DOMAIN)
                out.write(nonce)
                writeTransferFields(out, header, offset)
            }
        }.toByteArray()
    }

    fun receiverMessage(nonce: ByteArray, header: WHPP.Header, offset: Long,
                        receiverInstanceId: String): ByteArray {
        require(nonce.size == 32)
        val receiver = receiverInstanceId.lowercase().toByteArray(Charsets.US_ASCII)
        require(receiver.size == 32)
        return ByteArrayOutputStream().also { bytes ->
            DataOutputStream(bytes).use { out ->
                out.write(RECEIVER_DOMAIN)
                out.write(nonce)
                out.write(receiver)
                writeTransferFields(out, header, offset)
            }
        }.toByteArray()
    }

    private fun writeTransferFields(out: DataOutputStream, header: WHPP.Header, offset: Long) {
        val kind = header.kind.toByteArray(Charsets.UTF_8)
        val filename = header.filename.toByteArray(Charsets.UTF_8)
        require(kind.size <= 0xffff)
        out.write(header.senderInstanceId.lowercase().toByteArray(Charsets.US_ASCII))
        out.write(header.transferId.lowercase().toByteArray(Charsets.US_ASCII))
        out.write(header.sha256.lowercase().toByteArray(Charsets.US_ASCII))
        out.writeShort(kind.size)
        out.write(kind)
        out.writeInt(filename.size)
        out.write(filename)
        out.writeLong(header.plainSize)
        out.writeLong(header.modifiedMs)
        out.writeByte(if (header.encrypted) 1 else 0)
        out.writeLong(offset)
    }

    fun fingerprint(encoded: String): String {
        val raw = Base64.decode(encoded, Base64.DEFAULT)
        decodePublicKey(raw)
        return hex(MessageDigest.getInstance("SHA-256").digest(raw))
    }

    fun verify(encodedPublicKey: String, message: ByteArray, encodedSignature: String): Boolean =
        try {
            val publicKey = decodePublicKey(Base64.decode(encodedPublicKey, Base64.DEFAULT))
            Signature.getInstance("SHA256withECDSA").run {
                initVerify(publicKey)
                update(message)
                verify(Base64.decode(encodedSignature, Base64.DEFAULT))
            }
        } catch (_: Exception) {
            false
        }

    internal fun encodePublicKey(key: ECPublicKey): ByteArray = byteArrayOf(4) +
        fixedCoordinate(key.w.affineX) + fixedCoordinate(key.w.affineY)

    private fun fixedCoordinate(value: BigInteger): ByteArray {
        val raw = value.toByteArray()
        if (raw.size == 32) return raw
        if (raw.size == 33 && raw[0] == 0.toByte()) return raw.copyOfRange(1, 33)
        require(raw.size < 32)
        return ByteArray(32 - raw.size) + raw
    }

    private fun decodePublicKey(raw: ByteArray): ECPublicKey {
        require(raw.size == 65 && raw[0] == 4.toByte())
        val parameters = AlgorithmParameters.getInstance("EC").apply {
            init(ECGenParameterSpec("secp256r1"))
        }.getParameterSpec(ECParameterSpec::class.java)
        val point = ECPoint(
            BigInteger(1, raw.copyOfRange(1, 33)),
            BigInteger(1, raw.copyOfRange(33, 65)),
        )
        return KeyFactory.getInstance("EC").generatePublic(
            ECPublicKeySpec(point, parameters)) as ECPublicKey
    }

    internal fun hex(bytes: ByteArray): String =
        bytes.joinToString("") { "%02x".format(it) }
}
