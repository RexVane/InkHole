package com.rexvane.inkhole.p2p

import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.EOFException
import java.io.InterruptedIOException
import java.io.InputStream
import java.io.IOException
import java.io.OutputStream
import org.json.JSONArray
import org.json.JSONObject

/** WHPP (InkHole P2P Protocol) 常量与读写工具。 */
object WHPP {
    val MAGIC = "WHPP".toByteArray(Charsets.US_ASCII)
    val CAP_MAGIC = "WHPC".toByteArray(Charsets.US_ASCII)
    const val CAP_VERSION = 2
    const val FOLDER_KIND = "folder-v1"
    const val BUFFER_SIZE = 256 * 1024
    const val MAX_HEADER = 64 * 1024              // header 长度上限(来自网络，不可信)
    const val MAX_FILE_SIZE = 1L shl 40           // 单文件 1TB 上限，防恶意 size 声明
    const val ACK_OK: Int = 0x01                  // 接收方回执：成功落盘
    const val ACK_FAIL: Int = 0x00                // 接收方回执：失败

    data class Header(
        val filename: String,
        val size: Long,
        val encrypted: Boolean,
        val wantAck: Boolean,
        val encMode: String = "",   // "" = WHE1 整块; "chunked" = WHE2 分块流
        val kind: String = "file",
        val plainSize: Long = size,
        val modifiedMs: Long = 0,
    )

    data class Capabilities(
        val instanceId: String,
        val peerName: String,
        val capabilities: Set<String>,
    )

    /** 把 header JSON 序列化(与桌面版 Python 完全一致)。 */
    fun encodeHeader(h: Header): ByteArray {
        val json = JSONObject()
        json.put("filename", h.filename)
        json.put("size", h.size)
        json.put("encrypted", h.encrypted)
        json.put("want_ack", h.wantAck)
        if (h.encMode.isNotEmpty()) json.put("enc_mode", h.encMode)
        if (h.kind != "file") json.put("kind", h.kind)
        if (h.kind == FOLDER_KIND || h.plainSize != h.size) json.put("plain_size", h.plainSize)
        if (h.modifiedMs > 0) json.put("mtime_ms", h.modifiedMs)
        return json.toString().toByteArray(Charsets.UTF_8)
    }

    /** 从 JSON bytes 解析 header。 */
    fun decodeHeader(bytes: ByteArray): Header {
        val json = JSONObject(String(bytes, Charsets.UTF_8))
        return Header(
            filename = json.getString("filename"),
            size = json.getLong("size"),
            encrypted = json.optBoolean("encrypted", false),
            wantAck = json.optBoolean("want_ack", false),
            encMode = json.optString("enc_mode", ""),
            kind = json.optString("kind", "file"),
            plainSize = json.optLong("plain_size", json.getLong("size")),
            modifiedMs = json.optLong("mtime_ms", 0),
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

    /** 向输出流写 WHPP 帧(magic + header + 数据)。onProgress 传已发送字节数。 */
    fun writeFrame(
        out: OutputStream,
        filename: String,
        size: Long,
        encrypted: Boolean,
        dataStream: InputStream,
        wantAck: Boolean = true,
        onProgress: ((Long) -> Unit)? = null,
        shouldCancel: (() -> Boolean)? = null,
    ) {
        writeHeader(out, Header(filename, size, encrypted, wantAck))
        // 写文件数据
        val buf = ByteArray(BUFFER_SIZE)
        var sent = 0L
        while (sent < size) {
            if (shouldCancel?.invoke() == true) throw InterruptedIOException("发送已取消")
            val wanted = minOf(buf.size.toLong(), size - sent).toInt()
            val n = dataStream.read(buf, 0, wanted)
            if (n < 0) throw EOFException("文件读取不完整")
            if (n == 0) continue
            out.write(buf, 0, n)
            sent += n
            onProgress?.invoke(sent)
        }
        out.flush()
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

    /** WHPC v2 response: magic + JSON length + identity and capabilities. */
    fun writeCapabilities(out: OutputStream, instanceId: String, peerName: String) {
        val body = JSONObject().apply {
            put("version", CAP_VERSION)
            put("caps", JSONArray().put(FOLDER_KIND))
            put("instance_id", instanceId)
            put("peer_name", peerName)
        }.toString().toByteArray(Charsets.UTF_8)
        DataOutputStream(out).apply {
            write(CAP_MAGIC)
            writeInt(body.size)
            write(body)
            flush()
        }
    }

    fun readCapabilities(input: InputStream): Capabilities {
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
        val capsJson = json.optJSONArray("caps") ?: throw IOException("missing WHPC caps")
        if (json.optInt("version", 0) != CAP_VERSION ||
            !instanceId.matches(Regex("[0-9a-f]{32}")) || peerName.isEmpty()) {
            throw IOException("unsupported WHPC version")
        }
        val caps = LinkedHashSet<String>()
        for (index in 0 until capsJson.length()) {
            val value = capsJson.opt(index)
            if (value !is String) throw IOException("bad WHPC capability")
            caps.add(value)
        }
        return Capabilities(instanceId, peerName, caps)
    }
}
