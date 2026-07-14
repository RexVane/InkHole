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
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.rexvane.inkhole.p2p.Peer
import com.rexvane.inkhole.p2p.InkHoleListener
import com.rexvane.inkhole.relay.RelaySettings
import com.rexvane.inkhole.relay.RelayNode
import com.rexvane.inkhole.relay.AndroidHostKey
import com.rexvane.inkhole.relay.AndroidSshConnector
import com.rexvane.inkhole.relay.AndroidSshCredentials
import com.rexvane.inkhole.relay.SshRelayRuntime
import com.rexvane.inkhole.relay.SshRelaySession
import com.rexvane.inkhole.relay.registryGroupKey
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

private data class HostKeyPrompt(
    val hostKey: AndroidHostKey,
    val latch: CountDownLatch = CountDownLatch(1),
    val accepted: AtomicBoolean = AtomicBoolean(false),
)

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
    private val transferActive = mutableStateOf(false)
    private val transportMode = mutableStateOf("local")
    private val relayConfigured = mutableStateOf(false)

    // 设置
    private val peerName = mutableStateOf(Build.MODEL)
    private val secret = mutableStateOf("")
    private val trustedOnly = mutableStateOf(false)
    private var showSettings = mutableStateOf(false)
    private var showRelaySetup = mutableStateOf(false)
    private val hostKeyPrompt = mutableStateOf<HostKeyPrompt?>(null)

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
            val restored = InkHoleBus.node?.getSelectedPeer()
            if (selectedPeer.value == null && restored != null && list.any { it.name == restored }) {
                selectedPeer.value = restored
            }
            if (selectedPeer.value != null && list.none { it.name == selectedPeer.value }) {
                selectedPeer.value = null   // 选中的设备离线了
            }
        }
        override fun onFileReceived(filename: String, path: String) = runOnUiThread {
            transferActive.value = false
            refreshReceived()
        }
        override fun onStatus(msg: String) = runOnUiThread { statusMsg.value = msg }
        override fun onProgress(kind: String, filename: String, done: Long, total: Long) =
            runOnUiThread {
                val pct = if (total > 0) (done * 100f / total) else 100f
                transferKind.value = kind
                transferPct.value = if (pct >= 100f) -1f else pct
                transferActive.value = pct < 100f
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
        transportMode.value = prefs.getString("transport_mode", "local") ?: "local"
        relayConfigured.value = RelaySettings.load(this) != null

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
                    transportMode = transportMode.value,
                    modeSwitchEnabled = !transferActive.value,
                    relayConfigured = relayConfigured.value,
                    onModeChange = { mode -> switchTransportMode(mode) },
                    onRelaySetup = { showRelaySetup.value = true },
                    onDeviceClick = { peer -> onDeviceClicked(peer) },
                    onSendClick = {
                        if (selectedPeer.value == null) {
                            statusMsg.value = when {
                                transportMode.value == "remote" && !relayConfigured.value ->
                                    "请先连接 SSH 服务器"
                                peers.isEmpty() && transportMode.value == "remote" ->
                                    "等待同一 SSH 账户下的设备上线"
                                peers.isEmpty() -> "还没发现设备，两台设备要连同一个 WiFi"
                                else -> "先点选一台目标设备"
                            }
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
                RelaySetupDialog()
                HostKeyConfirmDialog()
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
        transferActive.value = true
        InkHoleBus.transferActive = true
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
            InkHoleBus.transferActive = false
            runOnUiThread { transferActive.value = false }
        }.start()
    }

    private fun switchTransportMode(mode: String) {
        if (mode !in listOf("local", "remote") || transferActive.value || InkHoleBus.transferActive) {
            statusMsg.value = "传输进行中，暂时不能切换模式"
            return
        }
        if (mode == transportMode.value) return
        transportMode.value = mode
        selectedPeer.value = null
        peers.clear()
        getSharedPreferences("inkhole", Context.MODE_PRIVATE).edit()
            .putString("transport_mode", mode).apply()
        InkHoleBus.pendingSelectedService = null
        InkHoleService.restart(this)
        if (mode == "remote" && !relayConfigured.value) showRelaySetup.value = true
    }

    private fun connectSshRelay(
        host: String,
        username: String,
        port: Int,
        privateKey: String,
        passphrase: String,
    ) {
        if (transferActive.value || InkHoleBus.transferActive) {
            statusMsg.value = "传输进行中，不能更换 SSH 服务器"
            return
        }
        showRelaySetup.value = false
        statusMsg.value = "正在检查 SSH 主机密钥…"
        val credentials = AndroidSshCredentials(
            host, username, port, privateKey.toCharArray(), passphrase.toCharArray())
        Thread {
            var installed = false
            try {
                val connector = AndroidSshConnector()
                val hostKey = connector.probeHostKey(host, port)
                val prompt = HostKeyPrompt(hostKey)
                runOnUiThread { hostKeyPrompt.value = prompt }
                if (!prompt.latch.await(120, TimeUnit.SECONDS) || !prompt.accepted.get()) {
                    throw IllegalStateException("未确认 SSH 主机密钥")
                }
                runOnUiThread { statusMsg.value = "正在验证 SSH 转发权限…" }
                val verified = connector.validate(credentials, hostKey)
                val groupKey = registryGroupKey(verified.privateKey)
                RelaySettings.create(this, host, verified.username, port, hostKey)
                SshRelayRuntime.install(SshRelaySession(verified, groupKey))
                installed = true
                getSharedPreferences("inkhole", Context.MODE_PRIVATE).edit()
                    .putString("transport_mode", "remote").apply()
                runOnUiThread {
                    relayConfigured.value = true
                    transportMode.value = "remote"
                    statusMsg.value = "SSH 远程通道已启动"
                    peers.clear()
                    selectedPeer.value = null
                    InkHoleService.restart(this)
                }
            } catch (e: Exception) {
                runOnUiThread { statusMsg.value = "SSH 连接失败: ${e.message}" }
            } finally {
                if (!installed) credentials.clear()
                runOnUiThread { hostKeyPrompt.value = null }
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
            var nameInput by remember { mutableStateOf(peerName.value) }
            var secretInput by remember { mutableStateOf(secret.value) }
            var trustedInput by remember { mutableStateOf(trustedOnly.value) }
            AlertDialog(
                onDismissRequest = { showSettings.value = false },
                title = { Text("设置") },
                text = {
                    Column {
                        // 本机信息提示：对端看到的设备名-instance_id
                        val instanceId = getSharedPreferences("inkhole", Context.MODE_PRIVATE)
                            .getString("instance_id", "") ?: ""
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
                                onCheckedChange = { trustedInput = it },
                                enabled = transportMode.value == "local")
                        }
                        Spacer(Modifier.height(8.dp))
                        TextButton(onClick = {
                            showSettings.value = false
                            showRelaySetup.value = true
                        }) {
                            Text(if (relayConfigured.value)
                                "更换 SSH 服务器" else "连接 SSH 服务器")
                        }
                    }
                },
                confirmButton = {
                    TextButton(onClick = {
                        // 只有设置真正变化时才重建节点(改名/改口令需重新注册 mDNS)；
                        // 没变就不动，避免已连接的设备无谓断开、要重新点连接。
                        val changed = nameInput != peerName.value ||
                            secretInput != secret.value ||
                            trustedInput != trustedOnly.value
                        val remoteRename = transportMode.value == "remote" &&
                            nameInput != peerName.value
                        val requestedName = nameInput
                        peerName.value = nameInput
                        secret.value = secretInput
                        trustedOnly.value = trustedInput
                        getSharedPreferences("inkhole", Context.MODE_PRIVATE)
                            .edit()
                            .putString("peer_name", nameInput)
                            .putString("secret", secretInput)
                            .putBoolean("trusted_only", trustedInput)
                            .apply()
                        showSettings.value = false
                        if (changed) {
                            // 重启前记住当前选中目标，重建后由智能保留自动选回
                            InkHoleBus.pendingSelectedService =
                                InkHoleBus.node?.getSelectedServiceName()
                            selectedPeer.value = null
                            peers.clear()
                            if (remoteRename) {
                                val relayNode = InkHoleBus.node as? RelayNode
                                Thread {
                                    runCatching { relayNode?.rename(requestedName) }
                                        .onFailure { error -> runOnUiThread {
                                            statusMsg.value = "远程设备改名失败: ${error.message}"
                                        } }
                                    runOnUiThread { InkHoleService.restart(this@MainActivity) }
                                }.start()
                            } else {
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

    @Composable
    private fun RelaySetupDialog() {
        if (!showRelaySetup.value) return
        val profile = remember { RelaySettings.loadProfile(this@MainActivity) }
        var host by remember { mutableStateOf(profile?.host.orEmpty()) }
        var username by remember { mutableStateOf(profile?.username.orEmpty()) }
        var port by remember { mutableStateOf((profile?.port ?: 22).toString()) }
        var privateKey by remember { mutableStateOf("") }
        var passphrase by remember { mutableStateOf("") }
        var showAdvanced by remember { mutableStateOf(false) }
        AlertDialog(
            onDismissRequest = { showRelaySetup.value = false },
            title = { Text("连接 SSH 服务器") },
            text = {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .verticalScroll(rememberScrollState()),
                ) {
                    OutlinedTextField(
                        value = host,
                        onValueChange = { host = it },
                        label = { Text("服务器公网 IP / 域名") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(7.dp))
                    OutlinedTextField(
                        value = privateKey,
                        onValueChange = { privateKey = it },
                        label = { Text("共享 SSH 私钥（所有设备一致）") },
                        minLines = 5,
                        maxLines = 8,
                        visualTransformation = PasswordVisualTransformation(),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(8.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text("高级设置", modifier = Modifier.weight(1f), fontSize = 13.sp)
                        Switch(checked = showAdvanced,
                            onCheckedChange = { showAdvanced = it })
                    }
                    if (showAdvanced) {
                        OutlinedTextField(
                            value = username,
                            onValueChange = { username = it },
                            label = { Text("SSH 用户名（留空自动识别）") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Spacer(Modifier.height(7.dp))
                        OutlinedTextField(
                            value = port,
                            onValueChange = { port = it.filter(Char::isDigit).take(5) },
                            label = { Text("SSH 端口") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Spacer(Modifier.height(7.dp))
                        OutlinedTextField(
                            value = passphrase,
                            onValueChange = { passphrase = it },
                            label = { Text("私钥口令（可选）") },
                            singleLine = true,
                            visualTransformation = PasswordVisualTransformation(),
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
                }
            },
            confirmButton = {
                Button(
                    enabled = host.isNotBlank() && privateKey.isNotBlank() &&
                        (port.toIntOrNull() ?: 0) in 1..65535,
                    onClick = {
                        val key = privateKey
                        val phrase = passphrase
                        privateKey = ""
                        passphrase = ""
                        connectSshRelay(host.trim(), username.trim(),
                            port.toInt(), key, phrase)
                    },
                ) { Text("连接") }
            },
            dismissButton = {
                TextButton(onClick = { showRelaySetup.value = false }) { Text("取消") }
            },
        )
    }

    @Composable
    private fun HostKeyConfirmDialog() {
        val prompt = hostKeyPrompt.value ?: return
        AlertDialog(
            onDismissRequest = {
                prompt.accepted.set(false); prompt.latch.countDown(); hostKeyPrompt.value = null
            },
            title = { Text("确认 SSH 主机密钥") },
            text = {
                Column {
                    Text(prompt.hostKey.algorithm, fontSize = 12.sp)
                    Spacer(Modifier.height(8.dp))
                    Text(prompt.hostKey.fingerprint, fontSize = 13.sp)
                }
            },
            confirmButton = {
                Button(onClick = {
                    prompt.accepted.set(true); prompt.latch.countDown(); hostKeyPrompt.value = null
                }) { Text("确认") }
            },
            dismissButton = {
                TextButton(onClick = {
                    prompt.accepted.set(false); prompt.latch.countDown(); hostKeyPrompt.value = null
                }) { Text("取消") }
            },
        )
    }

}
