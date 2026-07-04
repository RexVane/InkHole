package com.rexvane.wormhole

import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.rexvane.wormhole.p2p.Peer

// ==================== 色板 ====================

private val BgDark        = Color(0xFF0a0a0f)
private val BgCard        = Color(0xFF161620)
private val BgCardActive  = Color(0xFF1e1e35)
private val AccentBlue    = Color(0xFF88aaff)
private val AccentPurple  = Color(0xFF6655cc)
private val TextPrimary   = Color(0xFFe8e8f0)
private val TextSecondary = Color(0xFF8888aa)
private val TextDim       = Color(0xFF555566)
private val BorderColor   = Color(0xFF252535)

// ==================== 黑洞图标 ====================

@Composable
fun BlackHoleIcon(modifier: Modifier = Modifier) {
    Canvas(modifier = modifier) {
        val r = size.minDimension / 2f
        val c = Offset(size.width / 2f, size.height / 2f)
        drawCircle(
            brush = Brush.radialGradient(
                colors = listOf(
                    Color(0xFF000000), Color(0xFF000000), Color(0xFF000000),
                    Color(0xFF0a0a18), Color(0xFF1a1828), Color(0xFF2a2840),
                    Color(0xFF484456), Color(0xFF665588), Color(0xFF88aaff),
                    Color(0xFF665588), Color(0xFF484456), Color(0xFF2a2840),
                    Color(0xFF1a1828), Color(0xFF0a0a0f),
                ),
                center = c,
                radius = r,
            ),
            center = c,
            radius = r,
        )
    }
}

// ==================== 主题 ====================

@Composable
fun WormholeTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            background = BgDark,
            surface = BgCard,
            primary = AccentBlue,
        ),
        content = content,
    )
}

// ==================== 主界面 ====================

@Composable
fun MainScreen(
    statusMsg: String,
    peers: List<Peer>,
    selectedPeer: String?,
    receivedFiles: List<String>,
    onDeviceClick: (Peer) -> Unit,
    onSendClick: () -> Unit,
    onOpenInbox: () -> Unit,
    onOpenFile: (String) -> Unit,
    onSettingsClick: () -> Unit,
    pendingCount: Int = 0,   // 系统分享进来、等着选设备的文件数
) {
    Scaffold(
        topBar = { WormholeTopBar(onSettingsClick) },
        containerColor = BgDark,
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 16.dp),
        ) {
            // 状态条
            Surface(
                color = BgCard.copy(alpha = 0.5f),
                shape = RoundedCornerShape(10.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 8.dp),
            ) {
                Text(
                    statusMsg,
                    color = AccentBlue,
                    fontSize = 13.sp,
                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                )
            }

            // 设备区域
            SectionHeader("设备", count = peers.size)
            if (peers.isEmpty()) {
                EmptyState()
            } else {
                LazyColumn(
                    modifier = Modifier.weight(1f),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    items(peers) { peer ->
                        DeviceCard(
                            peer = peer,
                            selected = peer.name == selectedPeer,
                            onClick = { onDeviceClick(peer) },
                        )
                    }
                }
            }

            // 发送按钮
            Spacer(Modifier.height(10.dp))
            SendButton(
                enabled = selectedPeer != null,
                label = when {
                    pendingCount > 0 && selectedPeer == null -> "点选设备发送 $pendingCount 个文件"
                    selectedPeer != null -> "发送文件"
                    else -> "请先选择设备"
                },
                onClick = onSendClick,
            )

            // 收件箱区域
            Spacer(Modifier.height(20.dp))
            InboxSection(
                files = receivedFiles,
                onOpenInbox = onOpenInbox,
                onOpenFile = onOpenFile,
            )
            Spacer(Modifier.height(16.dp))
        }
    }
}

// ==================== 子组件 ====================

@Composable
private fun WormholeTopBar(onSettingsClick: () -> Unit) {
    Surface(color = BgCard, shadowElevation = 2.dp) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            BlackHoleIcon(
                modifier = Modifier
                    .size(30.dp)
                    .clip(CircleShape),
            )
            Spacer(Modifier.width(12.dp))
            Text("虫洞", color = TextPrimary, fontSize = 22.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.weight(1f))
            IconButton(onClick = onSettingsClick) {
                Icon(Icons.Default.Settings, contentDescription = "设置", tint = TextSecondary)
            }
        }
    }
}

@Composable
private fun SectionHeader(title: String, count: Int = 0) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(top = 10.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(title, color = TextSecondary, fontSize = 13.sp, fontWeight = FontWeight.Medium)
        if (count > 0) {
            Spacer(Modifier.width(6.dp))
            Text("($count)", color = TextDim, fontSize = 12.sp)
        }
    }
}

@Composable
private fun EmptyState() {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 36.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        BlackHoleIcon(
            modifier = Modifier
                .size(56.dp)
                .clip(CircleShape),
        )
        Spacer(Modifier.height(14.dp))
        Text("搜索设备中…", color = TextSecondary, fontSize = 14.sp)
        Spacer(Modifier.height(4.dp))
        Text("等待其他虫洞设备上线", color = TextDim, fontSize = 12.sp)
    }
}

@Composable
private fun DeviceCard(peer: Peer, selected: Boolean, onClick: () -> Unit) {
    val bg by animateColorAsState(
        targetValue = if (selected) BgCardActive else BgCard,
        label = "cardBg",
    )
    val borderC by animateColorAsState(
        targetValue = if (selected) AccentBlue.copy(alpha = 0.5f) else BorderColor,
        label = "cardBorder",
    )

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(bg)
            .border(1.dp, borderC, RoundedCornerShape(12.dp))
            .clickable { onClick() }
            .padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(10.dp)
                .clip(CircleShape)
                .background(if (selected) AccentBlue else TextDim.copy(alpha = 0.3f)),
        )
        Spacer(Modifier.width(12.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                peer.name,
                color = TextPrimary,
                fontSize = 15.sp,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(peer.host, color = TextDim, fontSize = 11.sp, maxLines = 1)
        }
        if (selected) {
            Text("已选中", color = AccentBlue, fontSize = 11.sp)
        }
    }
}

@Composable
private fun SendButton(enabled: Boolean, label: String, onClick: () -> Unit) {
    val gradient = Brush.horizontalGradient(
        colors = if (enabled) listOf(Color(0xFF5544aa), Color(0xFF88aaff))
                 else listOf(Color(0xFF252535), Color(0xFF2a2a3a)),
    )
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(52.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(gradient)
            .clickable { if (enabled) onClick() }
            .padding(horizontal = 16.dp),
        contentAlignment = Alignment.Center,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(
                Icons.Default.Send,
                contentDescription = null,
                tint = if (enabled) Color.White else TextDim,
                modifier = Modifier.size(18.dp),
            )
            Spacer(Modifier.width(8.dp))
            Text(
                label,
                color = if (enabled) Color.White else TextDim,
                fontSize = 15.sp,
                fontWeight = FontWeight.Medium,
            )
        }
    }
}

@Composable
private fun InboxSection(
    files: List<String>,
    onOpenInbox: () -> Unit,
    onOpenFile: (String) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("收件箱", color = TextSecondary, fontSize = 13.sp, fontWeight = FontWeight.Medium)
        TextButton(onClick = onOpenInbox) {
            Icon(Icons.Default.Folder, contentDescription = null, tint = AccentBlue, modifier = Modifier.size(16.dp))
            Spacer(Modifier.width(4.dp))
            Text("打开收件箱", color = AccentBlue, fontSize = 13.sp)
        }
    }
    if (files.isEmpty()) {
        Text(
            "暂无已接收文件",
            color = TextDim,
            fontSize = 13.sp,
            modifier = Modifier.padding(top = 4.dp, bottom = 16.dp),
        )
    } else {
        LazyColumn(
            modifier = Modifier.heightIn(max = 200.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            items(files) { name ->
                FileItem(name) { onOpenFile(name) }
            }
        }
    }
}

@Composable
private fun FileItem(fileName: String, onClick: () -> Unit) {
    Surface(
        color = BgCard,
        shape = RoundedCornerShape(10.dp),
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onClick() },
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 14.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("📄", fontSize = 16.sp)
            Spacer(Modifier.width(10.dp))
            Text(
                fileName,
                color = AccentBlue,
                fontSize = 13.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
        }
    }
}

// ==================== Preview 预览 ====================

@Preview(showBackground = true, backgroundColor = 0xFF0a0a0f)
@Composable
fun PreviewMainScreen() {
    val mockPeers = listOf(
        Peer("MacBook Pro", "192.168.1.100", 8848),
        Peer("ThinkPad X1", "192.168.1.101", 8848),
    )
    WormholeTheme {
        MainScreen(
            statusMsg = "已发现 2 台设备",
            peers = mockPeers,
            selectedPeer = "MacBook Pro",
            receivedFiles = listOf("年度报告.pdf", "截图_001.png"),
            onDeviceClick = {},
            onSendClick = {},
            onOpenInbox = {},
            onOpenFile = {},
            onSettingsClick = {},
        )
    }
}

@Preview(showBackground = true, backgroundColor = 0xFF0a0a0f)
@Preview(name = "空状态", group = "states")
@Composable
fun PreviewEmptyState() {
    WormholeTheme {
        MainScreen(
            statusMsg = "正在搜索设备…",
            peers = emptyList(),
            selectedPeer = null,
            receivedFiles = emptyList(),
            onDeviceClick = {},
            onSendClick = {},
            onOpenInbox = {},
            onOpenFile = {},
            onSettingsClick = {},
        )
    }
}

@Preview(showBackground = true, backgroundColor = 0xFF0a0a0f, name = "黑洞图标")
@Composable
fun PreviewBlackHole() {
    Column(
        modifier = Modifier.padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        BlackHoleIcon(modifier = Modifier.size(80.dp).clip(CircleShape))
        Spacer(Modifier.height(12.dp))
        Text("虫洞 Wormhole", color = Color.White, fontSize = 14.sp)
    }
}
