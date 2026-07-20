package com.rexvane.inkhole.p2p

import java.io.File
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
