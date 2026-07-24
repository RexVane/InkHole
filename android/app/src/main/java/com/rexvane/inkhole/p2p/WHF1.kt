package com.rexvane.inkhole.p2p

import java.io.EOFException
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.text.Normalizer
import java.util.Locale

/** Streaming folder payload carried inside a WHPP kind=folder-v1 frame. */
internal object WHF1 {
    val MAGIC = "WHF1".toByteArray(Charsets.US_ASCII)
    const val ENTRY_HEADER_SIZE = 21
    const val TYPE_DIRECTORY = 0
    const val TYPE_FILE = 1
    const val MAX_ENTRIES = 100_000
    const val MAX_PATH_BYTES = 4096
    private const val MAX_DEPTH = 128
    private const val COPY_BUFFER = 1024 * 1024
    private const val INVALID_CHARS = "<>:\"|?*"
    private val WINDOWS_RESERVED = buildSet {
        addAll(listOf("CON", "PRN", "AUX", "NUL"))
        for (i in 1..9) {
            add("COM$i")
            add("LPT$i")
        }
    }

    data class Result(val entryCount: Int, val fileCount: Int, val fileBytes: Long)

    private class ExactReader(private val input: InputStream, private val limit: Long) {
        var consumed: Long = 0
            private set

        fun readExact(size: Int): ByteArray {
            if (size < 0 || consumed + size > limit) {
                throw IllegalArgumentException("folder size mismatch")
            }
            val result = ByteArray(size)
            var offset = 0
            while (offset < size) {
                val read = input.read(result, offset, size - offset)
                if (read < 0) throw EOFException("folder payload is incomplete")
                if (read == 0) continue
                offset += read
            }
            consumed += size
            return result
        }

        fun copyExact(output: FileOutputStream, size: Long) {
            if (size < 0 || consumed + size > limit) {
                throw IllegalArgumentException("folder file size mismatch")
            }
            val buffer = ByteArray(COPY_BUFFER)
            var remaining = size
            while (remaining > 0) {
                val wanted = minOf(buffer.size.toLong(), remaining).toInt()
                val read = input.read(buffer, 0, wanted)
                if (read < 0) throw EOFException("folder file is incomplete")
                if (read == 0) continue
                output.write(buffer, 0, read)
                remaining -= read
                consumed += read
            }
        }

        fun finish() {
            if (consumed != limit) throw IllegalArgumentException("folder size mismatch")
        }
    }

    private fun portableParts(path: String): Pair<List<String>, String> {
        if (path.isEmpty() || path.startsWith('/') || '\\' in path || '\u0000' in path) {
            throw IllegalArgumentException("unsafe folder path")
        }
        val parts = path.split('/')
        if (parts.size > MAX_DEPTH) throw IllegalArgumentException("folder path is too deep")
        val keys = ArrayList<String>(parts.size)
        for (part in parts) {
            val stem = part.substringBefore('.').uppercase(Locale.ROOT)
            if (part.isEmpty() || part == "." || part == ".." ||
                part.toByteArray(Charsets.UTF_8).size > 255 ||
                part.trimEnd('.', ' ') != part ||
                part.any { it.code < 32 || it in INVALID_CHARS } ||
                stem in WINDOWS_RESERVED) {
                throw IllegalArgumentException("unsupported folder name: ${part.ifEmpty { "?" }}")
            }
            keys += Normalizer.normalize(part, Normalizer.Form.NFC).uppercase(Locale.ROOT)
        }
        return parts to keys.joinToString("/")
    }

    private fun longValue(bytes: ByteArray, offset: Int): Long =
        ByteBuffer.wrap(bytes, offset, 8).order(ByteOrder.BIG_ENDIAN).long

    fun receive(input: InputStream, plainSize: Long, staging: File): Result {
        if (plainSize < 8 || plainSize > WHPP.MAX_FILE_SIZE) {
            throw IllegalArgumentException("bad folder plain size")
        }
        if (!staging.isDirectory) throw IllegalArgumentException("missing staging directory")
        val reader = ExactReader(input, plainSize)
        if (!reader.readExact(4).contentEquals(MAGIC)) {
            throw IllegalArgumentException("bad folder magic")
        }
        val count = ByteBuffer.wrap(reader.readExact(4)).order(ByteOrder.BIG_ENDIAN).int
        if (count < 0 || count > MAX_ENTRIES) throw IllegalArgumentException("too many entries")

        val seen = HashSet<String>()
        val fileKeys = HashSet<String>()
        val ancestorKeys = HashSet<String>()
        val directoryMtimes = ArrayList<Triple<File, Long, Int>>()
        val stagingCanonical = staging.canonicalFile
        val stagingPrefix = stagingCanonical.path + File.separator
        var fileCount = 0
        var fileBytes = 0L

        repeat(count) {
            val header = reader.readExact(ENTRY_HEADER_SIZE)
            val type = header[0].toInt() and 0xff
            val pathSize = ByteBuffer.wrap(header, 1, 4).order(ByteOrder.BIG_ENDIAN).int
            val size = longValue(header, 5)
            val modifiedMs = longValue(header, 13)
            if (pathSize <= 0 || pathSize > MAX_PATH_BYTES) {
                throw IllegalArgumentException("bad folder path length")
            }
            if (size < 0 || size > WHPP.MAX_FILE_SIZE || modifiedMs < 0) {
                throw IllegalArgumentException("bad folder entry metadata")
            }
            val pathBytes = reader.readExact(pathSize)
            val relative = pathBytes.toString(Charsets.UTF_8)
            if (!relative.toByteArray(Charsets.UTF_8).contentEquals(pathBytes)) {
                throw IllegalArgumentException("bad UTF-8 folder path")
            }
            val (parts, collisionKey) = portableParts(relative)
            if (!seen.add(collisionKey)) throw IllegalArgumentException("duplicate folder path")

            val normalizedParts = collisionKey.split('/')
            val parents = (1 until parts.size).map { normalizedParts.take(it).joinToString("/") }
            if (parents.any { it in fileKeys }) throw IllegalArgumentException("file/dir conflict")
            if (type == TYPE_FILE && collisionKey in ancestorKeys) {
                throw IllegalArgumentException("file/dir conflict")
            }
            if (type !in TYPE_DIRECTORY..TYPE_FILE || (type == TYPE_DIRECTORY && size != 0L)) {
                throw IllegalArgumentException("bad folder entry type")
            }
            ancestorKeys.addAll(parents)

            val target = parts.fold(stagingCanonical) { parent, child -> File(parent, child) }
                .canonicalFile
            if (!target.path.startsWith(stagingPrefix)) {
                throw IllegalArgumentException("folder path escaped staging")
            }
            if (type == TYPE_DIRECTORY) {
                if (!target.isDirectory && !target.mkdirs()) {
                    throw IllegalArgumentException("cannot create folder entry")
                }
                directoryMtimes += Triple(target, modifiedMs, parts.size)
            } else {
                fileKeys += collisionKey
                val parent = target.parentFile
                    ?: throw IllegalArgumentException("folder file has no parent")
                if (!parent.isDirectory && !parent.mkdirs()) {
                    throw IllegalArgumentException("cannot create folder parent")
                }
                if (!target.createNewFile()) throw IllegalArgumentException("duplicate folder file")
                FileOutputStream(target).use { reader.copyExact(it, size) }
                if (modifiedMs > 0) target.setLastModified(modifiedMs)
                fileCount++
                fileBytes += size
            }
        }

        reader.finish()
        directoryMtimes.sortedByDescending { it.third }.forEach { (directory, time, _) ->
            if (time > 0) directory.setLastModified(time)
        }
        return Result(count, fileCount, fileBytes)
    }
}
