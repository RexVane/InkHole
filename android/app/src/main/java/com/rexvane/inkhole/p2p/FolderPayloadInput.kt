package com.rexvane.inkhole.p2p

import java.io.DataInputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream

internal interface VerifiablePayloadInput {
    fun verifyComplete()
}

internal class BoundedPayloadInputStream(
    private val source: InputStream,
    private val expected: Long,
    private val onProgress: (Long) -> Unit,
) : InputStream(), VerifiablePayloadInput {
    private var consumed = 0L

    override fun read(): Int {
        val one = ByteArray(1)
        return if (read(one, 0, 1) < 0) -1 else one[0].toInt() and 0xff
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        if (consumed == expected) return -1
        val wanted = minOf(length.toLong(), expected - consumed).toInt()
        val read = source.read(buffer, offset, wanted)
        if (read < 0) throw EOFException("folder payload is incomplete")
        if (read > 0) {
            consumed += read
            onProgress(consumed)
        }
        return read
    }

    override fun verifyComplete() {
        if (consumed != expected) throw IOException("folder payload size mismatch")
    }
}

internal class ChunkedFolderInputStream(
    source: InputStream,
    private val wireSize: Long,
    private val plainSize: Long,
    secret: String,
    private val onProgress: (Long) -> Unit,
) : InputStream(), VerifiablePayloadInput {
    private val input = DataInputStream(source)
    private val decryptor: Crypto.ChunkedDecryptor
    private var wireConsumed = 0L
    private var plainProduced = 0L
    private var chunk = ByteArray(0)
    private var chunkOffset = 0

    init {
        if (wireSize < 32 || plainSize < 0) {
            throw IllegalArgumentException("bad chunked encryption size")
        }
        val header = ByteArray(32)
        input.readFully(header)
        wireConsumed = 32
        onProgress(wireConsumed)
        decryptor = Crypto.ChunkedDecryptor(secret, header)
    }

    private fun loadChunk(): Boolean {
        if (wireConsumed == wireSize) {
            if (plainProduced != plainSize) throw EOFException("folder plaintext is incomplete")
            return false
        }
        val ciphertextSize = try {
            input.readInt()
        } catch (e: IOException) {
            throw EOFException("encrypted folder is incomplete").also { it.initCause(e) }
        }
        if (ciphertextSize < 16 || ciphertextSize > Crypto.CHUNK_SIZE + 16 ||
            wireConsumed + 4L + ciphertextSize > wireSize) {
            throw IOException("bad encrypted folder chunk")
        }
        val ciphertext = ByteArray(ciphertextSize)
        input.readFully(ciphertext)
        val plain = decryptor.decryptChunk(ciphertext)
            ?: throw IOException("folder decryption failed")
        if (plainProduced + plain.size > plainSize) {
            throw IOException("folder plaintext exceeds declared size")
        }
        wireConsumed += 4L + ciphertextSize
        plainProduced += plain.size
        chunk = plain
        chunkOffset = 0
        onProgress(wireConsumed)
        return true
    }

    override fun read(): Int {
        val one = ByteArray(1)
        return if (read(one, 0, 1) < 0) -1 else one[0].toInt() and 0xff
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        if (chunkOffset >= chunk.size && !loadChunk()) return -1
        val count = minOf(length, chunk.size - chunkOffset)
        System.arraycopy(chunk, chunkOffset, buffer, offset, count)
        chunkOffset += count
        if (chunkOffset == chunk.size) {
            chunk = ByteArray(0)
            chunkOffset = 0
        }
        return count
    }

    override fun verifyComplete() {
        if (wireConsumed != wireSize || plainProduced != plainSize || chunkOffset != chunk.size) {
            throw IOException("encrypted folder size mismatch")
        }
    }
}
