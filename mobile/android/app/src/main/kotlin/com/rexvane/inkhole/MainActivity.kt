package com.rexvane.inkhole

import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import com.google.zxing.integration.android.IntentIntegrator
import com.journeyapps.barcodescanner.ScanIntentResult
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
    private var scannerChannel: MethodChannel? = null
    private var pendingScan: MethodChannel.Result? = null

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
                    "open" -> {
                        val path = call.argument<String>("path").orEmpty()
                        val name = call.argument<String>("name").orEmpty()
                        val treeUri = call.argument<String>("treeUri")
                        exportExecutor.execute { openReceived(path, name, treeUri, result) }
                    }
                    "downloadsPath" -> result.success(Opener.downloadsPath())
                    "describeTree" -> {
                        val uri = call.argument<String>("uri").orEmpty()
                        if (uri.isEmpty()) {
                            result.success(null)
                        } else {
                            exportExecutor.execute {
                                val described = Opener.describeTree(this, uri)
                                runOnUiThread { result.success(described) }
                            }
                        }
                    }
                    else -> result.notImplemented()
                }
            }
        }
        scannerChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, SCANNER_CHANNEL).also { channel ->
            channel.setMethodCallHandler { call: MethodCall, result: MethodChannel.Result ->
                when (call.method) {
                    "scan" -> startScan(result)
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
        when (requestCode) {
            REQUEST_PICK_DIRECTORY -> completeDirectoryPick(resultCode, data)
            REQUEST_SCAN -> {
                val pending = pendingScan ?: return
                pendingScan = null
                // contents 为 null 表示用户按返回键放弃了扫码。
                pending.success(ScanIntentResult.parseActivityResult(resultCode, data).contents)
            }
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_CAMERA) return
        if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            launchScanner()
        } else {
            val pending = pendingScan ?: return
            pendingScan = null
            pending.error("camera_denied", "需要相机权限才能扫描二维码", null)
        }
    }

    private fun completeDirectoryPick(resultCode: Int, data: Intent?) {
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

    /** 扫码前先要相机权限;权限回调里再真正拉起取景界面。 */
    private fun startScan(result: MethodChannel.Result) {
        if (pendingScan != null) {
            result.error("busy", "扫码进行中", null)
            return
        }
        pendingScan = result
        if (checkSelfPermission(android.Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED
        ) {
            launchScanner()
        } else {
            requestPermissions(arrayOf(android.Manifest.permission.CAMERA), REQUEST_CAMERA)
        }
    }

    private fun launchScanner() {
        val intent = IntentIntegrator(this)
            .setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
            .setPrompt("将一次性短码二维码放入框内")
            .setBeepEnabled(false)
            .setOrientationLocked(true)
            .setCaptureActivity(PortraitCaptureActivity::class.java)
            .createScanIntent()
        try {
            @Suppress("DEPRECATION")
            startActivityForResult(intent, REQUEST_SCAN)
        } catch (e: ActivityNotFoundException) {
            val pending = pendingScan ?: return
            pendingScan = null
            pending.error("scan_unavailable", e.message ?: "无法启动扫码", null)
        }
    }

    /** 打开收件条目;文件夹和查不到的条目回退到系统下载管理。 */
    private fun openReceived(
        path: String,
        name: String,
        treeUri: String?,
        result: MethodChannel.Result,
    ) {
        val intent = try {
            Opener.viewIntent(this, path, name, treeUri)
        } catch (_: Exception) {
            null
        }
        runOnUiThread {
            if (intent != null && tryStart(intent)) {
                result.success(Opener.RESULT_EXACT)
                return@runOnUiThread
            }
            if (tryStart(Opener.downloadsIntent())) {
                result.success(Opener.RESULT_DOWNLOADS)
            } else {
                result.error("open_failed", "没有可以打开该文件的应用", null)
            }
        }
    }

    private fun tryStart(intent: Intent): Boolean = try {
        startActivity(intent)
        true
    } catch (_: ActivityNotFoundException) {
        false
    } catch (_: SecurityException) {
        false
    }

    override fun onDestroy() {
        shareExecutor.shutdownNow()
        updateExecutor.shutdownNow()
        exportExecutor.shutdownNow()
        exporterChannel = null
        pendingDirectoryPick = null
        pendingScan = null
        scannerChannel = null
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
        private const val SCANNER_CHANNEL = "com.rexvane.inkhole/scanner"
        private const val REQUEST_PICK_DIRECTORY = 9107
        private const val REQUEST_SCAN = 9108
        private const val REQUEST_CAMERA = 9109
        private const val SHARE_CACHE_RETENTION_MS = 7L * 24 * 60 * 60 * 1000
    }
}
