package com.rexvane.inkhole.p2p

import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.InputStream
import java.io.IOException
import java.io.OutputStream
import org.json.JSONArray
import org.json.JSONObject

/** WHPP (InkHole P2P Protocol) 常量与读写工具。 */
object WHPP {
    val MAGIC = "WHPP".toByteArray(Charsets.US_ASCII)
    val CAP_MAGIC = "WHPC".toByteArray(Charsets.US_ASCII)
    const val CAP_VERSION = 3
    const val PROTOCOL_VERSION = 3
    const val FOLDER_KIND = "folder-v1"
    const val RELIABLE_KIND = "reliable-v3"
    const val BUFFER_SIZE = 256 * 1024
    const val MAX_HEADER = 64 * 1024              // header 长度上限(来自网络，不可信)
    const val MAX_FILE_SIZE = 1L shl 40           // 单文件 1TB 上限，防恶意 size 声明
    const val ACK_OK: Int = 0x01                  // 接收方回执：成功落盘
    const val ACK_FAIL: Int = 0x00                // 接收方回执：失败
    const val RESUME: Int = 0x02                  // 后跟 8B 已持久化明文偏移
    const val DIGEST_SIZE = 32

    data class Header(
        val version: Int = PROTOCOL_VERSION,
        val filename: String,
        val plainSize: Long,
        val transferId: String,
        val sha256: String,
        val encrypted: Boolean,
        val wantAck: Boolean = true,
        val encMode: String = "",   // WHPP v3 加密传输必须为 "chunked"（WHE2）
        val kind: String = "file",
        val modifiedMs: Long = 0,
        val senderInstanceId: String = "",
        val senderPublicKey: String = "",
    )

    data class Capabilities(
        val instanceId: String,
        val peerName: String,
        val capabilities: Set<String>,
        val publicKey: String,
        val fingerprint: String,
    )

    /** 把 header JSON 序列化(与桌面版 Python 完全一致)。 */
    fun encodeHeader(h: Header): ByteArray {
        val json = JSONObject()
        json.put("version", h.version)
        json.put("filename", h.filename)
        json.put("plain_size", h.plainSize)
        json.put("transfer_id", h.transferId)
        json.put("sha256", h.sha256)
        json.put("encrypted", h.encrypted)
        json.put("want_ack", h.wantAck)
        if (h.encMode.isNotEmpty()) json.put("enc_mode", h.encMode)
        json.put("kind", h.kind)
        if (h.modifiedMs > 0) json.put("mtime_ms", h.modifiedMs)
        json.put("sender_instance_id", h.senderInstanceId)
        json.put("sender_public_key", h.senderPublicKey)
        return json.toString().toByteArray(Charsets.UTF_8)
    }

    /** 从 JSON bytes 解析 header。 */
    fun decodeHeader(bytes: ByteArray): Header {
        val json = JSONObject(String(bytes, Charsets.UTF_8))
        return Header(
            version = json.getInt("version"),
            filename = json.getString("filename"),
            plainSize = json.getLong("plain_size"),
            transferId = json.getString("transfer_id"),
            sha256 = json.getString("sha256"),
            encrypted = json.optBoolean("encrypted", false),
            wantAck = json.optBoolean("want_ack", false),
            encMode = json.optString("enc_mode", ""),
            kind = json.optString("kind", "file"),
            modifiedMs = json.optLong("mtime_ms", 0),
            senderInstanceId = json.optString("sender_instance_id", "").lowercase(),
            senderPublicKey = json.optString("sender_public_key", ""),
        )
    }

    /** 只写 WHPP 帧头(magic + header)，数据体由调用方自己写(分块加密用)。 */
    fun writeHeader(out: OutputStream, h: Header) {
        val header = encodeHeader(h)
        val dout = DataOutputStream(out)
        dout.write(MAGIC)
        dout.writeInt(header.size)          // big-endian, 与 Python struct.pack("!I") 一致
        dout.write(header)
        dout.flush()
    }

    /** 从输入流读 WHPP 帧, 返回 header + 数据的输入流引用(调用方负责读 size 字节)。 */
    fun readHeader(input: InputStream): Header {
        val din = DataInputStream(input)
        val magic = ByteArray(4)
        din.readFully(magic)
        if (!magic.contentEquals(MAGIC)) throw IllegalArgumentException("bad magic")
        return readHeaderAfterMagic(input)
    }

    /** Magic 已由连接分派器读取后，继续解析 WHPP JSON header。 */
    fun readHeaderAfterMagic(input: InputStream): Header {
        val din = DataInputStream(input)
        val headerLen = din.readInt()       // big-endian
        if (headerLen <= 0 || headerLen > MAX_HEADER) throw IllegalArgumentException("bad header len")
        val headerBytes = ByteArray(headerLen)
        din.readFully(headerBytes)
        return decodeHeader(headerBytes)
    }

    fun readMagic(input: InputStream): ByteArray = ByteArray(4).also {
        DataInputStream(input).readFully(it)
    }

    /** WHPC v3 response: signed random challenge + identity and capabilities. */
    fun writeCapabilities(out: OutputStream, instanceId: String, peerName: String,
                          nonce: ByteArray, identity: DeviceIdentity) {
        val capabilities = listOf(FOLDER_KIND, RELIABLE_KIND)
        val body = JSONObject().apply {
            put("version", CAP_VERSION)
            put("caps", JSONArray(capabilities))
            put("instance_id", instanceId)
            put("peer_name", peerName)
            put("public_key", identity.publicKey)
            put("signature", identity.sign(
                DeviceAuth.capabilityMessage(
                    nonce, instanceId, peerName, CAP_VERSION, capabilities)))
        }.toString().toByteArray(Charsets.UTF_8)
        DataOutputStream(out).apply {
            write(CAP_MAGIC)
            writeInt(body.size)
            write(body)
            flush()
        }
    }

    fun readCapabilities(input: InputStream, nonce: ByteArray): Capabilities {
        val din = DataInputStream(input)
        val magic = ByteArray(4).also { din.readFully(it) }
        if (!magic.contentEquals(CAP_MAGIC)) throw IOException("bad WHPC magic")
        val bodySize = din.readInt()
        if (bodySize <= 0 || bodySize > MAX_HEADER) throw IOException("bad WHPC length")
        val body = ByteArray(bodySize).also { din.readFully(it) }
        val json = try {
            JSONObject(String(body, Charsets.UTF_8))
        } catch (e: Exception) {
            throw IOException("bad WHPC body", e)
        }
        val instanceId = json.optString("instance_id", "").lowercase()
        val peerName = json.optString("peer_name", "").trim()
        val publicKey = json.optString("public_key", "")
        val signature = json.optString("signature", "")
        val capsJson = json.optJSONArray("caps") ?: throw IOException("missing WHPC caps")
        if (json.optInt("version", 0) != CAP_VERSION ||
            !instanceId.matches(Regex("[0-9a-f]{32}")) || peerName.isEmpty()) {
            throw IOException("unsupported WHPC version")
        }
        val fingerprint = try {
            DeviceAuth.fingerprint(publicKey)
        } catch (error: Exception) {
            throw IOException("bad WHPC public key", error)
        }
        val caps = LinkedHashSet<String>()
        for (index in 0 until capsJson.length()) {
            val value = capsJson.opt(index)
            if (value !is String) throw IOException("bad WHPC capability")
            caps.add(value)
        }
        if (!DeviceAuth.verify(publicKey,
                DeviceAuth.capabilityMessage(
                    nonce, instanceId, peerName, CAP_VERSION, caps), signature)) {
            throw IOException("bad WHPC identity signature")
        }
        return Capabilities(instanceId, peerName, caps, publicKey, fingerprint)
    }
}
