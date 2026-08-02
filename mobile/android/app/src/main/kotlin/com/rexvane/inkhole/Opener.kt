package com.rexvane.inkhole

import android.app.DownloadManager
import android.content.ContentUris
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.DocumentsContract
import android.provider.MediaStore
import android.webkit.MimeTypeMap
import androidx.core.content.FileProvider
import androidx.documentfile.provider.DocumentFile
import java.io.File

/**
 * 收件条目的「打开」与收件目录的可读路径。
 *
 * 收件成品可能落在三个地方:导出失败时留在应用私有收件箱、用户选了 SAF 自定义
 * 目录、或默认的 Download/InkHole(API 29+ 只有 MediaStore 记录,没有可直接
 * 访问的文件路径)。这里按同样的顺序依次解析,都解不出来就回退到系统下载管理。
 */
object Opener {

    /** 打开结果:exact 表示直接命中了那个文件,否则只是打开了下载目录。 */
    const val RESULT_EXACT = "exact"
    const val RESULT_DOWNLOADS = "downloads"

    /** 默认收件落点的绝对路径,例如 /storage/emulated/0/Download/InkHole。 */
    fun downloadsPath(): String = File(
        Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
        INBOX_FOLDER,
    ).absolutePath

    /**
     * 解析出能打开该收件条目的 Intent;返回 null 表示只能回退到下载目录。
     *
     * 会读 MediaStore / SAF,必须在工作线程调用。
     */
    fun viewIntent(ctx: Context, path: String, name: String, treeUri: String?): Intent? {
        val direct = File(path)
        if (path.isNotEmpty() && direct.isFile) {
            val uri = runCatching {
                FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", direct)
            }.getOrNull()
            if (uri != null) return view(uri, guessMime(direct.name))
        }
        if (name.isEmpty()) return null
        if (!treeUri.isNullOrEmpty()) {
            val child = runCatching {
                DocumentFile.fromTreeUri(ctx, Uri.parse(treeUri))?.findFile(name)
            }.getOrNull()
            if (child != null && child.isFile) {
                return view(child.uri, child.type ?: guessMime(name))
            }
        }
        if (Build.VERSION.SDK_INT >= 29) {
            downloadUri(ctx, name)?.let { return view(it, guessMime(name)) }
        } else {
            val legacy = File(File(downloadsPath()), name)
            if (legacy.isFile) {
                val uri = runCatching {
                    FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", legacy)
                }.getOrNull()
                if (uri != null) return view(uri, guessMime(name))
            }
        }
        return null
    }

    /** 系统下载管理界面;文件夹收件和 MediaStore 查不到的情况都走这里。 */
    fun downloadsIntent(): Intent = Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)

    /**
     * 把 SAF 树 URI 解成可读路径:
     * `content://…/tree/primary%3ADocs%2FInk` → `内部存储/Docs/Ink`。
     * 非本机存储的提供方(网盘等)没有真实路径,退回「目录名(文档 ID 尾段)」。
     */
    fun describeTree(ctx: Context, raw: String): String {
        val uri = runCatching { Uri.parse(raw) }.getOrNull() ?: return raw
        val documentId = runCatching { DocumentsContract.getTreeDocumentId(uri) }.getOrNull()
        if (documentId != null && uri.authority == EXTERNAL_STORAGE_AUTHORITY) {
            val volume = documentId.substringBefore(':')
            val relative = documentId.substringAfter(':', "")
            val root = if (volume.equals("primary", ignoreCase = true)) {
                "内部存储"
            } else {
                "存储卡($volume)"
            }
            return if (relative.isEmpty()) root else "$root/$relative"
        }
        val label = runCatching { DocumentFile.fromTreeUri(ctx, uri)?.name }.getOrNull()
        val tail = documentId?.substringAfterLast(':')?.substringAfterLast('/').orEmpty()
        return when {
            !label.isNullOrEmpty() && tail.isNotEmpty() && tail != label -> "$label（$tail）"
            !label.isNullOrEmpty() -> label
            else -> raw
        }
    }

    private fun view(uri: Uri, mime: String): Intent = Intent(Intent.ACTION_VIEW)
        .setDataAndType(uri, mime)
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)

    /** 在 Download/InkHole 下按文件名找最近一条记录(同名文件导出时会加序号)。 */
    private fun downloadUri(ctx: Context, name: String): Uri? {
        val prefix = "${Environment.DIRECTORY_DOWNLOADS}/$INBOX_FOLDER"
        return runCatching {
            ctx.contentResolver.query(
                MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                arrayOf(MediaStore.MediaColumns._ID),
                "${MediaStore.MediaColumns.DISPLAY_NAME} = ? AND " +
                    "${MediaStore.MediaColumns.RELATIVE_PATH} LIKE ?",
                arrayOf(name, "$prefix%"),
                "${MediaStore.MediaColumns._ID} DESC",
            )?.use { cursor ->
                if (!cursor.moveToFirst()) return@use null
                ContentUris.withAppendedId(
                    MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                    cursor.getLong(0),
                )
            }
        }.getOrNull()
    }

    private fun guessMime(name: String): String {
        val ext = name.substringAfterLast('.', "").lowercase()
        return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext)
            ?: "application/octet-stream"
    }

    private const val INBOX_FOLDER = "InkHole"
    private const val EXTERNAL_STORAGE_AUTHORITY = "com.android.externalstorage.documents"
}
