package com.rexvane.wormhole

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
import com.rexvane.wormhole.p2p.Peer
import com.rexvane.wormhole.p2p.WormholeListener
import com.rexvane.wormhole.p2p.WormholeNode
import java.io.File

/**
 * 前台服务：拥有 P2P 节点的生命周期。
 *
 * 之前节点绑在 Activity 上——锁屏/切后台 Activity 被杀就断线，转屏还会
 * 重启节点(端口变、设备列表清空、传输中断)。挪进前台服务后手机侧真正
 * "常驻可收"。收到的文件导出到系统 Downloads/InkHole(用户找得到)并发通知。
 */
class WormholeService : Service() {

    companion object {
        private const val CHANNEL_STATUS = "wormhole_status"
        private const val CHANNEL_FILES = "wormhole_files"
        private const val NOTIF_STATUS_ID = 1
        private const val ACTION_RELOAD = "com.rexvane.wormhole.RELOAD"

        fun start(context: Context) {
            val intent = Intent(context, WormholeService::class.java)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
            else context.startService(intent)
        }

        /** 设置(名字/口令)变更后重建节点。
         * 不能用 stopService+startService：stop 是异步的，服务没销毁完时
         * start 只触发 onStartCommand 不触发 onCreate，节点不会重建。 */
        fun restart(context: Context) {
            val intent = Intent(context, WormholeService::class.java).setAction(ACTION_RELOAD)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent)
            else context.startService(intent)
        }
    }

    private var fileNotifId = 100

    override fun onCreate() {
        super.onCreate()
        createChannels()
        startForeground(NOTIF_STATUS_ID, buildStatusNotification("正在启动…"))
        WormholeBus.loadHistory(this)
        startNode()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_RELOAD) {
            WormholeBus.node?.stop()
            WormholeBus.node = null
            WormholeBus.lastPeers = emptyList()
            startNode()
        }
        return START_STICKY
    }

    private fun startNode() {
        val prefs = getSharedPreferences("wormhole", Context.MODE_PRIVATE)
        val name = prefs.getString("peer_name", Build.MODEL) ?: Build.MODEL
        val secret = prefs.getString("secret", "") ?: ""
        val trustedOnly = prefs.getBoolean("trusted_only", false)
        val inbox = File(getExternalFilesDir(null), "收件箱")

        val node = WormholeNode(this, name, inbox, secret, trustedOnly, listener = forwarder)
        WormholeBus.node = node
        node.start()
    }

    override fun onDestroy() {
        WormholeBus.node?.stop()
        WormholeBus.node = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ---- 事件转发：更新 Bus 缓存 + 通知 + 转给 Activity(在时) ----

    private val forwarder = object : WormholeListener {
        override fun onPeerChanged(peers: List<Peer>) {
            WormholeBus.lastPeers = peers
            updateStatusNotification(
                if (peers.isEmpty()) "搜索设备中…" else "发现 ${peers.size} 台设备")
            WormholeBus.uiListener?.onPeerChanged(peers)
        }

        override fun onFileReceived(filename: String, path: String) {
            val record = exportToDownloads(File(path))
            WormholeBus.receivedFiles.add(0, record)
            WormholeBus.saveHistory(this@WormholeService)
            notifyFileReceived(record)
            WormholeBus.uiListener?.onFileReceived(filename, path)
        }

        override fun onStatus(msg: String) {
            WormholeBus.lastStatus = msg
            WormholeBus.uiListener?.onStatus(msg)
        }

        override fun onProgress(kind: String, filename: String, done: Long, total: Long) {
            WormholeBus.uiListener?.onProgress(kind, filename, done, total)
        }
    }

    // ---- 收件箱导出：应用私有目录 -> 系统 Downloads/InkHole ----
    // 私有目录(Android/data/…)用户在文件管理器里根本找不到(Android 11+ 甚至无法访问)。

    private fun exportToDownloads(src: File): ReceivedFile {
        val mime = guessMime(src.name)
        val size = src.length()
        val now = System.currentTimeMillis()
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                val values = ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, src.name)
                    put(MediaStore.MediaColumns.MIME_TYPE, mime)
                    put(MediaStore.MediaColumns.RELATIVE_PATH,
                        Environment.DIRECTORY_DOWNLOADS + "/InkHole")
                }
                val uri = contentResolver.insert(
                    MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                if (uri != null) {
                    contentResolver.openOutputStream(uri)?.use { out ->
                        src.inputStream().use { it.copyTo(out) }
                    }
                    src.delete()
                    return ReceivedFile(src.name, uri, mime, size, now)
                }
            } else if (checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                == PackageManager.PERMISSION_GRANTED) {
                val dir = File(Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS), "InkHole")
                dir.mkdirs()
                val dst = File(dir, src.name)
                src.copyTo(dst, overwrite = true)
                src.delete()
                val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", dst)
                return ReceivedFile(dst.name, uri, mime, size, now)
            }
        } catch (_: Exception) {
            // 导出失败不丢文件：留在私有收件箱，仍可从 App 内打开
        }
        val uri = try {
            FileProvider.getUriForFile(this, "$packageName.fileprovider", src)
        } catch (_: Exception) { null }
        return ReceivedFile(src.name, uri, mime, size, now)
    }

    private fun guessMime(name: String): String {
        val ext = name.substringAfterLast('.', "").lowercase()
        return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: "application/octet-stream"
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

    private fun buildStatusNotification(text: String): Notification {
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val builder = if (Build.VERSION.SDK_INT >= 26)
            Notification.Builder(this, CHANNEL_STATUS) else Notification.Builder(this)
        return builder
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
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

    private fun notifyFileReceived(record: ReceivedFile) {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) return

        val builder = if (Build.VERSION.SDK_INT >= 26)
            Notification.Builder(this, CHANNEL_FILES) else Notification.Builder(this)
        builder.setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("墨洞吐出文件")
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
        }
        try {
            nm.notify(fileNotifId++, builder.build())
        } catch (_: Exception) {}
    }
}
