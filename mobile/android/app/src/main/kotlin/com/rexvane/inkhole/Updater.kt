package com.rexvane.inkhole

import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/** GitHub Releases 检查更新与 APK 应用内更新。 */
object Updater {

    const val REPOSITORY_PAGE = "https://github.com/RexVane/InkHole"
    const val RELEASES_PAGE = "https://github.com/RexVane/InkHole/releases/latest"
    private const val API = "https://api.github.com/repos/RexVane/InkHole/releases/latest"
    private const val MAX_APK_SIZE = 250L * 1024 * 1024
    private val RELEASE_ABIS = setOf("arm64-v8a", "armeabi-v7a", "x86_64")

    data class Info(val version: String, val apkUrl: String, val notes: String)

    /** 把 GitHub Release Markdown 压缩为更新弹窗里的简短要点。 */
    internal fun summarizeReleaseNotes(raw: String, maxItems: Int = 4): String {
        val items = mutableListOf<String>()
        val stopSections = listOf(
            "安装", "下载", "校验", "install", "download", "verify", "checksum",
        )
        for (rawLine in raw.replace("\r\n", "\n").lines()) {
            val line = rawLine.trim()
            if (line.isEmpty()) continue
            if (line.startsWith("#")) {
                val heading = line.trimStart('#').trim().lowercase()
                if (items.isNotEmpty() && stopSections.any { it in heading }) break
                continue
            }
            if (line.startsWith(">")) continue

            var text = line
                .replace(Regex("!\\[[^]]*]\\([^)]*\\)"), "")
                .replace(Regex("\\[([^]]+)]\\([^)]*\\)"), "$1")
                .replace(Regex("^[-*+]\\s+"), "")
                .replace(Regex("^\\d+[.)]\\s+"), "")
                .replace("**", "")
                .replace("__", "")
                .replace("`", "")
                .trim()
            if (text.isEmpty() || text.startsWith("http://") || text.startsWith("https://")) {
                continue
            }
            if (text.length > 90) text = text.take(89).trimEnd() + "…"
            items.add("• $text")
            if (items.size >= maxItems) break
        }
        return items.joinToString("\n")
    }

    /** remote 是否比 local 新(容忍 v 前缀/位数不齐)。 */
    fun versionNewer(remote: String, local: String): Boolean {
        fun parts(v: String): List<Int> {
            val segs = v.trim().removePrefix("v").removePrefix("V").split(".")
            val out = segs.map { seg -> seg.filter { it.isDigit() }.toIntOrNull() ?: 0 }
            return out + List(maxOf(0, 4 - out.size)) { 0 }
        }
        val a = parts(remote); val b = parts(local)
        for (i in 0 until maxOf(a.size, b.size)) {
            val x = a.getOrElse(i) { 0 }; val y = b.getOrElse(i) { 0 }
            if (x != y) return x > y
        }
        return false
    }

    /** Pick the APK matching this device, with a generic fallback for legacy releases. */
    internal fun selectApkUrl(
        tag: String,
        assets: Map<String, String>,
        supportedAbis: List<String>,
    ): String {
        val prefix = "InkHole-$tag-"
        val hasAbiSplits = assets.keys.any {
            it.startsWith(prefix) && it.endsWith(".apk")
        }
        if (hasAbiSplits) {
            for (abi in supportedAbis) {
                if (abi !in RELEASE_ABIS) continue
                assets["$prefix$abi.apk"]?.let { return it }
            }
            return ""
        }
        return assets["InkHole-$tag.apk"].orEmpty()
    }

    /** 拉取最新 Release(阻塞,调用方放线程)。失败抛异常。
     *
     * GitHub API 匿名限流按出口 IP 计,挂代理极易 403;API 失败后回退
     * 读 releases/latest 的重定向 Location 解析 tag(网页端无限流),
     * APK 地址按设备 ABI 和 CI 命名规则直接构造,更新说明为空。
     */
    fun fetchLatest(): Info {
        return try {
            fetchLatestFromApi()
        } catch (apiExc: Exception) {
            try {
                fetchLatestFromRedirect()
            } catch (_: Exception) {
                throw apiExc
            }
        }
    }

    private fun fetchLatestFromApi(): Info {
        val conn = URL(API).openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 10_000
            conn.readTimeout = 15_000
            conn.setRequestProperty("User-Agent", "InkHole-Updater")
            conn.setRequestProperty("Accept", "application/vnd.github+json")
            conn.inputStream.use { input ->
                val data = JSONObject(input.bufferedReader().readText())
                val tag = data.optString("tag_name").trim()
                require(tag.isNotEmpty()) { "最新版本号为空" }
                val notes = summarizeReleaseNotes(data.optString("body"))
                val apkAssets = linkedMapOf<String, String>()
                val assets = data.optJSONArray("assets")
                if (assets != null) {
                    for (i in 0 until assets.length()) {
                        val a = assets.optJSONObject(i) ?: continue
                        val name = a.optString("name")
                        if (name.endsWith(".apk")) {
                            apkAssets[name] = a.optString("browser_download_url")
                        }
                    }
                }
                val apk = selectApkUrl(tag, apkAssets, Build.SUPPORTED_ABIS.toList())
                return Info(tag, apk, notes)
            }
        } finally {
            conn.disconnect()
        }
    }

    private fun fetchLatestFromRedirect(): Info {
        val conn = URL(RELEASES_PAGE).openConnection() as HttpURLConnection
        conn.connectTimeout = 10_000
        conn.readTimeout = 15_000
        conn.instanceFollowRedirects = false
        conn.setRequestProperty("User-Agent", "InkHole-Updater")
        val location = conn.getHeaderField("Location").orEmpty()
        conn.disconnect()
        val tag = location.trimEnd('/').substringAfterLast("/tag/", "")
        require(tag.isNotEmpty() && "/" !in tag) { "无法解析最新版本号" }
        val preferredAbi = Build.SUPPORTED_ABIS.firstOrNull { it in RELEASE_ABIS }
        val apkName = when (preferredAbi) {
            // The stable legacy filename remains an arm64 alias.
            "arm64-v8a" -> "InkHole-$tag.apk"
            null -> ""
            else -> "InkHole-$tag-$preferredAbi.apk"
        }
        val apk = if (apkName.isEmpty()) "" else {
            "https://github.com/RexVane/InkHole/releases/download/$tag/$apkName"
        }
        return Info(tag, apk, "")
    }

    /** 下载 APK 到应用缓存(阻塞,调用方放线程)。返回文件。 */
    @Synchronized
    fun downloadApk(ctx: Context, url: String, onProgress: (Int) -> Unit): File {
        val dir = File(ctx.cacheDir, "update").apply { mkdirs() }
        val dst = File(dir, "InkHole-update.apk")
        val source = URL(url)
        require(source.protocol.equals("https", ignoreCase = true)) { "更新地址不是 HTTPS" }
        val conn = source.openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 15_000
            conn.readTimeout = 30_000
            conn.instanceFollowRedirects = true
            conn.setRequestProperty("User-Agent", "InkHole-Updater")
            val code = conn.responseCode
            if (code !in 200..299) throw IOException("下载服务器返回 HTTP $code")
            val total = conn.contentLengthLong
            if (total > MAX_APK_SIZE) throw IOException("安装包大小异常")
            conn.inputStream.use { input ->
                dst.outputStream().use { out ->
                    val buf = ByteArray(256 * 1024)
                    var done = 0L
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        done += n
                        if (done > MAX_APK_SIZE) throw IOException("安装包大小异常")
                        out.write(buf, 0, n)
                        if (total > 0) onProgress((done * 100 / total).toInt())
                    }
                }
            }
            verifyApk(ctx, dst)
            return dst
        } catch (e: Exception) {
            dst.delete()
            throw e
        } finally {
            conn.disconnect()
        }
    }

    @Suppress("DEPRECATION")
    private fun verifyApk(ctx: Context, apk: File) {
        val pm = ctx.packageManager
        val flags = if (Build.VERSION.SDK_INT >= 28) {
            PackageManager.GET_SIGNING_CERTIFICATES
        } else {
            PackageManager.GET_SIGNATURES
        }
        val archive = pm.getPackageArchiveInfo(apk.absolutePath, flags)
            ?: throw IOException("下载内容不是有效 APK")
        if (archive.packageName != ctx.packageName) {
            throw IOException("安装包应用标识不匹配")
        }
        val installed = pm.getPackageInfo(ctx.packageName, flags)
        val archiveSigners = signerDigests(archive)
        val installedSigners = signerDigests(installed)
        if (archiveSigners.isEmpty() || archiveSigners != installedSigners) {
            throw IOException(
                "当前安装版本使用了与正式版不同的签名，Android 无法直接覆盖更新。" +
                    "若当前版本为 v2.0.0-v2.0.8，请先记录设置并卸载旧版，" +
                    "再仅从 GitHub Release 安装最新版；完成这次迁移后可正常应用内更新。",
            )
        }
    }

    @Suppress("DEPRECATION")
    private fun signerDigests(info: PackageInfo): Set<String> {
        val signatures = if (Build.VERSION.SDK_INT >= 28) {
            info.signingInfo?.apkContentsSigners.orEmpty()
        } else {
            info.signatures.orEmpty()
        }
        return signatures.mapTo(LinkedHashSet()) { signature ->
            MessageDigest.getInstance("SHA-256")
                .digest(signature.toByteArray())
                .joinToString("") { "%02x".format(it) }
        }
    }

    /** 拉起系统安装器(首次会引导授权"安装未知应用")。 */
    fun installApk(ctx: Context, apk: File) {
        val uri = FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", apk)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        ctx.startActivity(intent)
    }
}
