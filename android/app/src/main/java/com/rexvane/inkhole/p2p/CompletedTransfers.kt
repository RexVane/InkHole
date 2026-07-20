package com.rexvane.inkhole.p2p

import java.io.File
import java.security.MessageDigest
import org.json.JSONObject

internal data class CompletedTransfer(
    val transferId: String,
    val filename: String,
    val path: String,
    val completedAt: Long,
)

internal object CompletedTransfers {
    private val receiptName = Regex("^\\.inkhole-([0-9a-f]{64})\\.done\\.json$")

    fun pending(inboxDir: File): List<CompletedTransfer> {
        val root = try {
            inboxDir.canonicalFile
        } catch (_: Exception) {
            return emptyList()
        }
        val prefix = root.path + File.separator
        return root.listFiles().orEmpty().mapNotNull { receipt ->
            val transferId = receiptName.matchEntire(receipt.name)
                ?.groupValues?.get(1) ?: return@mapNotNull null
            val data = try {
                if (!receipt.isFile) return@mapNotNull null
                JSONObject(receipt.readText(Charsets.UTF_8))
            } catch (_: Exception) {
                return@mapNotNull null
            }
            val filename = data.optString("filename").trim()
            val storedPath = data.optString("path").trim()
            if (filename.isEmpty() || storedPath.isEmpty()) return@mapNotNull null
            val destination = try {
                File(storedPath).canonicalFile
            } catch (_: Exception) {
                return@mapNotNull null
            }
            if (!destination.path.startsWith(prefix) ||
                (!destination.isFile && !destination.isDirectory)) {
                return@mapNotNull null
            }
            CompletedTransfer(
                transferId = transferId,
                filename = ReceiveFiles.safeName(filename),
                path = destination.absolutePath,
                completedAt = data.optLong("completed_at", 0L),
            )
        }.sortedBy(CompletedTransfer::completedAt)
    }
}

internal object ReceiveCommits {
    fun recover(inboxDir: File, journal: JSONObject?, part: File,
                metadata: JSONObject, isFolder: Boolean): File? {
        if (!WHPP.metadataMatches(journal, metadata) || journal == null) return null
        val storedPath = journal.optString("path").trim()
        if (storedPath.isEmpty()) return null
        val root = try { inboxDir.canonicalFile } catch (_: Exception) { return null }
        val destination = try { File(storedPath).absoluteFile } catch (_: Exception) { return null }
        val canonicalDestination = try { destination.canonicalFile } catch (_: Exception) { return null }
        if (canonicalDestination.parentFile != root ||
            canonicalDestination.name != destination.name) return null

        val expectedSize = metadata.optLong("plain_size", -1L)
        val expectedDigest = metadata.optString("sha256")
        if (expectedSize < 0 || expectedDigest.length != 64) return null
        return try {
            if (isFolder) {
                val canonicalPart = part.canonicalFile
                if (!destination.isDirectory || !part.isFile ||
                    canonicalPart.parentFile != root || canonicalPart.name != part.name ||
                    part.length() != expectedSize || sha256(part) != expectedDigest) null
                else destination
            } else {
                if (!destination.isFile || destination.length() != expectedSize ||
                    sha256(destination) != expectedDigest) null
                else destination
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().buffered(WHPP.BUFFER_SIZE).use { input ->
            val buffer = ByteArray(WHPP.BUFFER_SIZE)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                if (count > 0) digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}
