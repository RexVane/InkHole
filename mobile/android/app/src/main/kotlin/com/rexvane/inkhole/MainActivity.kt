package com.rexvane.inkhole

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.android.FlutterActivity
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.util.LinkedHashSet
import java.util.concurrent.Executors

class MainActivity : FlutterActivity() {
    private val shareExecutor = Executors.newSingleThreadExecutor()
    private val pendingLock = Any()
    private val pendingSharedFiles = ArrayList<String>()
    private var shareChannel: MethodChannel? = null
    private var updaterChannel: MethodChannel? = null
    private val updateExecutor = Executors.newSingleThreadExecutor()
    private var exporterChannel: MethodChannel? = null
    private val exportExecutor = Executors.newSingleThreadExecutor()
    private var pendingDirectoryPick: MethodChannel.Result? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        startLanService()
        cleanupShareCache()
        handleIncomingIntent(intent)
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        shareChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, SHARE_CHANNEL).also { channel ->
            channel.setMethodCallHandler { call: MethodCall, result: MethodChannel.Result ->
                when (call.method) {
                    "consumeSharedFiles" -> result.success(drainPendingSharedFiles())
                    else -> result.notImplemented()
                }
            }
        }
        exporterChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, EXPORTER_CHANNEL).also { channel ->
            channel.setMethodCallHandler { call: MethodCall, result: MethodChannel.Result ->
                when (call.method) {
                    "export" -> {
                        val path = call.argument<String>("path").orEmpty()
                        val treeUri = call.argument<String>("treeUri")
                        exportExecutor.execute {
                            try {
                                val outcome = Exporter.export(this, path, treeUri)
                                runOnUiThread {
                                    result.success(
                                        mapOf("name" to outcome.name, "location" to outcome.location),
                                    )
                                }
                            } catch (e: Exception) {
                                runOnUiThread { result.error("export_failed", e.message ?: "导出失败", null) }
                            }
                        }
                    }
                    "pickDirectory" -> {
                        if (pendingDirectoryPick != null) {
                            result.error("busy", "目录选择进行中", null)
                        } else {
                            pendingDirectoryPick = result
                            val intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).addFlags(
                                Intent.FLAG_GRANT_READ_URI_PERMISSION or
                                    Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION,
                            )
                            @Suppress("DEPRECATION")
                            startActivityForResult(intent, REQUEST_PICK_DIRECTORY)
                        }
                    }
                    else -> result.notImplemented()
                }
            }
        }
        updaterChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, UPDATER_CHANNEL).also { channel ->
            channel.setMethodCallHandler { call: MethodCall, result: MethodChannel.Result ->
                when (call.method) {
                    "check" -> {
                        val current = call.argument<String>("current").orEmpty()
                        updateExecutor.execute {
                            try {
                                val info = Updater.fetchLatest()
                                runOnUiThread {
                                    result.success(
                                        mapOf(
                                            "version" to info.version,
                                            "apkUrl" to info.apkUrl,
                                            "notes" to info.notes,
                                            "newer" to Updater.versionNewer(info.version, current),
                                        ),
                                    )
                                }
                            } catch (e: Exception) {
                                runOnUiThread { result.error("check_failed", e.message ?: "检查更新失败", null) }
                            }
                        }
                    }
                    "downloadInstall" -> {
                        val url = call.argument<String>("url").orEmpty()
                        updateExecutor.execute {
                            try {
                                val apk = Updater.downloadApk(this, url) { percent ->
                                    runOnUiThread { updaterChannel?.invokeMethod("progress", percent) }
                                }
                                Updater.installApk(this, apk)
                                runOnUiThread { result.success(true) }
                            } catch (e: Exception) {
                                runOnUiThread { result.error("download_failed", e.message ?: "下载安装失败", null) }
                            }
                        }
                    }
                    else -> result.notImplemented()
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIncomingIntent(intent)
    }

    @Suppress("DEPRECATION")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_PICK_DIRECTORY) return
        val pending = pendingDirectoryPick ?: return
        pendingDirectoryPick = null
        val uri = data?.data
        if (resultCode != RESULT_OK || uri == null) {
            pending.success(null)
            return
        }
        try {
            contentResolver.takePersistableUriPermission(
                uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
            )
        } catch (_: SecurityException) {
        }
        val label = androidx.documentfile.provider.DocumentFile.fromTreeUri(this, uri)?.name
        pending.success(mapOf("uri" to uri.toString(), "label" to (label ?: "自定义目录")))
    }

    override fun onDestroy() {
        shareExecutor.shutdownNow()
        updateExecutor.shutdownNow()
        exportExecutor.shutdownNow()
        exporterChannel = null
        pendingDirectoryPick = null
        shareChannel = null
        updaterChannel = null
        super.onDestroy()
    }

    private fun startLanService() {
        val serviceIntent = Intent(this, InkHoleForegroundService::class.java)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(serviceIntent)
            } else {
                startService(serviceIntent)
            }
        } catch (_: IllegalStateException) {
            // The Activity may be restored while Android is still resuming it.
        } catch (_: SecurityException) {
            // A missing foreground-service permission must not prevent the UI from opening.
        }
    }

    private fun handleIncomingIntent(incoming: Intent?) {
        val uris = extractSharedUris(incoming)
        if (uris.isEmpty()) return

        shareExecutor.execute {
            val copied = uris.mapIndexedNotNull { index, uri -> copyUriToCache(uri, index) }
            if (copied.isEmpty()) return@execute
            runOnUiThread {
                synchronized(pendingLock) {
                    pendingSharedFiles.addAll(copied)
                }
                shareChannel?.invokeMethod("sharedFiles", copied)
            }
        }
    }

    private fun cleanupShareCache() {
        val root = File(cacheDir, "inkhole-share")
        val cutoff = System.currentTimeMillis() - SHARE_CACHE_RETENTION_MS
        root.listFiles()?.forEach { entry ->
            if (entry.lastModified() < cutoff) entry.deleteRecursively()
        }
    }

    private fun extractSharedUris(incoming: Intent?): List<Uri> {
        if (incoming == null ||
            (incoming.action != Intent.ACTION_SEND && incoming.action != Intent.ACTION_SEND_MULTIPLE)
        ) {
            return emptyList()
        }

        val uris = LinkedHashSet<Uri>()
        incoming.clipData?.let { clipData ->
            for (index in 0 until clipData.itemCount) {
                clipData.getItemAt(index).uri?.let(uris::add)
            }
        }

        if (incoming.action == Intent.ACTION_SEND_MULTIPLE) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                incoming.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                    ?.forEach(uris::add)
            } else {
                @Suppress("DEPRECATION")
                val streams = incoming.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)
                streams?.forEach(uris::add)
            }
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            incoming.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)?.let(uris::add)
        } else {
            @Suppress("DEPRECATION")
            incoming.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)?.let(uris::add)
        }
        return uris.toList()
    }

    private fun copyUriToCache(uri: Uri, index: Int): String? {
        return try {
            val displayName = contentResolver.query(
                uri,
                arrayOf(OpenableColumns.DISPLAY_NAME),
                null,
                null,
                null,
            )?.use { cursor ->
                val nameColumn = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (nameColumn >= 0 && cursor.moveToFirst()) cursor.getString(nameColumn) else null
            }
                ?: uri.lastPathSegment
                ?: "shared-file"

            val normalizedName = displayName
                .substringAfterLast('/')
                // Keep Unicode basenames; only remove separators, control
                // characters, and characters rejected by common filesystems.
                .replace(Regex("[\\\\/:*?\"<>|\\u0000-\\u001F]"), "_")
                .trimEnd('.', ' ')
                .take(120)
                .ifEmpty { "shared-file" }
            val safeName = if (normalizedName == "." || normalizedName == "..") {
                "shared-file"
            } else {
                normalizedName
            }
            val targetDirectory = File(
                cacheDir,
                "inkhole-share/${System.currentTimeMillis()}_${index}_${System.nanoTime()}",
            ).apply { mkdirs() }
            // Keep the original basename so the receiver sees the same filename as the share source.
            val target = File(targetDirectory, safeName)
            val input = contentResolver.openInputStream(uri) ?: return null
            input.use { source ->
                target.outputStream().use { destination -> source.copyTo(destination) }
            }
            target.absolutePath
        } catch (_: Exception) {
            null
        }
    }

    private fun drainPendingSharedFiles(): List<String> = synchronized(pendingLock) {
        val files = pendingSharedFiles.toList()
        pendingSharedFiles.clear()
        files
    }

    companion object {
        private const val SHARE_CHANNEL = "com.rexvane.inkhole/share"
        private const val UPDATER_CHANNEL = "com.rexvane.inkhole/updater"
        private const val EXPORTER_CHANNEL = "com.rexvane.inkhole/exporter"
        private const val REQUEST_PICK_DIRECTORY = 9107
        private const val SHARE_CACHE_RETENTION_MS = 7L * 24 * 60 * 60 * 1000
    }
}
