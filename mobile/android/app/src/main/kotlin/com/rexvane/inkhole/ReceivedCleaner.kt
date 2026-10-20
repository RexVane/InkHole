package com.rexvane.inkhole

import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import androidx.documentfile.provider.DocumentFile
import java.io.File

/**
 * 收件清理器：「清空收件记录」时把对应的实体文件也删掉。
 *
 * 一条收件记录可能对应三个位置，按同样的优先级逐个尝试：
 * 1. 应用私有收件箱（导出失败时的兜底，有真实路径，可直接删）；
 * 2. 用户选的 SAF 目录（按文件名在该树下查找）；
 * 3. 默认 Download/InkHole（API 29+ 只有 MediaStore 记录；文件夹收件的
 *    内容按 RELATIVE_PATH 前缀一起删）。
 *
 * 删不掉的条目不抛异常，计入 skipped，由调用方如实提示，避免"看起来删了
 * 其实还在"。
 */
object ReceivedCleaner {

    data class Outcome(val deleted: Int, val skipped: Int)

    fun clear(
        ctx: Context,
        paths: List<String>,
        names: List<String>,
        treeUri: String?,
    ): Outcome {
        var deleted = 0
        var skipped = 0
        val tree = treeUri
            ?.takeIf { it.isNotEmpty() }
            ?.let { runCatching { DocumentFile.fromTreeUri(ctx, Uri.parse(it)) }.getOrNull() }
        for (index in names.indices) {
            val name = ReceiveFiles.safeName(names[index])
            val path = paths.getOrNull(index).orEmpty()
            val removed = deletePrivate(path) ||
                deleteFromTree(tree, name, path) ||
                deleteFromMediaStore(ctx, name)
            if (removed) deleted++ else skipped++
        }
        return Outcome(deleted, skipped)
    }

    /** 私有收件箱里的文件/文件夹（文件夹收件时是一条目录）。 */
    private fun deletePrivate(path: String): Boolean {
        if (path.isEmpty()) return false
        val file = File(path)
        if (!file.exists()) return false
        return runCatching {
            if (file.isDirectory) file.deleteRecursively() else file.delete()
        }.getOrDefault(false)
    }

    private fun deleteFromTree(tree: DocumentFile?, name: String, path: String): Boolean {
        if (tree == null) return false
        // 导出时会做重名去重（name (2).ext），所以按真实文件名先找一次，
        // 找不到再退回记录里的名字。
        val candidates = listOfNotNull(
            path.takeIf { it.isNotEmpty() }?.let { File(it).name },
            name,
        ).distinct()
        for (candidate in candidates) {
            val target = runCatching { tree.findFile(candidate) }.getOrNull() ?: continue
            if (runCatching { target.delete() }.getOrDefault(false)) return true
        }
        return false
    }

    private fun deleteFromMediaStore(ctx: Context, name: String): Boolean {
        if (Build.VERSION.SDK_INT < 29) return deleteFromLegacyDownloads(name)
        val relativeRoot = "${Environment.DIRECTORY_DOWNLOADS}/${Opener.INBOX_FOLDER}"
        // 同名文件（导出去重时可能带 " (n)" 后缀）与整棵文件夹子树各删一次。
        val filesRemoved = runCatching {
            ctx.contentResolver.delete(
                MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                "${MediaStore.MediaColumns.DISPLAY_NAME} = ? OR " +
                    "${MediaStore.MediaColumns.DISPLAY_NAME} LIKE ?",
                arrayOf(name, "$name (%"),
            )
        }.getOrDefault(0)
        if (filesRemoved > 0) return true
        val folderRemoved = runCatching {
            ctx.contentResolver.delete(
                MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                "${MediaStore.MediaColumns.RELATIVE_PATH} LIKE ?",
                arrayOf("$relativeRoot/$name/%"),
            )
        }.getOrDefault(0)
        return folderRemoved > 0
    }

    private fun deleteFromLegacyDownloads(name: String): Boolean {
        val root = File(
            Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
            Opener.INBOX_FOLDER,
        )
        val legacy = File(root, name)
        if (!legacy.exists()) return false
        return runCatching {
            if (legacy.isDirectory) legacy.deleteRecursively() else legacy.delete()
        }.getOrDefault(false)
    }
}
