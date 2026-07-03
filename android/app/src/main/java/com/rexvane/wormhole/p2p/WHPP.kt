package com.rexvane.wormhole.p2p

import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.InputStream
import java.io.OutputStream
import org.json.JSONObject

/** WHPP (Wormhole P2P Protocol) 常量与读写工具。 */
object WHPP {
    val MAGIC = "WHPP".toByteArray(Charsets.US_ASCII)
    const val BUFFER_SIZE = 65536

    data class Header(
        val filename: String,
        val size: Long,
        val encrypted: Boolean,
    )

    /** 把 header JSON 序列化(与桌面版 Python 完全一致)。 */
    fun encodeHeader(h: Header): ByteArray {
        val json = JSONObject()
        json.put("filename", h.filename)
        json.put("size", h.size)
        json.put("encrypted", h.encrypted)
        return json.toString().toByteArray(Charsets.UTF_8)
    }

    /** 从 JSON bytes 解析 header。 */
    fun decodeHeader(bytes: ByteArray): Header {
        val json = JSONObject(String(bytes, Charsets.UTF_8))
        return Header(
            filename = json.getString("filename"),
            size = json.getLong("size"),
            encrypted = json.optBoolean("encrypted", false),
        )
    }

    /** 向输出流写 WHPP 帧(magic + header + 数据)。 */
    fun writeFrame(
        out: OutputStream,
        filename: String,
        size: Long,
        encrypted: Boolean,
        dataStream: InputStream,
    ) {
        val header = encodeHeader(Header(filename, size, encrypted))
        val dout = DataOutputStream(out)
        dout.write(MAGIC)
        dout.writeInt(header.size)          // big-endian, 与 Python struct.pack("!I") 一致
        dout.write(header)
        dout.flush()
        // 写文件数据
        val buf = ByteArray(BUFFER_SIZE)
        while (true) {
            val n = dataStream.read(buf)
            if (n < 0) break
            out.write(buf, 0, n)
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
        val headerBytes = ByteArray(headerLen)
        din.readFully(headerBytes)
        return decodeHeader(headerBytes)
    }
}
