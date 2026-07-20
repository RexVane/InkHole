package com.rexvane.inkhole

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.IBinder
import android.provider.MediaStore
import android.webkit.MimeTypeMap
import androidx.core.content.FileProvider
import com.rexvane.inkhole.p2p.Peer
import com.rexvane.inkhole.p2p.InkHoleListener
import com.rexvane.inkhole.p2p.InkHoleNode
import com.rexvane.inkhole.p2p.ReceiveFiles
import com.rexvane.inkhole.transport.TransportEventListener
import com.rexvane.inkhole.transport.TransportManager
import com.rexvane.inkhole.transport.TransferSecretStore
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.atomic.AtomicInteger

/**
 * 前台服务：拥有 P2P 节点的生命周期。
 *
 * 之前节点绑在 Activity 上——锁屏/切后台 Activity 被杀就断线，转屏还会
 * 重启节点(端口变、设备列表清空、传输中断)。挪进前台服务后手机侧真正
 * "常驻可收"。收到的文件导出到系统 Downloads/InkHole(用户找得到)并发通知。
 */
class InkHoleService : Service() {

    companion object {
        private const val CHANNEL_STATUS = "inkhole_status"
        private const val CHANNEL_FILES = "inkhole_files"
        private const val NOTIF_STATUS_ID = 1
        private const val ACTION_RELOAD = "com.rexvane.inkhole.RELOAD"
        private const val EXPORT_BUFFER = 1024 * 1024   // 导出到 Downloads 的复制缓冲

        fun start(context: Context) {
            val intent = Intent(context, InkHoleService::class.java)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
            else context.startService(intent)
        }

        /** 设置(名字/口令)变更后重建节点。
         * 不能用 stopService+startService：stop 是异步的，服务没销毁完时
         * start 只触发 onStartCommand 不触发 onCreate，节点不会重建。 */
        fun restart(context: Context) {
            val intent = Intent(context, InkHoleService::class.java).setAction(ACTION_RELOAD)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
            else context.startService(intent)
        }
    }

    private val fileNotifId = AtomicInteger(100)
    // 息屏后 vivo/各厂商会让 WiFi 休眠,TCP 监听对外不可达,对端把本机判离线。
    // 前台服务期间持有高性能 WifiLock,保持 WiFi 常联通(亮屏恢复即自愈的
    // "设备消失又回来"就是它治的)。
    private var wifiLock: android.net.wifi.WifiManager.WifiLock? = null
    private val exportLock = Any()

    override fun onCreate() {
        super.onCreate()
        createChannels()
        startForeground(NOTIF_STATUS_ID, buildStatusNotification("正在启动…"))
        try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE)
                as android.net.wifi.WifiManager
            @Suppress("DEPRECATION")
            val mode = if (Build.VERSION.SDK_INT >= 29)
                android.net.wifi.WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            else android.net.wifi.WifiManager.WIFI_MODE_FULL_HIGH_PERF
            wifiLock = wm.createWifiLock(mode, "inkhole:wifi").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (_: Exception) {}
        InkHoleBus.loadHistory(this)
        startNode()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_RELOAD) {
            TransportManager.detach()
            InkHoleBus.node?.stop()
            InkHoleBus.node = null
            InkHoleBus.lastPeers = emptyList()
            startNode()
        }
        return START_STICKY
    }

    private fun startNode() {
        val prefs = getSharedPreferences("inkhole", Context.MODE_PRIVATE)
        val storedName = prefs.getString("peer_name", Build.MODEL) ?: Build.MODEL
        val name = storedName.filterNot { it.isISOControl() }.trim().take(40)
            .ifEmpty { Build.MODEL }
        if (name != storedName) prefs.edit().putString("peer_name", name).apply()
        val secretLoad = TransferSecretStore.load(applicationContext)
        val storedSecret = secretLoad.value
        val encryptionRequested = prefs.getBoolean(
            "encryption_enabled", storedSecret.isNotEmpty())
        val encryptionEnabled = encryptionRequested && storedSecret.isNotEmpty()
        if (encryptionRequested && !encryptionEnabled) {
            prefs.edit().putBoolean("encryption_enabled", false).apply()
        }
        val secret = if (encryptionEnabled) storedSecret else ""
        val trustedOnly = prefs.getBoolean("trusted_only", false)
        val listenPort = prefs.getInt("listen_port", 0)
        val inboxRoot = getExternalFilesDir(null) ?: filesDir
        val inbox = File(inboxRoot, "收件箱")

        val node = InkHoleNode(applicationContext, name, inbox, secret, trustedOnly, listenPort,
                               listener = forwarder)
        // 设置变更重建时恢复选中目标：对端被重新发现后智能保留会自动选回
        InkHoleBus.pendingSelectedService?.let {
            node.restoreSelectedService(it)
            InkHoleBus.pendingSelectedService = null
        }
        InkHoleBus.node = node
        node.start()
        recoverPendingExports(node)
        if (secretLoad.warning.isNotEmpty()) forwarder.onStatus(secretLoad.warning)
        if (node.getActualPort() > 0) {
            try {
                TransportManager.listener = transportForwarder
                TransportManager.attach(
                    applicationContext, node, name, node.getInstanceId())
            } catch (error: Exception) {
                forwarder.onStatus("跨网核心启动失败: ${error.message}")
            }
        }
    }

    override fun onDestroy() {
        try { wifiLock?.release() } catch (_: Exception) {}
        wifiLock = null
        TransportManager.detach()
        TransportManager.listener = null
        InkHoleBus.node?.stop()
        InkHoleBus.node = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ---- 事件转发：更新 Bus 缓存 + 通知 + 转给 Activity(在时) ----

    private val forwarder = object : InkHoleListener {
        override fun onPeerChanged(peers: List<Peer>) {
            InkHoleBus.lastPeers = peers
            updateStatusNotification(
                if (peers.isEmpty()) "搜索设备中…" else "发现 ${peers.size} 台设备")
            InkHoleBus.uiListener?.onPeerChanged(peers)
        }

        override fun onFileReceived(filename: String, path: String, transferId: String) {
            // WHPP ACK is emitted after the private atomic commit. Public MediaStore
            // export can take minutes for a large folder and must not occupy the
            // receiver connection or turn a durable receive into a sender timeout.
            Thread({
                exportAndRecord(filename, path, transferId)
            }, "inkhole-public-export").apply { isDaemon = true }.start()
        }

        override fun onStatus(msg: String) {
            InkHoleBus.lastStatus = msg
            updateStatusNotification(msg)
            InkHoleBus.uiListener?.onStatus(msg)
        }

        override fun onProgress(kind: String, filename: String, done: Long, total: Long) {
            InkHoleBus.uiListener?.onProgress(kind, filename, done, total)
        }

        override fun onTransferEnded(kind: String, filename: String, completed: Boolean) {
            InkHoleBus.uiListener?.onTransferEnded(kind, filename, completed)
        }
    }

    private fun recoverPendingExports(node: InkHoleNode) {
        val pending = node.pendingCompletedTransfers()
        if (pending.isEmpty()) return
        Thread({
            pending.forEach { transfer ->
                exportAndRecord(transfer.filename, transfer.path, transfer.transferId)
            }
        }, "inkhole-export-recovery").apply { isDaemon = true }.start()
    }

    private fun exportAndRecord(filename: String, path: String, transferId: String) {
        val source = File(path)
        val record = synchronized(exportLock) {
            if (!source.exists()) return
            exportToDownloads(source).copy(transferId = transferId)
        }
        InkHoleBus.recordReceived(this, record)
        notifyFileReceived(record)
        InkHoleBus.uiListener?.onFileReceived(filename, path, transferId)
    }

    private val transportForwarder = TransportEventListener { event, data ->
        val message = when (event) {
            "ssh.ready" -> "SSH 中继已连接"
            "ssh.disconnected" -> "SSH 中继已断开，正在重连"
            "ssh.connected" -> "SSH 中继已恢复"
            "ssh.config.error", "ssh.pair.error", "core.error" ->
                data.optString("error", "跨网操作失败")
            "wormhole.error" -> "一次性短码失败: ${data.optString("error", "连接已结束")}"
            else -> ""
        }
        if (message.isNotEmpty()) forwarder.onStatus(message)
        InkHoleBus.dispatchTransportEvent(event, data)
    }

    // ---- 收件箱导出：应用私有目录 -> 系统 Downloads/InkHole ----
    // 私有目录(Android/data/…)用户在文件管理器里根本找不到(Android 11+ 甚至无法访问)。

    private fun exportToDownloads(src: File): ReceivedFile {
        if (src.isDirectory) return exportFolderToDownloads(src)
        val mime = guessMime(src.name)
        val size = src.length()
        val now = System.currentTimeMillis()
        val categoryDirectory = publicCategoryDirectory(src)
        if (Build.VERSION.SDK_INT >= 29) {
            var inserted: Uri? = null
            try {
                val values = ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, src.name)
                    put(MediaStore.MediaColumns.MIME_TYPE, mime)
                    put(MediaStore.MediaColumns.RELATIVE_PATH,
                        publicInboxRelativePath(categoryDirectory))
                    put(MediaStore.MediaColumns.IS_PENDING, 1)
                }
                inserted = contentResolver.insert(
                    MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                val uri = inserted ?: throw IOException("无法创建下载文件")
                val out = contentResolver.openOutputStream(uri)
                    ?: throw IOException("无法打开下载文件")
                // 1MB 缓冲:GB 级文件默认 8KB 会产生几十万次读写调用,
                // 导出耗时数倍于必要值,用户看到"进度走完了还在转"
                out.use { output ->
                    src.inputStream().use { it.copyTo(output, EXPORT_BUFFER) }
                }
                val published = contentResolver.update(uri, ContentValues().apply {
                    put(MediaStore.MediaColumns.IS_PENDING, 0)
                }, null, null)
                if (published <= 0) throw IOException("无法发布下载文件")
                val displayName = try {
                    contentResolver.query(
                        uri, arrayOf(MediaStore.MediaColumns.DISPLAY_NAME), null, null, null
                    )?.use { cursor ->
                        if (cursor.moveToFirst()) cursor.getString(0) else null
                    } ?: src.name
                } catch (_: Exception) {
                    src.name
                }
                src.delete()
                return ReceivedFile(displayName, uri, mime, size, now)
            } catch (_: Exception) {
                inserted?.let { uri ->
                    try { contentResolver.delete(uri, null, null) } catch (_: Exception) {}
                }
            }
        } else if (checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
            == PackageManager.PERMISSION_GRANTED) {
            try {
                val dir = File(Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS),
                    publicInboxRelativePath(categoryDirectory)
                        .removePrefix(Environment.DIRECTORY_DOWNLOADS + "/"))
                if (!dir.exists() && !dir.mkdirs()) throw IOException("无法创建下载目录")
                val dst = synchronized(exportLock) {
                    val candidate = ReceiveFiles.uniqueFile(dir, src.name)
                    src.copyTo(candidate, overwrite = false, bufferSize = EXPORT_BUFFER)
                    candidate
                }
                val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", dst)
                src.delete()
                return ReceivedFile(dst.name, uri, mime, size, now)
            } catch (_: Exception) {
                // 导出失败不丢文件：留在私有收件箱，仍可从 App 内打开
            }
        }
        val uri = try {
            FileProvider.getUriForFile(this, "$packageName.fileprovider", src)
        } catch (_: Exception) { null }
        return ReceivedFile(src.name, uri, mime, size, now)
    }

    private fun exportFolderToDownloads(src: File): ReceivedFile = synchronized(exportLock) {
        val mime = "inode/directory"
        val now = System.currentTimeMillis()
        val categoryDirectory = publicCategoryDirectory(src)
        val files = src.walkTopDown().filter { it.isFile }
            .sortedBy { it.relativeTo(src).invariantSeparatorsPath }
            .toList()
        val totalSize = files.fold(0L) { total, file ->
            if (Long.MAX_VALUE - total < file.length()) Long.MAX_VALUE else total + file.length()
        }

        if (Build.VERSION.SDK_INT >= 29) {
            // Scoped Storage cannot create a durable row for an empty directory.
            // Empty-only trees are intentionally ignored; non-empty trees retain
            // every file's relative parent under one unique public root.
            if (files.isEmpty()) {
                src.deleteRecursively()
                return@synchronized ReceivedFile(src.name, null, mime, 0, now)
            }
            val publicRoot = uniqueMediaStoreFolderName(src.name, categoryDirectory)
            val inserted = ArrayList<Uri>(files.size)
            try {
                for (file in files) {
                    val relative = file.relativeTo(src).invariantSeparatorsPath
                    val parent = relative.substringBeforeLast('/', "")
                    val relativePath = buildString {
                        append(publicInboxRelativePath(categoryDirectory))
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
                    val uri = contentResolver.insert(
                        MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                        ?: throw IOException("无法创建下载文件")
                    inserted += uri
                    val output = contentResolver.openOutputStream(uri)
                        ?: throw IOException("无法打开下载文件")
                    output.use { out ->
                        file.inputStream().use { it.copyTo(out, EXPORT_BUFFER) }
                    }
                }
                for (uri in inserted) {
                    val published = contentResolver.update(uri, ContentValues().apply {
                        put(MediaStore.MediaColumns.IS_PENDING, 0)
                    }, null, null)
                    if (published <= 0) throw IOException("无法发布下载文件夹")
                }
                src.deleteRecursively()
                return@synchronized ReceivedFile(publicRoot, null, mime, totalSize, now)
            } catch (_: Exception) {
                inserted.forEach { uri ->
                    try { contentResolver.delete(uri, null, null) } catch (_: Exception) {}
                }
                // Keep the complete private folder when public export fails.
                return@synchronized ReceivedFile(src.name, null, mime, totalSize, now)
            }
        }

        if (checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
            == PackageManager.PERMISSION_GRANTED) {
            var destination: File? = null
            try {
                val root = File(Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS),
                    publicInboxRelativePath(categoryDirectory)
                        .removePrefix(Environment.DIRECTORY_DOWNLOADS + "/"))
                if (!root.isDirectory && !root.mkdirs()) throw IOException("无法创建下载目录")
                destination = ReceiveFiles.uniqueDirectory(root, src.name)
                if (!src.copyRecursively(destination, overwrite = false)) {
                    throw IOException("无法复制下载文件夹")
                }
                src.deleteRecursively()
                return@synchronized ReceivedFile(destination.name, null, mime, totalSize, now)
            } catch (_: Exception) {
                destination?.deleteRecursively()
            }
        }
        ReceivedFile(src.name, null, mime, totalSize, now)
    }

    private fun uniqueMediaStoreFolderName(name: String, categoryDirectory: String): String {
        var candidate = name
        var suffix = 2
        while (mediaStoreFolderExists(candidate, categoryDirectory)) {
            candidate = "$name ($suffix)"
            suffix++
        }
        return candidate
    }

    private fun mediaStoreFolderExists(name: String, categoryDirectory: String): Boolean {
        if (Build.VERSION.SDK_INT < 29) return false
        val prefix = "${publicInboxRelativePath(categoryDirectory)}/$name/"
        return try {
            contentResolver.query(
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
        return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: "application/octet-stream"
    }

    private fun publicCategoryDirectory(src: File): String {
        val prefs = getSharedPreferences("inkhole", Context.MODE_PRIVATE)
        if (!prefs.getBoolean(InboxClassification.PREF_ENABLED, false)) return ""
        val category = InboxClassification.categoryFor(src.name, src.isDirectory)
        return InboxClassification.directoryName(
            category,
            prefs.getString(InboxClassification.preferenceKey(category), ""),
        )
    }

    private fun publicInboxRelativePath(categoryDirectory: String): String = buildString {
        append(Environment.DIRECTORY_DOWNLOADS)
        append("/InkHole")
        if (categoryDirectory.isNotEmpty()) {
            append('/')
            append(categoryDirectory)
        }
    }

    // ---- 通知 ----

    private fun createChannels() {
        if (Build.VERSION.SDK_INT < 26) return
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL_STATUS, "墨洞运行状态", NotificationManager.IMPORTANCE_MIN))
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL_FILES, "收到文件", NotificationManager.IMPORTANCE_DEFAULT))
    }

    @Suppress("DEPRECATION")
    private fun buildStatusNotification(text: String): Notification {
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val builder = if (Build.VERSION.SDK_INT >= 26)
            Notification.Builder(this, CHANNEL_STATUS) else Notification.Builder(this)
        return builder
            .setSmallIcon(R.drawable.ic_notification)
            .setColor(0xFF58E6C8.toInt())
            .setContentTitle("墨洞")
            .setContentText(text)
            .setContentIntent(openApp)
            .setOngoing(true)
            .build()
    }

    private fun updateStatusNotification(text: String) {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        try {
            nm.notify(NOTIF_STATUS_ID, buildStatusNotification(text))
        } catch (_: Exception) {}
    }

    @Suppress("DEPRECATION")
    private fun notifyFileReceived(record: ReceivedFile) {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) return

        val builder = if (Build.VERSION.SDK_INT >= 26)
            Notification.Builder(this, CHANNEL_FILES) else Notification.Builder(this)
        builder.setSmallIcon(R.drawable.ic_notification)
            .setColor(0xFF58E6C8.toInt())
            .setContentTitle(if (record.mime == "inode/directory")
                "墨洞已接收文件夹" else "墨洞已接收文件")
            .setContentText(record.name)
            .setAutoCancel(true)
        if (record.uri != null) {
            val view = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(record.uri, record.mime)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            builder.setContentIntent(PendingIntent.getActivity(
                this, record.uri.hashCode(), view,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE))
        } else if (record.mime == "inode/directory" && record.size > 0) {
            val downloads = Intent(android.app.DownloadManager.ACTION_VIEW_DOWNLOADS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            builder.setContentIntent(PendingIntent.getActivity(
                this, record.name.hashCode(), downloads,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE))
        }
        try {
            nm.notify(fileNotifId.getAndIncrement(), builder.build())
        } catch (_: Exception) {}
    }
}
