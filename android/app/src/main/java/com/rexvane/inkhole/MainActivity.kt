package com.rexvane.inkhole

import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.rexvane.inkhole.p2p.Peer
import com.rexvane.inkhole.p2p.InkHoleListener
import com.rexvane.inkhole.p2p.ManualPeer
import com.rexvane.inkhole.p2p.ManualPeers
import com.rexvane.inkhole.p2p.ReceiveFiles
import com.rexvane.inkhole.p2p.WHPP
import java.io.File
import java.io.IOException
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 主界面(纯 UI)。P2P 节点的生命周期在 InkHoleService(前台服务)：
 * 转屏/锁屏/切后台都不断线，Activity 只是挂上去看状态、发起发送。
 *
 * 发送入口：
 *   1. 轻点墨洞 -> 系统文件选择器(可多选)
 *   2. 系统分享菜单(图库/文件管理器 -> 分享 -> 墨洞)，未选设备时先排队
 */
class MainActivity : ComponentActivity() {

    companion object {
        private const val PREF_USAGE_GUIDE_SEEN = "usage_guide_seen"
        private const val STATE_PENDING_SHARES = "pending_shares"
        // Activity 横竖屏重建时发送线程仍在工作；队列和取消标记必须跨实例共享。
        private val sendQueue = ConcurrentLinkedQueue<Uri>()
        private val sendWorkerRunning = AtomicBoolean(false)
        private val sendCancelled = AtomicBoolean(false)
    }

    // UI 状态
    private val peers = mutableStateListOf<Peer>()
    private val selectedPeer = mutableStateOf<String?>(null)
    private val statusMsg = mutableStateOf("正在启动…")
    private val receivedFiles = mutableStateListOf<ReceivedFile>()
    private val pendingShares = mutableStateListOf<Uri>()   // 分享进来、等选设备的文件
    private val transferPct = mutableFloatStateOf(-1f)      // -1=空闲
    private val sendingActive = mutableStateOf(false)
    private var activeProgressKey: String? = null
    private var progressSampleTime = 0L
    private var progressSampleDone = 0L
    private var progressSpeed = 0.0

    // 设置
    private val peerName = mutableStateOf(Build.MODEL)
    private val secret = mutableStateOf("")
    private val trustedOnly = mutableStateOf(false)
    private var showSettings = mutableStateOf(false)
    private val showUsageGuide = mutableStateOf(false)

    // 应用内更新
    private val updateInfo = mutableStateOf<Updater.Info?>(null)
    private val checkingUpdate = mutableStateOf(false)

    private fun currentVersion(): String = try {
        packageManager.getPackageInfo(packageName, 0).versionName ?: "?"
    } catch (_: Exception) { "?" }

    private fun checkUpdate() {
        if (checkingUpdate.value) return
        checkingUpdate.value = true
        Thread {
            try {
                val info = Updater.fetchLatest()
                runOnUiThread {
                    checkingUpdate.value = false
                    if (Updater.versionNewer(info.version, currentVersion())) {
                        updateInfo.value = info
                    } else {
                        // 检查更新是在设置对话框里点的,statusMsg(主页状态栏)被
                        // 对话框盖住看不到 -> 用 Toast 明确回馈"已是最新版"。
                        Toast.makeText(this,
                            "已是最新版本 v${currentVersion()}",
                            Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    checkingUpdate.value = false
                    Toast.makeText(this,
                        "检查更新失败: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    private fun downloadAndInstall(info: Updater.Info) {
        Thread {
            try {
                runOnUiThread { statusMsg.value = "正在下载新版本…" }
                val apk = Updater.downloadApk(this, info.apkUrl) { p ->
                    runOnUiThread { statusMsg.value = "正在下载新版本… $p%" }
                }
                runOnUiThread {
                    try {
                        statusMsg.value = "下载完成，请在安装器中确认"
                        Updater.installApk(this, apk)
                    } catch (e: Exception) {
                        statusMsg.value = "无法打开安装器: ${e.message}"
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { statusMsg.value = "更新下载失败: ${e.message}" }
            }
        }.start()
    }

    @androidx.compose.runtime.Composable
    private fun UpdateDialog() {
        val info = updateInfo.value ?: return
        AlertDialog(
            onDismissRequest = { updateInfo.value = null },
            title = { Text("发现新版本") },
            text = {
                Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                    Text("当前版本：v${currentVersion()}", fontSize = 12.sp,
                        color = androidx.compose.ui.graphics.Color.Gray)
                    Text("最新版本：${info.version}", fontSize = 12.sp,
                        color = androidx.compose.ui.graphics.Color.Gray)
                    Text(
                        text = if (info.apkUrl.isNotEmpty()) "更新状态：可直接更新"
                            else "更新状态：可前往发布页下载",
                        fontSize = 12.sp,
                        color = androidx.compose.ui.graphics.Color(0xFF58CDB5),
                    )
                    Spacer(Modifier.height(12.dp))
                    Text("本次更新", fontSize = 13.sp,
                        fontWeight = androidx.compose.ui.text.font.FontWeight.SemiBold)
                    Spacer(Modifier.height(4.dp))
                    Text(info.notes.ifEmpty { "发布说明暂不可用" }, fontSize = 12.sp)
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val i = info
                    updateInfo.value = null
                    if (i.apkUrl.isNotEmpty()) downloadAndInstall(i)
                    else startActivity(Intent(Intent.ACTION_VIEW,
                        Uri.parse(Updater.RELEASES_PAGE)))
                }) { Text(if (info.apkUrl.isNotEmpty()) "立即更新" else "查看发布页") }
            },
            dismissButton = {
                TextButton(onClick = { updateInfo.value = null }) { Text("取消") }
            },
        )
    }

    private fun closeUsageGuide() {
        getSharedPreferences("inkhole", Context.MODE_PRIVATE).edit()
            .putBoolean(PREF_USAGE_GUIDE_SEEN, true)
            .apply()
        showUsageGuide.value = false
    }

    @androidx.compose.runtime.Composable
    private fun GuideSection(title: String, body: String) {
        Text(title, fontSize = 14.sp,
            fontWeight = androidx.compose.ui.text.font.FontWeight.SemiBold)
        Spacer(Modifier.height(4.dp))
        Text(body, fontSize = 12.sp,
            color = androidx.compose.ui.graphics.Color.Gray)
        Spacer(Modifier.height(12.dp))
    }

    @androidx.compose.runtime.Composable
    private fun UsageGuideDialog() {
        if (!showUsageGuide.value) return
        AlertDialog(
            onDismissRequest = { closeUsageGuide() },
            title = { Text("使用说明") },
            text = {
                Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                    GuideSection("局域网",
                        "两台设备连接同一个 WiFi，并同时打开墨洞。发现设备后，" +
                            "点击对方设备，再点击墨洞图标选择文件发送。")
                    GuideSection("跨网络",
                        "两台设备登录同一个 Tailscale 网络。在设置中配置固定监听端口，" +
                            "然后填写对方的 Tailscale IP 和监听端口添加设备。" +
                            "添加后，发送方式与局域网相同。")
                    Text("收到的文件保存在 Download/InkHole，也可以在首页「已接收」中查看。",
                        fontSize = 12.sp,
                        color = androidx.compose.ui.graphics.Color.Gray)
                }
            },
            confirmButton = {
                TextButton(onClick = { closeUsageGuide() }) { Text("知道了") }
            },
        )
    }

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()) { /* 拒绝也能用，只是没通知/旧机型不导出 */ }

    private val filePicker = registerForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments()
    ) { uris ->
        if (!uris.isNullOrEmpty()) sendUris(uris)
    }

    private val uiListener = object : InkHoleListener {
        override fun onPeerChanged(peers: List<Peer>) = runOnUiThread {
            this@MainActivity.peers.clear()
            this@MainActivity.peers.addAll(peers)
            if (selectedPeer.value != null && peers.none { it.name == selectedPeer.value }) {
                selectedPeer.value = null   // 选中的设备离线了
            }
        }
        override fun onFileReceived(filename: String, path: String) = runOnUiThread {
            refreshReceived()
        }
        override fun onStatus(msg: String) = runOnUiThread { statusMsg.value = msg }
        override fun onProgress(kind: String, filename: String, done: Long, total: Long) =
            runOnUiThread {
                val key = "$kind|$filename"
                val now = System.currentTimeMillis()
                if (activeProgressKey != key || done < progressSampleDone) {
                    activeProgressKey = key
                    progressSampleTime = now
                    progressSampleDone = done
                    progressSpeed = 0.0
                } else {
                    val elapsed = now - progressSampleTime
                    if (elapsed > 0) {
                        val instant = (done - progressSampleDone) * 1000.0 / elapsed
                        progressSpeed = if (progressSpeed <= 0) instant
                            else progressSpeed * 0.65 + instant * 0.35
                        progressSampleTime = now
                        progressSampleDone = done
                    }
                }
                val pct = if (total > 0) (done * 100f / total) else 100f
                transferPct.floatValue = if (pct >= 100f) -1f else pct
                val speedText = when {
                    progressSpeed >= 1024 * 1024 ->
                        " · %.1f MB/s".format(progressSpeed / 1024 / 1024)
                    progressSpeed >= 1024 -> " · %.0f KB/s".format(progressSpeed / 1024)
                    else -> ""
                }
                // 百分比与速度放最前:状态栏空间不够时截断的是文件名,
                // 实时速度永远可见
                statusMsg.value =
                    "${if (kind == "send") "↑ 发送" else "↓ 接收"} ${pct.toInt()}%$speedText · $filename"
            }

        override fun onTransferEnded(kind: String, filename: String, completed: Boolean) =
            runOnUiThread {
                if (activeProgressKey == "$kind|$filename") {
                    activeProgressKey = null
                    progressSampleTime = 0L
                    progressSampleDone = 0L
                    progressSpeed = 0.0
                    transferPct.floatValue = -1f
                }
            }

        override fun onSendingChanged(active: Boolean) = runOnUiThread {
            sendingActive.value = active
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val prefs = getSharedPreferences("inkhole", Context.MODE_PRIVATE)
        peerName.value = (prefs.getString("peer_name", Build.MODEL) ?: Build.MODEL)
            .filterNot { it.isISOControl() }.trim().take(40).ifEmpty { Build.MODEL }
        secret.value = prefs.getString("secret", "") ?: ""
        trustedOnly.value = prefs.getBoolean("trusted_only", false)
        showUsageGuide.value = !prefs.getBoolean(PREF_USAGE_GUIDE_SEEN, false)

        requestNeededPermissions()
        InkHoleService.start(this)
        InkHoleBus.loadHistory(this)
        InkHoleBus.uiListener = uiListener

        // 服务可能早已在跑(锁屏收文件后点开)：恢复最近状态。节点活着时以它的
        // 实时列表为准——lastPeers 可能属于已被系统杀死的旧节点,是陈旧状态。
        peers.addAll(InkHoleBus.node?.getPeers() ?: InkHoleBus.lastPeers)
        statusMsg.value = InkHoleBus.lastStatus
        selectedPeer.value = InkHoleBus.node?.getSelectedPeer()
        sendingActive.value = sendWorkerRunning.get()
        refreshReceived()

        if (savedInstanceState == null) {
            handleShareIntent(intent)
        } else {
            savedInstanceState.getStringArrayList(STATE_PENDING_SHARES)
                ?.mapTo(pendingShares, Uri::parse)
        }

        setContent {
            InkHoleTheme {
                MainScreen(
                    statusMsg = statusMsg.value,
                    peers = peers,
                    selectedPeer = selectedPeer.value,
                    receivedFiles = receivedFiles,
                    transferPct = transferPct.floatValue,
                    sending = sendingActive.value,
                    pendingCount = pendingShares.size,
                    onDeviceClick = { peer -> onDeviceClicked(peer) },
                    onSendClick = {
                        if (selectedPeer.value == null) {
                            statusMsg.value = if (peers.isEmpty())
                                "还没发现设备" else "先点选一台目标设备"
                        } else {
                            filePicker.launch(arrayOf("*/*"))
                        }
                    },
                    onCancelSend = { cancelSending() },
                    onOpenInbox = { openInbox() },
                    onOpenFile = { rec -> openFile(rec) },
                    onClearHistory = {
                        InkHoleBus.clearHistory(this)
                        refreshReceived()
                    },
                    onRefreshClick = {
                        statusMsg.value = "正在重新搜索设备…"
                        InkHoleBus.node?.restartDiscovery()
                    },
                    onSettingsClick = { showSettings.value = true },
                )
                SettingsDialog()
                UpdateDialog()
                UsageGuideDialog()
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShareIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        // 回前台先与节点全量对表,再立即触发一轮探活:息屏期间探活循环随
        // 进程冻结,下线设备的剔除毫无进展,不踢一脚就得干等下个轮询周期,
        // 用户看到的是"设备早就关了还显示在线"。
        InkHoleBus.node?.let { node ->
            peers.clear()
            peers.addAll(node.getPeers())
            selectedPeer.value = node.getSelectedPeer()
            node.probeNow()
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putStringArrayList(
            STATE_PENDING_SHARES,
            ArrayList(pendingShares.map(Uri::toString)),
        )
        super.onSaveInstanceState(outState)
    }

    override fun onDestroy() {
        // 只摘掉 UI 监听；节点在前台服务里继续跑
        if (InkHoleBus.uiListener === uiListener) InkHoleBus.uiListener = null
        super.onDestroy()
    }

    // ---- 发送 ----

    private fun onDeviceClicked(peer: Peer) {
        val newSel = if (selectedPeer.value == peer.name) null else peer.name
        selectedPeer.value = newSel
        InkHoleBus.node?.selectPeer(newSel)
        if (newSel != null && pendingShares.isNotEmpty()) {
            val toSend = pendingShares.toList()
            pendingShares.clear()
            sendUris(toSend)   // 分享排队的文件，选中设备后立刻发出
        }
    }

    private fun handleShareIntent(intent: Intent?) {
        val uris = intent?.streamUris().orEmpty()
        if (uris.isEmpty()) return
        intent?.action = null
        intent?.removeExtra(Intent.EXTRA_STREAM)
        if (selectedPeer.value != null) {
            sendUris(uris)
        } else {
            pendingShares.addAll(uris)
            statusMsg.value = "收到 ${pendingShares.size} 个待发送文件"
        }
    }

    private fun Intent.streamUris(): List<Uri> = try {
        if (action != Intent.ACTION_SEND && action != Intent.ACTION_SEND_MULTIPLE) {
            emptyList()
        } else if (Build.VERSION.SDK_INT >= 33) {
            when (action) {
                Intent.ACTION_SEND ->
                    listOfNotNull(getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java))
                else ->
                    getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java).orEmpty()
            }
        } else {
            @Suppress("DEPRECATION")
            when (action) {
                Intent.ACTION_SEND ->
                    listOfNotNull(getParcelableExtra(Intent.EXTRA_STREAM) as? Uri)
                else ->
                    getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM).orEmpty()
            }
        }
    } catch (_: Exception) {
        emptyList()
    }

    /** URI 进入单工作线程队列；已知大小时直接流式发送，不再复制整份大文件。 */
    private fun sendUris(uris: List<Uri>) {
        if (InkHoleBus.node == null) {
            statusMsg.value = "墨洞未就绪"
            return
        }
        if (!sendWorkerRunning.get()) sendCancelled.set(false)
        uris.forEach(sendQueue::add)
        startSendWorker()
    }

    private fun startSendWorker() {
        if (!sendWorkerRunning.compareAndSet(false, true)) return
        InkHoleBus.uiListener?.onSendingChanged(true)
        Thread {
            var okCount = 0
            var total = 0
            var wasCancelled = false
            try {
                while (!sendCancelled.get()) {
                    val uri = sendQueue.poll() ?: break
                    total++
                    if (sendOneUri(uri)) okCount++
                }
                wasCancelled = sendCancelled.get()
            } finally {
                if (wasCancelled) sendQueue.clear()
                sendWorkerRunning.set(false)
                if (!wasCancelled && sendQueue.isNotEmpty()) {
                    startSendWorker()  // 处理 poll 空与新入队撞车的窄窗口
                } else {
                    sendCancelled.set(false)
                    InkHoleBus.uiListener?.onSendingChanged(false)
                    when {
                        wasCancelled -> InkHoleBus.uiListener?.onStatus("已取消发送")
                        total > 1 -> InkHoleBus.uiListener?.onStatus(
                            "已发送 $okCount/$total 个文件")
                    }
                }
            }
        }.start()
    }

    private fun sendOneUri(uri: Uri): Boolean {
        val node = InkHoleBus.node ?: return false
        var tmp: File? = null
        return try {
            val name = ReceiveFiles.safeName(
                queryDisplayName(uri) ?: "file_${System.currentTimeMillis()}")
            val size = queryContentSize(uri)
            InkHoleBus.uiListener?.onStatus("发送：$name")
            if (size != null) {
                node.sendStream(size, name, shouldCancel = { sendCancelled.get() }) {
                    contentResolver.openInputStream(uri)
                        ?: throw IOException("无法读取文件")
                }
            } else {
                // 极少数文档提供方不报告大小；此时才回退缓存文件以确定协议 size。
                val dst = File.createTempFile("inkhole-send-", ".tmp", cacheDir)
                tmp = dst
                contentResolver.openInputStream(uri)?.use { input ->
                    dst.outputStream().buffered(WHPP.BUFFER_SIZE).use { output ->
                        val buffer = ByteArray(WHPP.BUFFER_SIZE)
                        while (true) {
                            if (sendCancelled.get()) throw java.io.InterruptedIOException()
                            val read = input.read(buffer)
                            if (read < 0) break
                            output.write(buffer, 0, read)
                        }
                    }
                } ?: throw IOException("无法读取文件")
                if (sendCancelled.get()) false else node.sendStream(
                    dst.length(), name, shouldCancel = { sendCancelled.get() }) {
                    dst.inputStream()
                }
            }
        } catch (e: Exception) {
            if (!sendCancelled.get()) {
                InkHoleBus.uiListener?.onStatus("发送失败: ${e.message}")
            }
            false
        } finally {
            tmp?.delete()
        }
    }

    private fun cancelSending() {
        if (!sendingActive.value && sendQueue.isEmpty() && InkHoleBus.node?.isSending() != true) return
        sendCancelled.set(true)
        sendQueue.clear()
        InkHoleBus.node?.cancelSend()
        statusMsg.value = "正在取消发送…"
    }

    private fun queryDisplayName(uri: Uri): String? =
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (idx >= 0 && c.moveToFirst()) c.getString(idx) else null
        }

    private fun queryContentSize(uri: Uri): Long? {
        val cursorSize = try {
            contentResolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { c ->
                val idx = c.getColumnIndex(OpenableColumns.SIZE)
                if (idx >= 0 && c.moveToFirst() && !c.isNull(idx)) c.getLong(idx) else null
            }
        } catch (_: Exception) { null }
        if (cursorSize != null && cursorSize >= 0) return cursorSize
        return try {
            contentResolver.openAssetFileDescriptor(uri, "r")?.use { afd ->
                afd.length.takeIf { it >= 0 }
            }
        } catch (_: Exception) { null }
    }

    // ---- 接收侧 UI ----

    private fun refreshReceived() {
        receivedFiles.clear()
        receivedFiles.addAll(InkHoleBus.receivedFiles)
    }

    private fun openFile(rec: ReceivedFile) {
        val uri = rec.uri ?: run {
            statusMsg.value = "文件在 Download/InkHole"
            return
        }
        try {
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, rec.mime)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            startActivity(Intent.createChooser(intent, "打开 ${rec.name}"))
        } catch (e: Exception) {
            statusMsg.value = "无法打开: ${e.message}"
        }
    }

    private fun openInbox() {
        // 收到的文件都导出在系统 Download/InkHole，打开系统下载页最直接
        try {
            startActivity(Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: Exception) {
            statusMsg.value = "收件箱: Download/InkHole"
        }
    }

    // ---- 权限 ----

    private fun requestNeededPermissions() {
        val perms = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            perms.add(android.Manifest.permission.POST_NOTIFICATIONS)
        }
        if (Build.VERSION.SDK_INT < 29 &&
            checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
            != PackageManager.PERMISSION_GRANTED) {
            perms.add(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
        }
        if (perms.isNotEmpty()) permissionLauncher.launch(perms.toTypedArray())
    }

    // ---- 设置 ----

    @Composable
    private fun SettingsDialog() {
        if (showSettings.value) {
            val prefs = getSharedPreferences("inkhole", Context.MODE_PRIVATE)
            var nameInput by remember { mutableStateOf(peerName.value) }
            var secretInput by remember { mutableStateOf(secret.value) }
            var trustedInput by remember { mutableStateOf(trustedOnly.value) }
            val originalPort = remember { prefs.getInt("listen_port", 0) }
            var portInput by remember {
                mutableStateOf(if (originalPort == 0) "" else originalPort.toString())
            }
            val manualList = remember {
                mutableStateListOf<ManualPeer>().apply { addAll(ManualPeers.load(prefs)) }
            }
            var manualName by remember { mutableStateOf("") }
            // 用 TextFieldValue 而非 String:自动补点改写文本后必须显式把光标放到
            // 末尾,否则 Compose 会把光标留在旧 offset(点号前),后续输入全插错位
            var manualHost by remember {
                mutableStateOf(androidx.compose.ui.text.input.TextFieldValue(""))
            }
            var manualPort by remember { mutableStateOf("") }
            var manualError by remember { mutableStateOf("") }
            var settingsError by remember { mutableStateOf("") }
            var editingPeer by remember { mutableStateOf<ManualPeer?>(null) }
            AlertDialog(
                onDismissRequest = { showSettings.value = false },
                title = { Text("设置") },
                text = {
                    Column(
                        modifier = Modifier.verticalScroll(rememberScrollState()),
                    ) {
                        // 本机信息
                        val instanceId = prefs.getString("instance_id", "") ?: ""
                        val actualPort = InkHoleBus.node?.getActualPort() ?: 0
                        androidx.compose.foundation.text.selection.SelectionContainer {
                            Column {
                                Text(
                                    text = "本机：${peerName.value}-$instanceId",
                                    fontSize = 12.sp,
                                    color = androidx.compose.ui.graphics.Color.Gray,
                                )
                                Text(
                                    text = "版本：v${currentVersion()}",
                                    fontSize = 12.sp,
                                    color = androidx.compose.ui.graphics.Color.Gray,
                                )
                                Text(
                                    text = "端口：${if (actualPort > 0) actualPort else "未启动"}" +
                                        "（建议自定义固定端口）",
                                    fontSize = 12.sp,
                                    color = androidx.compose.ui.graphics.Color.Gray,
                                    modifier = Modifier.padding(bottom = 12.dp)
                                )
                            }
                        }
                        Text("设备设置", fontSize = 14.sp,
                            fontWeight = androidx.compose.ui.text.font.FontWeight.SemiBold)
                        Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = nameInput,
                            onValueChange = { value ->
                                nameInput = value.filterNot { it.isISOControl() }.take(40)
                            },
                            label = { Text("设备名称") },
                            singleLine = true,
                        )

                        Spacer(Modifier.height(14.dp))
                        Text("传输安全", fontSize = 14.sp,
                            fontWeight = androidx.compose.ui.text.font.FontWeight.SemiBold)
                        Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = secretInput,
                            onValueChange = { secretInput = it },
                            label = { Text("加密口令 (可选，两端一致)") },
                            singleLine = true,
                        )
                        Spacer(Modifier.height(12.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text("仅接收目标设备", fontSize = 14.sp)
                                Text("只允许当前选中的设备向本机发送文件",
                                    fontSize = 11.sp,
                                    color = androidx.compose.ui.graphics.Color.Gray)
                            }
                            Spacer(Modifier.width(8.dp))
                            Switch(checked = trustedInput,
                                onCheckedChange = { trustedInput = it })
                        }

                        // ---- 跨网络配置(手动直连,如 Tailscale) ----
                        Spacer(Modifier.height(14.dp))
                        Text("跨网络配置", fontSize = 14.sp,
                            fontWeight = androidx.compose.ui.text.font.FontWeight.SemiBold)
                        Text("跨网直连时固定本机监听端口，并填写对方 Tailscale IP 与监听端口",
                            fontSize = 11.sp,
                            color = androidx.compose.ui.graphics.Color.Gray)
                        Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = portInput,
                            onValueChange = {
                                portInput = it.filter { c -> c.isDigit() }.take(5)
                                settingsError = ""
                            },
                            label = { Text("本机监听端口（留空=自动，如 52130）") },
                            singleLine = true,
                        )
                        if (settingsError.isNotEmpty()) {
                            Text(settingsError, fontSize = 11.sp,
                                color = androidx.compose.ui.graphics.Color(0xFFF08A7C))
                        }
                        Spacer(Modifier.height(8.dp))
                        OutlinedTextField(
                            value = manualName,
                            onValueChange = { manualName = it },
                            label = { Text("备注（如 我的电脑，可选）") },
                            singleLine = true,
                        )
                        Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = manualHost,
                            onValueChange = { tfv ->
                                // 光标在末尾才自动补点(不打扰中间编辑),并把光标固定回末尾——
                                // 否则 Compose 会把光标留在旧 offset,后续输入全插错位
                                manualHost = if (tfv.selection.end == tfv.text.length) {
                                    val masked = ManualPeers.maskTyping(tfv.text)
                                    androidx.compose.ui.text.input.TextFieldValue(
                                        masked,
                                        selection = androidx.compose.ui.text.TextRange(masked.length))
                                } else tfv
                            },
                            label = { Text("对方 Tailscale IP") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = manualPort,
                            onValueChange = { manualPort = it.filter { c -> c.isDigit() }.take(5) },
                            label = { Text("对方的监听端口") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        if (manualError.isNotEmpty()) {
                            Text(manualError, fontSize = 11.sp,
                                color = androidx.compose.ui.graphics.Color(0xFFF08A7C))
                        }
                        TextButton(onClick = {
                            val fixed = ManualPeers.normalizeHost(manualHost.text)
                            val port = manualPort.toIntOrNull()
                            if (fixed == null || port == null || port !in 1..65535) {
                                manualError = "Tailscale IP 无效，或对方监听端口不在 1-65535 范围内"
                            } else {
                                manualError = ""
                                editingPeer?.let { manualList.remove(it) }
                                manualList.removeAll { it.host == fixed && it.port == port }
                                manualList.add(ManualPeer(manualName.trim(), fixed, port))
                                editingPeer = null
                                manualName = ""
                                manualHost = androidx.compose.ui.text.input.TextFieldValue("")
                                manualPort = ""
                            }
                        }) { Text(if (editingPeer == null) "添加设备" else "保存设备") }
                        manualList.forEach { m ->
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Column(Modifier.weight(1f)) {
                                    if (m.name.isNotEmpty()) {
                                        Text(m.name, fontSize = 13.sp)
                                    }
                                    Text("${m.host}:${m.port}", fontSize = 12.sp,
                                        color = androidx.compose.ui.graphics.Color.Gray)
                                }
                                TextButton(onClick = {
                                    // 回填输入框，点「保存设备」后再替换原条目。
                                    manualName = m.name
                                    manualHost = androidx.compose.ui.text.input.TextFieldValue(
                                        m.host,
                                        selection = androidx.compose.ui.text.TextRange(m.host.length))
                                    manualPort = m.port.toString()
                                    editingPeer = m
                                }) { Text("编辑") }
                                TextButton(onClick = {
                                    manualList.remove(m)
                                    if (editingPeer == m) {
                                        editingPeer = null
                                        manualName = ""
                                        manualHost = androidx.compose.ui.text.input.TextFieldValue("")
                                        manualPort = ""
                                    }
                                }) { Text("删除") }
                            }
                        }

                        Spacer(Modifier.height(14.dp))
                        Text("帮助与更新", fontSize = 14.sp,
                            fontWeight = androidx.compose.ui.text.font.FontWeight.SemiBold)
                        Row {
                            TextButton(onClick = {
                                showUsageGuide.value = true
                            }) { Text("使用说明") }
                            TextButton(onClick = { checkUpdate() }) {
                                Text(if (checkingUpdate.value) "检查中…" else "检查更新")
                            }
                        }
                        TextButton(onClick = {
                            try {
                                startActivity(Intent(
                                    Intent.ACTION_VIEW,
                                    Uri.parse(Updater.REPOSITORY_PAGE),
                                ))
                            } catch (e: Exception) {
                                Toast.makeText(
                                    this@MainActivity,
                                    "无法打开 GitHub: ${e.message}",
                                    Toast.LENGTH_LONG,
                                ).show()
                            }
                        }) { Text("GitHub 仓库") }
                    }
                },
                confirmButton = {
                    TextButton(onClick = {
                        // 只有设置真正变化时才重建节点(改名/口令/端口/手动设备需生效)；
                        // 没变就不动，避免已连接的设备无谓断开、要重新点连接。
                        val parsedPort = portInput.toIntOrNull()
                        if (portInput.isNotBlank() && parsedPort !in 1..65535) {
                            settingsError = "本机监听端口必须在 1-65535 范围内"
                        } else {
                            val portVal = parsedPort ?: 0
                            val normalizedName = nameInput.trim().ifEmpty { Build.MODEL }
                            val manualChanged = ManualPeers.load(prefs) != manualList.toList()
                            val changed = normalizedName != peerName.value ||
                                secretInput != secret.value ||
                                trustedInput != trustedOnly.value ||
                                portVal != originalPort ||
                                manualChanged
                            peerName.value = normalizedName
                            secret.value = secretInput
                            trustedOnly.value = trustedInput
                            prefs.edit()
                                .putString("peer_name", normalizedName)
                                .putString("secret", secretInput)
                                .putBoolean("trusted_only", trustedInput)
                                .putInt("listen_port", portVal)
                                .apply()
                            ManualPeers.save(prefs, manualList.toList())
                            showSettings.value = false
                            if (changed) {
                                // 重启前记住当前选中目标，重建后由智能保留自动选回
                                InkHoleBus.pendingSelectedService =
                                    InkHoleBus.node?.getSelectedServiceName()
                                selectedPeer.value = null
                                peers.clear()
                                InkHoleService.restart(this@MainActivity)
                            }
                        }
                    }) { Text("保存") }
                },
                dismissButton = {
                    TextButton(onClick = { showSettings.value = false }) { Text("取消") }
                },
            )
        }
    }
}
