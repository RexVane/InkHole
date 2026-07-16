package com.rexvane.inkhole

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/** GitHub Releases 检查更新与 APK 应用内更新。 */
object Updater {

    const val RELEASES_PAGE = "https://github.com/RexVane/InkHole/releases/latest"
    private const val API = "https://api.github.com/repos/RexVane/InkHole/releases/latest"

    data class Info(val version: String, val apkUrl: String, val notes: String)

    /** 把 GitHub Release Markdown 压缩为更新弹窗里的简短要点。 */
    internal fun summarizeReleaseNotes(raw: String, maxItems: Int = 4): String {
        val items = mutableListOf<String>()
        val stopSections = listOf("安装", "下载", "校验", "checksum")
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

    /** 拉取最新 Release(阻塞,调用方放线程)。失败抛异常。
     *
     * GitHub API 匿名限流按出口 IP 计,挂代理极易 403;API 失败后回退
     * 读 releases/latest 的重定向 Location 解析 tag(网页端无限流),
     * APK 地址按 CI 命名规则 InkHole-<tag>.apk 直接构造,更新说明为空。
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
        conn.connectTimeout = 10_000
        conn.readTimeout = 15_000
        conn.setRequestProperty("User-Agent", "InkHole-Updater")
        conn.setRequestProperty("Accept", "application/vnd.github+json")
        conn.inputStream.use { input ->
            val data = JSONObject(input.bufferedReader().readText())
            val tag = data.optString("tag_name").trim()
            val notes = summarizeReleaseNotes(data.optString("body"))
            var apk = ""
            val assets = data.optJSONArray("assets")
            if (assets != null) {
                for (i in 0 until assets.length()) {
                    val a = assets.optJSONObject(i) ?: continue
                    if (a.optString("name").endsWith(".apk")) {
                        apk = a.optString("browser_download_url")
                        break
                    }
                }
            }
            return Info(tag, apk, notes)
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
        val apk = "https://github.com/RexVane/InkHole/releases/download/$tag/InkHole-$tag.apk"
        return Info(tag, apk, "")
    }

    /** 下载 APK 到应用缓存(阻塞,调用方放线程)。返回文件。 */
    fun downloadApk(ctx: Context, url: String, onProgress: (Int) -> Unit): File {
        val dir = File(ctx.cacheDir, "update").apply { mkdirs() }
        val dst = File(dir, "InkHole-update.apk")
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 15_000
        conn.readTimeout = 30_000
        conn.instanceFollowRedirects = true
        conn.setRequestProperty("User-Agent", "InkHole-Updater")
        val total = conn.contentLengthLong
        conn.inputStream.use { input ->
            dst.outputStream().use { out ->
                val buf = ByteArray(256 * 1024)
                var done = 0L
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    done += n
                    if (total > 0) onProgress((done * 100 / total).toInt())
                }
            }
        }
        return dst
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
