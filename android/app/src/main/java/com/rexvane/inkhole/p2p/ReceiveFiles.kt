package com.rexvane.inkhole.p2p

import java.io.File

internal object ReceiveFiles {
    private const val MAX_FILENAME_BYTES = 240
    private const val INVALID_CHARS = "<>:\"|?*"

    fun safeName(raw: String): String {
        var name = raw.replace('\\', '/').substringAfterLast('/')
        name = buildString(name.length) {
            for (ch in name) {
                append(if (ch.code < 32 || ch in INVALID_CHARS) '_' else ch)
            }
        }.trimEnd('.', ' ')
        if (name.isEmpty() || name == "." || name == "..") name = "unknown"
        return utf8Prefix(name, MAX_FILENAME_BYTES).trimEnd('.', ' ').ifEmpty { "unknown" }
    }

    fun uniqueFile(directory: File, filename: String): File {
        val initial = File(directory, filename)
        if (!initial.exists()) return initial

        val dot = filename.lastIndexOf('.').takeIf { it > 0 } ?: filename.length
        val stem = filename.substring(0, dot)
        val ext = filename.substring(dot)
        var n = 2
        while (true) {
            val candidate = File(directory, "$stem ($n)$ext")
            if (!candidate.exists()) return candidate
            n++
        }
    }

    fun uniqueDirectory(directory: File, name: String): File {
        val initial = File(directory, name)
        if (!initial.exists()) return initial
        var n = 2
        while (true) {
            val candidate = File(directory, "$name ($n)")
            if (!candidate.exists()) return candidate
            n++
        }
    }

    fun utf8Prefix(value: String, maxBytes: Int): String {
        var end = 0
        var used = 0
        while (end < value.length) {
            val codePoint = Character.codePointAt(value, end)
            val chars = Character.charCount(codePoint)
            val bytes = String(Character.toChars(codePoint)).toByteArray(Charsets.UTF_8).size
            if (used + bytes > maxBytes) break
            used += bytes
            end += chars
        }
        return value.substring(0, end)
    }
}
