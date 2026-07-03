package com.rexvane.wormhole

import android.app.Activity
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.rexvane.wormhole.p2p.Peer
import com.rexvane.wormhole.p2p.WormholeListener
import com.rexvane.wormhole.p2p.WormholeNode
import java.io.File

class MainActivity : ComponentActivity() {

    private var node: WormholeNode? = null

    // UI 状态
    private val peers = mutableStateListOf<Peer>()
    private val selectedPeer = mutableStateOf<String?>(null)
    private val statusMsg = mutableStateOf("正在启动…")
    private val receivedFiles = mutableStateListOf<String>()

    // 设置
    private val peerName = mutableStateOf(Build.MODEL)
    private val secret = mutableStateOf("")
    private var showSettings = mutableStateOf(false)

    private val filePicker = registerForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri ->
        if (uri != null) {
            // 把选中文件复制到临时文件再发送
            Thread {
                try {
                    val tmp = File(cacheDir, "send_${System.currentTimeMillis()}")
                    contentResolver.openInputStream(uri)?.use { input ->
                        tmp.outputStream().use { input.copyTo(it) }
                    }
                    // 从 URI 取文件名
                    var name = "file"
                    contentResolver.query(uri, null, null, null, null)?.use { c ->
                        val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                        if (idx >= 0 && c.moveToFirst()) name = c.getString(idx)
                    }
                    val dst = File(cacheDir, name)
                    tmp.renameTo(dst)
                    runOnUiThread { statusMsg.value = "发送中: $name" }
                    val ok = node?.sendFile(dst.absolutePath) ?: false
                    runOnUiThread {
                        statusMsg.value = if (ok) "已发送: $name" else "发送失败"
                    }
                    dst.delete()
                } catch (e: Exception) {
                    runOnUiThread { statusMsg.value = "发送失败: ${e.message}" }
                }
            }.start()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 从 SharedPreferences 读设置
        val prefs = getSharedPreferences("wormhole", Context.MODE_PRIVATE)
        peerName.value = prefs.getString("peer_name", Build.MODEL) ?: Build.MODEL
        secret.value = prefs.getString("secret", "") ?: ""

        // 收件箱
        val inbox = File(getExternalFilesDir(null), "收件箱")

        // 创建 P2P 节点
        node = WormholeNode(
            context = this,
            peerName = peerName.value,
            inboxDir = inbox,
            secret = secret.value,
            listener = object : WormholeListener {
                override fun onPeerChanged(list: List<Peer>) {
                    runOnUiThread {
                        peers.clear()
                        peers.addAll(list)
                    }
                }
                override fun onFileReceived(filename: String, path: String) {
                    runOnUiThread { receivedFiles.add(0, filename) }
                }
                override fun onStatus(msg: String) {
                    runOnUiThread { statusMsg.value = msg }
                }
            },
        )
        node?.start()

        setContent {
            WormholeTheme {
                MainScreen()
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        node?.stop()
    }

    @Composable
    private fun MainScreen() {
        Scaffold(
            topBar = {
                Surface(color = Color(0xFF1a1a1a)) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        // 黑洞图标
                        Box(
                            modifier = Modifier
                                .size(28.dp)
                                .clip(CircleShape)
                                .background(
                                    Brush.radialGradient(
                                        colors = listOf(
                                            Color(0xFF000000),
                                            Color(0xFF1a1a2e),
                                            Color(0xFFe8e8f4),
                                            Color(0xFF1a1a1a),
                                        ),
                                        radius = 14.dp.toPx(),
                                    )
                                )
                        )
                        Spacer(Modifier.width(10.dp))
                        Text(
                            "虫洞",
                            color = Color.White,
                            fontSize = 20.sp,
                            fontWeight = FontWeight.Bold,
                        )
                        Spacer(Modifier.weight(1f))
                        IconButton(onClick = { showSettings.value = true }) {
                            Icon(Icons.Default.Settings, contentDescription = "设置", tint = Color.White)
                        }
                    }
                }
            },
        ) { padding ->
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Color(0xFF0d0d0d))
                    .padding(padding)
                    .padding(horizontal = 16.dp),
            ) {
                // 状态行
                Text(
                    statusMsg.value,
                    color = Color(0xFFaaaacc),
                    fontSize = 13.sp,
                    modifier = Modifier.padding(vertical = 12.dp),
                )

                // 设备列表
                Text(
                    "设备",
                    color = Color(0xFF888899),
                    fontSize = 13.sp,
                    modifier = Modifier.padding(bottom = 8.dp),
                )
                if (peers.isEmpty()) {
                    Text(
                        "🔍 搜索设备中…",
                        color = Color(0xFF666677),
                        fontSize = 14.sp,
                        modifier = Modifier.padding(vertical = 16.dp),
                    )
                } else {
                    LazyColumn(
                        modifier = Modifier.weight(1f),
                        verticalArrangement = Arrangement.spacedBy(4.dp),
                    ) {
                        items(peers) { peer ->
                            DeviceRow(peer, peer.name == selectedPeer.value) {
                                selectedPeer.value = if (selectedPeer.value == peer.name) null else peer.name
                                node?.selectPeer(selectedPeer.value)
                            }
                        }
                    }
                }

                // 发送按钮
                Spacer(Modifier.height(8.dp))
                Button(
                    onClick = {
                        if (selectedPeer.value == null) {
                            statusMsg.value = "请先选择目标设备"
                        } else {
                            filePicker.launch(arrayOf("*/*"))
                        }
                    },
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(50.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = Color(0xFF4a4a6e),
                    ),
                    shape = RoundedCornerShape(12.dp),
                ) {
                    Icon(Icons.Default.Send, contentDescription = null, tint = Color.White)
                    Spacer(Modifier.width(8.dp))
                    Text("发送文件", color = Color.White)
                }

                // 收件箱
                Spacer(Modifier.height(16.dp))
                Text(
                    "收件箱",
                    color = Color(0xFF888899),
                    fontSize = 13.sp,
                    modifier = Modifier.padding(bottom = 8.dp),
                )
                if (receivedFiles.isEmpty()) {
                    Text(
                        "暂无已接收文件",
                        color = Color(0xFF555566),
                        fontSize = 13.sp,
                        modifier = Modifier.padding(bottom = 16.dp),
                    )
                } else {
                    LazyColumn(
                        modifier = Modifier.heightIn(max = 200.dp),
                        verticalArrangement = Arrangement.spacedBy(2.dp),
                    ) {
                        items(receivedFiles) { name ->
                            Text(
                                "📄 $name",
                                color = Color(0xFFccccdd),
                                fontSize = 13.sp,
                                modifier = Modifier.padding(vertical = 4.dp),
                            )
                        }
                    }
                }
                Spacer(Modifier.height(16.dp))
            }
        }
    }

    @Composable
    private fun DeviceRow(peer: Peer, selected: Boolean, onClick: () -> Unit) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(10.dp))
                .background(if (selected) Color(0xFF3a3a5e) else Color(0xFF1a1a1a))
                .clickable { onClick() }
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                if (selected) "●" else "○",
                color = if (selected) Color(0xFF88aaff) else Color(0xFF555566),
                fontSize = 16.sp,
            )
            Spacer(Modifier.width(10.dp))
            Text(peer.name, color = Color.White, fontSize = 15.sp)
            Spacer(Modifier.weight(1f))
            Text(peer.host, color = Color(0xFF666677), fontSize = 12.sp)
        }
    }

    @Composable
    private fun WormholeTheme(content: @Composable () -> Unit) {
        MaterialTheme(
            colorScheme = darkColorScheme(
                background = Color(0xFF0d0d0d),
                surface = Color(0xFF1a1a1a),
                primary = Color(0xFF88aaff),
            ),
            content = content,
        )
    }

    // 设置弹窗
    // (Compose 中用 AlertDialog 实现, 此处省略详细实现, 用最简版本)
    @Composable
    fun SettingsDialog() {
        if (showSettings.value) {
            var nameInput by remember { mutableStateOf(peerName.value) }
            var secretInput by remember { mutableStateOf(secret.value) }
            AlertDialog(
                onDismissRequest = { showSettings.value = false },
                title = { Text("设置") },
                text = {
                    Column {
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
                            label = { Text("加密口令 (可选)") },
                            singleLine = true,
                        )
                    }
                },
                confirmButton = {
                    TextButton(onClick = {
                        peerName.value = nameInput
                        secret.value = secretInput
                        getSharedPreferences("wormhole", Context.MODE_PRIVATE)
                            .edit()
                            .putString("peer_name", nameInput)
                            .putString("secret", secretInput)
                            .apply()
                        showSettings.value = false
                        // 重启节点以应用新设置
                        node?.stop()
                        recreate()
                    }) { Text("保存并重启") }
                },
                dismissButton = {
                    TextButton(onClick = { showSettings.value = false }) { Text("取消") }
                },
            )
        }
    }
}
