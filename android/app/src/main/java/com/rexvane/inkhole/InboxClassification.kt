package com.rexvane.inkhole

/** Stable inbox categories shared by settings and the public Downloads exporter. */
object InboxClassification {
    const val PREF_ENABLED = "inbox_auto_classify"
    const val MEDIA = "media"
    const val ARCHIVE = "archive"
    const val FILE = "file"
    const val FOLDER = "folder"

    val categories = listOf(MEDIA, ARCHIVE, FILE, FOLDER)

    private val labels = mapOf(
        MEDIA to "图片和视频",
        ARCHIVE to "压缩包",
        FILE to "文件",
        FOLDER to "文件夹",
    )
    private val mediaExtensions = setOf(
        "jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "heic",
        "heif", "svg", "mp4", "mov", "m4v", "mkv", "avi", "webm", "wmv",
        "flv", "mpeg", "mpg", "3gp", "ts",
    )
    private val archiveExtensions = setOf(
        "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz", "tbz", "tbz2",
        "txz", "zst", "lz", "lz4", "cab", "iso", "dmg",
    )

    fun preferenceKey(category: String) = "inbox_category_$category"

    fun label(category: String): String = labels[category] ?: labels.getValue(FILE)

    fun categoryFor(name: String, isDirectory: Boolean): String {
        if (isDirectory) return FOLDER
        val extension = name.substringAfterLast('.', "").lowercase()
        return when (extension) {
            in mediaExtensions -> MEDIA
            in archiveExtensions -> ARCHIVE
            else -> FILE
        }
    }

    fun directoryName(category: String, configured: String?): String {
        val fallback = label(category)
        val safe = configured.orEmpty()
            .filterNot { it.isISOControl() || it == '/' || it == '\\' }
            .trim()
            .trimEnd('.', ' ')
            .take(80)
        return safe.ifEmpty { fallback }
    }
}
