package com.rexvane.inkhole.p2p

import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.EOFException
import java.io.InterruptedIOException
import java.io.InputStream
import java.io.OutputStream
import org.json.JSONObject

/** WHPP (InkHole P2P Protocol) 常量与读写工具。 */
object WHPP {
    val MAGIC = "WHPP".toByteArray(Charsets.US_ASCII)
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
    )

    /** 把 header JSON 序列化(与桌面版 Python 完全一致)。 */
    fun encodeHeader(h: Header): ByteArray {
        val json = JSONObject()
        json.put("filename", h.filename)
        json.put("size", h.size)
        json.put("encrypted", h.encrypted)
        json.put("want_ack", h.wantAck)
        if (h.encMode.isNotEmpty()) json.put("enc_mode", h.encMode)
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
        val headerLen = din.readInt()       // big-endian
        if (headerLen <= 0 || headerLen > MAX_HEADER) throw IllegalArgumentException("bad header len")
        val headerBytes = ByteArray(headerLen)
        din.readFully(headerBytes)
        return decodeHeader(headerBytes)
    }
}
