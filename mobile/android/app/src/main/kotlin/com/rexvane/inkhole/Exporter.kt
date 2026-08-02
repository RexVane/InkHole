package com.rexvane.inkhole

import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.webkit.MimeTypeMap
import androidx.documentfile.provider.DocumentFile
import java.io.File
import java.io.IOException

/**
 * 收件导出器:把 Rust 私有收件箱里的成品文件/文件夹搬到用户可见位置。
 *
 * 默认导出到系统 Download/InkHole(API 29+ 走 MediaStore 免权限,
 * ≤28 需 WRITE_EXTERNAL_STORAGE);用户通过 SAF 选择了自定义目录时优先
 * 写入该目录树。任何一步失败都保留私有文件兜底,绝不丢数据。
 * 实现移植自 v1.7.3 InkHoleService(1MB 缓冲、唯一名、IS_PENDING 两段提交)。
 */
object Exporter {

    private const val EXPORT_BUFFER = 1024 * 1024
    private val exportLock = Any()

    /** 导出结果:display 为对用户展示的落点(如 Download/InkHole/xx)。 */
    data class Outcome(val name: String, val location: String)

    fun export(ctx: Context, path: String, treeUri: String?): Outcome {
        val source = File(path)
        if (!source.exists()) return Outcome(source.name, "")
        if (!treeUri.isNullOrEmpty()) {
            try {
                return exportToTree(ctx, source, Uri.parse(treeUri))
            } catch (_: Exception) {
                // 自定义目录不可写(被删除/权限被收回)时回退默认下载目录。
            }
        }
        return exportToDownloads(ctx, source)
    }

    // ---- 自定义目录(SAF 树) ----

    private fun exportToTree(ctx: Context, src: File, tree: Uri): Outcome {
        val root = DocumentFile.fromTreeUri(ctx, tree)
            ?: throw IOException("自定义目录不可用")
        if (!root.canWrite()) throw IOException("自定义目录不可写")
        val label = root.name ?: "自定义目录"
        synchronized(exportLock) {
            if (src.isDirectory) {
                val target = createUniqueDirectory(root, src.name)
                copyTreeInto(ctx, src, target)
                src.deleteRecursively()
                return Outcome(target.name ?: src.name, label)
            }
            val target = createUniqueFile(ctx, root, src.name)
            ctx.contentResolver.openOutputStream(target.uri)?.use { out ->
                src.inputStream().use { it.copyTo(out, EXPORT_BUFFER) }
            } ?: throw IOException("无法写入自定义目录")
            src.delete()
            return Outcome(target.name ?: src.name, label)
        }
    }

    private fun copyTreeInto(ctx: Context, src: File, target: DocumentFile) {
        val entries = src.listFiles() ?: return
        for (entry in entries.sortedBy { it.name }) {
            if (entry.isDirectory) {
                val child = target.createDirectory(ReceiveFiles.safeName(entry.name))
                    ?: throw IOException("无法创建子目录")
                copyTreeInto(ctx, entry, child)
            } else {
                val child = createUniqueFile(ctx, target, entry.name)
                ctx.contentResolver.openOutputStream(child.uri)?.use { out ->
                    entry.inputStream().use { it.copyTo(out, EXPORT_BUFFER) }
                } ?: throw IOException("无法写入子文件")
            }
        }
    }

    private fun createUniqueFile(ctx: Context, dir: DocumentFile, rawName: String): DocumentFile {
        val name = ReceiveFiles.safeName(rawName)
        val unique = uniqueDocumentName(dir, name)
        return dir.createFile(guessMime(unique), unique)
            ?: throw IOException("无法创建目标文件")
    }

    private fun createUniqueDirectory(dir: DocumentFile, rawName: String): DocumentFile {
        val name = ReceiveFiles.safeName(rawName)
        val unique = uniqueDocumentName(dir, name)
        return dir.createDirectory(unique) ?: throw IOException("无法创建目标目录")
    }

    private fun uniqueDocumentName(dir: DocumentFile, name: String): String {
        if (dir.findFile(name) == null) return name
        val dot = name.lastIndexOf('.').takeIf { it > 0 } ?: name.length
        val stem = name.substring(0, dot)
        val ext = name.substring(dot)
        var n = 2
        while (true) {
            val candidate = "$stem ($n)$ext"
            if (dir.findFile(candidate) == null) return candidate
            n++
        }
    }

    // ---- 默认:系统下载目录(移植自旧版) ----

    private fun exportToDownloads(ctx: Context, src: File): Outcome {
        if (src.isDirectory) return exportFolderToDownloads(ctx, src)
        val mime = guessMime(src.name)
        if (Build.VERSION.SDK_INT >= 29) {
            var inserted: Uri? = null
            try {
                val values = ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, src.name)
                    put(MediaStore.MediaColumns.MIME_TYPE, mime)
                    put(MediaStore.MediaColumns.RELATIVE_PATH, publicInboxRelativePath())
                    put(MediaStore.MediaColumns.IS_PENDING, 1)
                }
                inserted = ctx.contentResolver.insert(
                    MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                val uri = inserted ?: throw IOException("无法创建下载文件")
                val out = ctx.contentResolver.openOutputStream(uri)
                    ?: throw IOException("无法打开下载文件")
                // 1MB 缓冲:GB 级文件默认 8KB 会产生几十万次读写调用。
                out.use { output ->
                    src.inputStream().use { it.copyTo(output, EXPORT_BUFFER) }
                }
                val published = ctx.contentResolver.update(uri, ContentValues().apply {
                    put(MediaStore.MediaColumns.IS_PENDING, 0)
                }, null, null)
                if (published <= 0) throw IOException("无法发布下载文件")
                src.delete()
                return Outcome(src.name, "Download/InkHole")
            } catch (_: Exception) {
                inserted?.let { uri ->
                    try { ctx.contentResolver.delete(uri, null, null) } catch (_: Exception) {}
                }
            }
        } else if (ctx.checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
            == PackageManager.PERMISSION_GRANTED) {
            try {
                val dir = File(
                    Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
                    "InkHole",
                )
                if (!dir.exists() && !dir.mkdirs()) throw IOException("无法创建下载目录")
                synchronized(exportLock) {
                    val candidate = ReceiveFiles.uniqueFile(dir, src.name)
                    src.copyTo(candidate, overwrite = false, bufferSize = EXPORT_BUFFER)
                    src.delete()
                    return Outcome(candidate.name, "Download/InkHole")
                }
            } catch (_: Exception) {
                // 导出失败不丢文件:留在私有收件箱,仍可从 App 内访问。
            }
        }
        return Outcome(src.name, "")
    }

    private fun exportFolderToDownloads(ctx: Context, src: File): Outcome = synchronized(exportLock) {
        val files = src.walkTopDown().filter { it.isFile }
            .sortedBy { it.relativeTo(src).invariantSeparatorsPath }
            .toList()

        if (Build.VERSION.SDK_INT >= 29) {
            if (files.isEmpty()) {
                src.deleteRecursively()
                return@synchronized Outcome(src.name, "Download/InkHole")
            }
            val publicRoot = uniqueMediaStoreFolderName(ctx, src.name)
            val inserted = ArrayList<Uri>(files.size)
            try {
                for (file in files) {
                    val relative = file.relativeTo(src).invariantSeparatorsPath
                    val parent = relative.substringBeforeLast('/', "")
                    val relativePath = buildString {
                        append(publicInboxRelativePath())
                        append('/')
                        append(publicRoot)
                        if (parent.isNotEmpty()) {
                            append('/')
                            append(parent)
                        }
                    }
                    val values = ContentValues().apply {
                        put(MediaStore.MediaColumns.DISPLAY_NAME, file.name)
                        put(MediaStore.MediaColumns.MIME_TYPE, guessMime(file.name))
                        put(MediaStore.MediaColumns.RELATIVE_PATH, relativePath)
                        put(MediaStore.MediaColumns.IS_PENDING, 1)
                    }
                    val uri = ctx.contentResolver.insert(
                        MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                        ?: throw IOException("无法创建下载文件")
                    inserted += uri
                    val output = ctx.contentResolver.openOutputStream(uri)
                        ?: throw IOException("无法打开下载文件")
                    output.use { out ->
                        file.inputStream().use { it.copyTo(out, EXPORT_BUFFER) }
                    }
                }
                for (uri in inserted) {
                    val published = ctx.contentResolver.update(uri, ContentValues().apply {
                        put(MediaStore.MediaColumns.IS_PENDING, 0)
                    }, null, null)
                    if (published <= 0) throw IOException("无法发布下载文件夹")
                }
                src.deleteRecursively()
                return@synchronized Outcome(publicRoot, "Download/InkHole")
            } catch (_: Exception) {
                inserted.forEach { uri ->
                    try { ctx.contentResolver.delete(uri, null, null) } catch (_: Exception) {}
                }
                return@synchronized Outcome(src.name, "")
            }
        }

        if (ctx.checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
            == PackageManager.PERMISSION_GRANTED) {
            var destination: File? = null
            try {
                val root = File(
                    Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
                    "InkHole",
                )
                if (!root.isDirectory && !root.mkdirs()) throw IOException("无法创建下载目录")
                destination = ReceiveFiles.uniqueDirectory(root, src.name)
                if (!src.copyRecursively(destination, overwrite = false)) {
                    throw IOException("无法复制下载文件夹")
                }
                src.deleteRecursively()
                return@synchronized Outcome(destination.name, "Download/InkHole")
            } catch (_: Exception) {
                destination?.deleteRecursively()
            }
        }
        Outcome(src.name, "")
    }

    private fun uniqueMediaStoreFolderName(ctx: Context, name: String): String {
        var candidate = name
        var suffix = 2
        while (mediaStoreFolderExists(ctx, candidate)) {
            candidate = "$name ($suffix)"
            suffix++
        }
        return candidate
    }

    private fun mediaStoreFolderExists(ctx: Context, name: String): Boolean {
        if (Build.VERSION.SDK_INT < 29) return false
        val prefix = "${publicInboxRelativePath()}/$name/"
        return try {
            ctx.contentResolver.query(
                MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                arrayOf(MediaStore.MediaColumns._ID),
                "${MediaStore.MediaColumns.RELATIVE_PATH} LIKE ?",
                arrayOf("$prefix%"),
                null,
            )?.use { it.moveToFirst() } ?: false
        } catch (_: Exception) {
            false
        }
    }

    private fun guessMime(name: String): String {
        val ext = name.substringAfterLast('.', "").lowercase()
        return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext)
            ?: "application/octet-stream"
    }

    private fun publicInboxRelativePath(): String =
        "${Environment.DIRECTORY_DOWNLOADS}/InkHole"
}
