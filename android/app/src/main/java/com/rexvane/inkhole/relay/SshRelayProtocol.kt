package com.rexvane.inkhole.relay

import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.util.Locale
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

internal const val SSH_RELAY_VERSION = 1
internal const val SSH_REGISTRY_LIMIT = 8 * 1024
internal const val SSH_FRAME_WIRE_LIMIT = RELAY_FRAME_PLAIN_LIMIT + 26
internal val SSH_HANDSHAKE_MAGIC = "ISSH".toByteArray(Charsets.US_ASCII)

internal data class RegistryRecord(
    val deviceId: String,
    val name: String,
    val port: Int,
    val publicKey: String,
)

internal data class TransferOffer(
    val transferId: String,
    val senderId: String,
    val receiverId: String,
    val publicKey: String,
)

private fun packedSsh(value: String): ByteArray {
    val raw = value.toByteArray(Charsets.UTF_8)
    require(raw.size <= 65535) { "字段过长" }
    return ByteBuffer.allocate(2 + raw.size).putShort(raw.size.toShort()).put(raw).array()
}

private fun hmacSha256(key: ByteArray, value: ByteArray): ByteArray =
    Mac.getInstance("HmacSHA256").run {
        init(SecretKeySpec(key, "HmacSHA256"))
        doFinal(value)
    }

internal fun registryGroupKey(privateKey: CharArray): ByteArray {
    val normalized = String(privateKey)
        .removePrefix("\uFEFF")
        .replace("\r\n", "\n")
        .replace('\r', '\n')
        .trim() + "\n"
    val seed = java.security.MessageDigest.getInstance("SHA-256")
        .digest(normalized.toByteArray(Charsets.UTF_8))
    return hmacSha256(seed, "inkhole ssh relay registry v1".toByteArray(Charsets.US_ASCII))
}

private fun registryTranscript(
    deviceId: String,
    name: String,
    port: Int,
    publicKey: String,
): ByteArray = ByteArrayOutputStream().apply {
    write("inkhole-ssh-registry-v1\u0000".toByteArray(Charsets.US_ASCII))
    write(packedSsh(deviceId))
    write(packedSsh(name))
    write(ByteBuffer.allocate(2).putShort(port.toShort()).array())
    write(packedSsh(publicKey))
}.toByteArray()

internal fun encodeRegistryRecord(record: RegistryRecord, key: ByteArray): ByteArray {
    require(record.deviceId.matches(Regex("[0-9a-f]{32}"))) { "设备 ID 无效" }
    require(record.name.length in 1..80 && record.port in 1..65535) { "设备登记内容无效" }
    val signature = base64UrlEncode(hmacSha256(key, registryTranscript(
        record.deviceId, record.name, record.port, record.publicKey)))
    val raw = JSONObject()
        .put("id", record.deviceId)
        .put("mac", signature)
        .put("name", record.name)
        .put("port", record.port)
        .put("public_key", record.publicKey)
        .put("v", SSH_RELAY_VERSION)
        .toString()
        .toByteArray(Charsets.UTF_8)
    require(raw.size <= SSH_REGISTRY_LIMIT) { "设备登记内容过大" }
    return raw
}

internal fun decodeRegistryRecord(raw: ByteArray, key: ByteArray): RegistryRecord {
    require(raw.isNotEmpty() && raw.size <= SSH_REGISTRY_LIMIT) { "设备登记内容大小无效" }
    val value = JSONObject(String(raw, Charsets.UTF_8))
    require(value.optInt("v") == SSH_RELAY_VERSION) { "设备登记版本不支持" }
    val record = RegistryRecord(
        value.optString("id"),
        value.optString("name"),
        value.optInt("port"),
        value.optString("public_key"),
    )
    require(record.deviceId.matches(Regex("[0-9a-f]{32}")) &&
        record.name.length in 1..80 && record.port in 1..65535 &&
        record.publicKey.length in 40..1024) { "设备登记字段无效" }
    val expected = hmacSha256(key, registryTranscript(
        record.deviceId, record.name, record.port, record.publicKey))
    val actual = base64UrlDecode(value.optString("mac"))
    require(java.security.MessageDigest.isEqual(expected, actual)) { "设备登记签名不匹配" }
    return record
}

private fun offerTranscript(offer: TransferOffer): ByteArray =
    ByteArrayOutputStream().apply {
        write("inkhole-ssh-offer-v1\u0000".toByteArray(Charsets.US_ASCII))
        write(packedSsh(offer.transferId))
        write(packedSsh(offer.senderId))
        write(packedSsh(offer.receiverId))
        write(packedSsh(offer.publicKey))
    }.toByteArray()

internal fun encodeOffer(offer: TransferOffer, key: ByteArray): ByteArray =
    JSONObject()
        .put("mac", base64UrlEncode(hmacSha256(key, offerTranscript(offer))))
        .put("public_key", offer.publicKey)
        .put("receiver_id", offer.receiverId)
        .put("sender_id", offer.senderId)
        .put("transfer_id", offer.transferId)
        .put("v", SSH_RELAY_VERSION)
        .toString()
        .toByteArray(Charsets.UTF_8)

internal fun decodeOffer(raw: ByteArray, key: ByteArray, receiverId: String): TransferOffer {
    require(raw.isNotEmpty() && raw.size <= SSH_REGISTRY_LIMIT) { "SSH 传输握手大小无效" }
    val value = JSONObject(String(raw, Charsets.UTF_8))
    val offer = TransferOffer(
        value.optString("transfer_id"),
        value.optString("sender_id"),
        value.optString("receiver_id"),
        value.optString("public_key"),
    )
    require(value.optInt("v") == SSH_RELAY_VERSION && offer.receiverId == receiverId &&
        offer.transferId.matches(Regex("[0-9a-f-]{32,36}")) &&
        offer.senderId.matches(Regex("[0-9a-f]{32}")) &&
        offer.publicKey.length in 40..1024) { "SSH 传输握手字段无效" }
    val expected = hmacSha256(key, offerTranscript(offer))
    val actual = base64UrlDecode(value.optString("mac"))
    require(java.security.MessageDigest.isEqual(expected, actual)) { "SSH 传输握手签名不匹配" }
    return offer
}

internal fun InputStream.readExact(size: Int): ByteArray {
    require(size >= 0)
    val value = ByteArray(size)
    var offset = 0
    while (offset < size) {
        val count = read(value, offset, size - offset)
        if (count < 0) throw java.io.EOFException("SSH 数据通道已中断")
        offset += count
    }
    return value
}

internal class SshFrameStream(
    private val input: InputStream,
    private val output: OutputStream,
    private val cipher: RelayCipher,
) {
    fun send(direction: Int, plain: ByteArray) {
        val frame = cipher.seal(direction, plain)
        output.write(ByteBuffer.allocate(4).putInt(frame.size).array())
        output.write(frame)
        output.flush()
    }

    fun receive(direction: Int): ByteArray {
        val size = ByteBuffer.wrap(input.readExact(4)).int
        require(size in 26..SSH_FRAME_WIRE_LIMIT) { "SSH 加密帧长度无效" }
        return cipher.open(input.readExact(size), direction)
    }
}

internal class SshFrameReader(
    private val stream: SshFrameStream,
    private val direction: Int,
) {
    private var pending = ByteArray(0)

    fun readExact(size: Int): ByteArray {
        while (pending.size < size) pending += stream.receive(direction)
        val result = pending.copyOfRange(0, size)
        pending = pending.copyOfRange(size, pending.size)
        return result
    }
}
