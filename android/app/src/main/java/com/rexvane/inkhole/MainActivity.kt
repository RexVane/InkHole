package com.rexvane.inkhole

import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
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
import java.io.File

/**
 * 主界面(纯 UI)。P2P 节点的生命周期在 InkHoleService(前台服务)：
 * 转屏/锁屏/切后台都不断线，Activity 只是挂上去看状态、发起发送。
 *
 * 发送入口：
 *   1. 轻点墨洞 -> 系统文件选择器(可多选)
 *   2. 系统分享菜单(图库/文件管理器 -> 分享 -> 墨洞)，未选设备时先排队
 */
class MainActivity : ComponentActivity() {

    // UI 状态
    private val peers = mutableStateListOf<Peer>()
    private val selectedPeer = mutableStateOf<String?>(null)
    private val statusMsg = mutableStateOf("正在启动…")
    private val receivedFiles = mutableStateListOf<ReceivedFile>()
    private val pendingShares = mutableStateListOf<Uri>()   // 分享进来、等选设备的文件
    private val transferPct = mutableStateOf(-1f)           // -1=空闲
    private val transferKind = mutableStateOf("")

    // 设置
    private val peerName = mutableStateOf(Build.MODEL)
    private val secret = mutableStateOf("")
    private val trustedOnly = mutableStateOf(false)
    private var showSettings = mutableStateOf(false)

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()) { /* 拒绝也能用，只是没通知/旧机型不导出 */ }

    private val filePicker = registerForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments()
    ) { uris ->
        if (!uris.isNullOrEmpty()) sendUris(uris)
    }

    private val uiListener = object : InkHoleListener {
        override fun onPeerChanged(list: List<Peer>) = runOnUiThread {
            peers.clear()
            peers.addAll(list)
            if (selectedPeer.value != null && list.none { it.name == selectedPeer.value }) {
                selectedPeer.value = null   // 选中的设备离线了
            }
        }
        override fun onFileReceived(filename: String, path: String) = runOnUiThread {
            refreshReceived()
        }
        override fun onStatus(msg: String) = runOnUiThread { statusMsg.value = msg }
        override fun onProgress(kind: String, filename: String, done: Long, total: Long) =
            runOnUiThread {
                val pct = if (total > 0) (done * 100f / total) else 100f
                transferKind.value = kind
                transferPct.value = if (pct >= 100f) -1f else pct
                statusMsg.value =
                    "${if (kind == "send") "↑ 吞入" else "↓ 吐出"} $filename ${pct.toInt()}%"
            }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val prefs = getSharedPreferences("inkhole", Context.MODE_PRIVATE)
        peerName.value = prefs.getString("peer_name", Build.MODEL) ?: Build.MODEL
        secret.value = prefs.getString("secret", "") ?: ""
        trustedOnly.value = prefs.getBoolean("trusted_only", false)

        requestNeededPermissions()
        InkHoleService.start(this)
        InkHoleBus.loadHistory(this)
        InkHoleBus.uiListener = uiListener

        // 服务可能早已在跑(锁屏收文件后点开)：恢复最近状态
        peers.addAll(InkHoleBus.lastPeers)
        statusMsg.value = InkHoleBus.lastStatus
        selectedPeer.value = InkHoleBus.node?.getSelectedPeer()
        refreshReceived()

        handleShareIntent(intent)

        setContent {
            InkHoleTheme {
                MainScreen(
                    statusMsg = statusMsg.value,
                    peers = peers,
                    selectedPeer = selectedPeer.value,
                    receivedFiles = receivedFiles,
                    transferPct = transferPct.value,
                    transferKind = transferKind.value,
                    pendingCount = pendingShares.size,
                    onDeviceClick = { peer -> onDeviceClicked(peer) },
                    onSendClick = {
                        if (selectedPeer.value == null) {
                            statusMsg.value = if (peers.isEmpty())
                                "还没发现设备，两台设备要连同一个 WiFi" else "先点选一台目标设备"
                        } else {
                            filePicker.launch(arrayOf("*/*"))
                        }
                    },
                    onOpenInbox = { openInbox() },
                    onOpenFile = { rec -> openFile(rec) },
                    onClearHistory = {
                        InkHoleBus.clearHistory(this)
                        refreshReceived()
                    },
                    onSettingsClick = { showSettings.value = true },
                )
                SettingsDialog()
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShareIntent(intent)
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
        if (selectedPeer.value != null) {
            sendUris(uris)
        } else {
            pendingShares.addAll(uris)
            statusMsg.value = "收到 ${pendingShares.size} 个待发文件"
        }
    }

    private fun Intent.streamUris(): List<Uri> {
        if (action != Intent.ACTION_SEND && action != Intent.ACTION_SEND_MULTIPLE) return emptyList()
        return if (Build.VERSION.SDK_INT >= 33) {
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
    }

    /** 把 content uri 复制到 cache 后经 P2P 发送(顺序发，避免并发写同名缓存)。 */
    private fun sendUris(uris: List<Uri>) {
        val node = InkHoleBus.node ?: run {
            statusMsg.value = "墨洞未就绪"
            return
        }
        Thread {
            var okCount = 0
            for (uri in uris) {
                var tmp: File? = null
                try {
                    val name = File(queryDisplayName(uri) ?: "file_${System.currentTimeMillis()}").name
                    val dst = File(cacheDir, name)
                    tmp = dst
                    contentResolver.openInputStream(uri)?.use { input ->
                        dst.outputStream().use { input.copyTo(it) }
                    } ?: continue
                    runOnUiThread { statusMsg.value = "吞入: $name" }
                    if (node.sendFile(dst.absolutePath)) okCount++
                } catch (e: Exception) {
                    runOnUiThread { statusMsg.value = "发送失败: ${e.message}" }
                } finally {
                    tmp?.delete()
                }
            }
            if (uris.size > 1) {
                runOnUiThread { statusMsg.value = "已吞入 $okCount/${uris.size} 个文件" }
            }
        }.start()
    }

    private fun queryDisplayName(uri: Uri): String? =
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (idx >= 0 && c.moveToFirst()) c.getString(idx) else null
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
            val manualList = remember {
                mutableStateListOf<ManualPeer>().apply { addAll(ManualPeers.load(prefs)) }
            }
            var manualHost by remember { mutableStateOf("") }
            var manualPort by remember { mutableStateOf("52130") }
            var manualError by remember { mutableStateOf("") }
            AlertDialog(
                onDismissRequest = { showSettings.value = false },
                title = { Text("设置") },
                text = {
                    Column(
                        modifier = Modifier.verticalScroll(rememberScrollState()),
                    ) {
                        // 本机信息提示：对端看到的设备名-instance_id
                        val instanceId = prefs.getString("instance_id", "") ?: ""
                        Text(
                            text = "本机：${peerName.value}-$instanceId",
                            fontSize = 12.sp,
                            color = androidx.compose.ui.graphics.Color.Gray,
                            modifier = Modifier.padding(bottom = 12.dp)
                        )
                        OutlinedTextField(
                            value = nameInput,
                            onValueChange = { nameInput = it },
                            label = { Text("设备名称") },
                            singleLine = true,
                        )
                        Spacer(Modifier.height(8.dp))
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
                                Text("拦掉局域网里陌生设备发来的文件",
                                    fontSize = 11.sp,
                                    color = androidx.compose.ui.graphics.Color.Gray)
                            }
                            Spacer(Modifier.width(8.dp))
                            Switch(checked = trustedInput,
                                onCheckedChange = { trustedInput = it })
                        }

                        // ---- 手动添加设备(跨网直连,如 Tailscale) ----
                        Spacer(Modifier.height(14.dp))
                        Text("手动添加设备", fontSize = 14.sp)
                        Text("自动发现不可用时(如 Tailscale 跨网)填对方 IP 与监听端口",
                            fontSize = 11.sp,
                            color = androidx.compose.ui.graphics.Color.Gray)
                        manualList.forEach { m ->
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Text("${m.host}:${m.port}",
                                    fontSize = 13.sp, modifier = Modifier.weight(1f))
                                TextButton(onClick = { manualList.remove(m) }) { Text("删除") }
                            }
                        }
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            OutlinedTextField(
                                value = manualHost,
                                onValueChange = { manualHost = it },
                                label = { Text("对方 IP") },
                                singleLine = true,
                                modifier = Modifier.weight(1f),
                            )
                            Spacer(Modifier.width(6.dp))
                            OutlinedTextField(
                                value = manualPort,
                                onValueChange = { manualPort = it.filter { c -> c.isDigit() } },
                                label = { Text("端口") },
                                singleLine = true,
                                modifier = Modifier.width(92.dp),
                            )
                        }
                        if (manualError.isNotEmpty()) {
                            Text(manualError, fontSize = 11.sp,
                                color = androidx.compose.ui.graphics.Color(0xFFF08A7C))
                        }
                        TextButton(onClick = {
                            val fixed = ManualPeers.normalizeHost(manualHost)
                            val port = manualPort.toIntOrNull()
                            if (fixed == null || port == null || port !in 1..65535) {
                                manualError = "IP 无效或有歧义，请检查（端口 1-65535）"
                            } else {
                                manualError = ""
                                manualList.removeAll { it.host == fixed && it.port == port }
                                manualList.add(ManualPeer("", fixed, port))
                                manualHost = ""
                            }
                        }) { Text("添加设备") }
                    }
                },
                confirmButton = {
                    TextButton(onClick = {
                        // 只有设置真正变化时才重建节点(改名/改口令/手动设备需生效)；
                        // 没变就不动，避免已连接的设备无谓断开、要重新点连接。
                        val manualChanged = ManualPeers.load(prefs) != manualList.toList()
                        val changed = nameInput != peerName.value ||
                            secretInput != secret.value ||
                            trustedInput != trustedOnly.value ||
                            manualChanged
                        peerName.value = nameInput
                        secret.value = secretInput
                        trustedOnly.value = trustedInput
                        prefs.edit()
                            .putString("peer_name", nameInput)
                            .putString("secret", secretInput)
                            .putBoolean("trusted_only", trustedInput)
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
                    }) { Text("保存") }
                },
                dismissButton = {
                    TextButton(onClick = { showSettings.value = false }) { Text("取消") }
                },
            )
        }
    }
}
