package com.rexvane.inkhole

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.outlined.Computer
import androidx.compose.material.icons.outlined.DeleteSweep
import androidx.compose.material.icons.outlined.Folder
import androidx.compose.material.icons.outlined.Smartphone
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.rexvane.inkhole.p2p.Peer

// ==================== 墨洞色板 ====================

private val BgDark        = Color(0xFF05070A)   // 墨黑底
private val BgCard        = Color(0xFF0D1416)
private val BgCardActive  = Color(0xFF11201D)
private val Teal          = Color(0xFF58E6C8)   // 视界青
private val TealSoft      = Color(0xFF7FEFD8)
private val TealDim       = Color(0xFF1E4A42)
private val TextPrimary   = Color(0xFFE3F2EC)
private val TextSecondary = Color(0xFF7FA098)
private val TextDim       = Color(0xFF48605A)
private val BorderColor   = Color(0xFF172623)

// ==================== 主题 ====================

@Composable
fun InkHoleTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            background = BgDark,
            surface = BgCard,
            primary = Teal,
        ),
        content = content,
    )
}

// ==================== 墨洞英雄区 ====================
// 平时：墨黑核心 + 双层青弧缓慢反向旋转 + 呼吸；无设备时外圈雷达波纹搜寻。
// 传输：外圈亮起青色进度环。轻点墨洞 = 选文件发送。

@Composable
fun InkHoleHero(
    transferPct: Float,          // -1 = 空闲；0..100 显示进度环
    searching: Boolean,          // true = 还没发现设备，播放雷达波纹
    onTap: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val infinite = rememberInfiniteTransition(label = "hole")
    val spin1 by infinite.animateFloat(
        0f, 360f,
        infiniteRepeatable(tween(46_000, easing = LinearEasing)), label = "spin1")
    val spin2 by infinite.animateFloat(
        360f, 0f,
        infiniteRepeatable(tween(71_000, easing = LinearEasing)), label = "spin2")
    val breath by infinite.animateFloat(
        0.55f, 1f,
        infiniteRepeatable(tween(2_600), repeatMode = RepeatMode.Reverse), label = "breath")
    val radar by infinite.animateFloat(
        0f, 1f,
        infiniteRepeatable(tween(2_400, easing = LinearEasing)), label = "radar")

    val pct by animateFloatAsState(
        targetValue = if (transferPct >= 0f) transferPct else 0f,
        label = "pct")

    Box(
        modifier = modifier
            .size(230.dp)
            .clickable(
                interactionSource = remember { MutableInteractionSource() },
                indication = null,
            ) { onTap() },
        contentAlignment = Alignment.Center,
    ) {
        Canvas(modifier = Modifier.fillMaxSize()) {
            val c = Offset(size.width / 2f, size.height / 2f)
            val r = size.minDimension / 2f

            // 雷达波纹：无设备时从洞口向外扩散
            if (searching) {
                val rr = r * (0.62f + 0.36f * radar)
                drawCircle(
                    color = Teal.copy(alpha = (1f - radar) * 0.28f),
                    radius = rr, center = c,
                    style = Stroke(width = 2.dp.toPx()),
                )
            }

            // 墨洞主体：墨黑核心 -> 暗青过渡 -> 青色光晕 -> 透明
            drawCircle(
                brush = Brush.radialGradient(
                    0.00f to Color.Black,
                    0.42f to Color(0xFF020807),
                    0.60f to Color(0xFF0A2A25).copy(alpha = 0.9f),
                    0.76f to Teal.copy(alpha = 0.40f * breath),
                    0.90f to Color(0xFF1E5046).copy(alpha = 0.15f),
                    1.00f to Color.Transparent,
                    center = c, radius = r * 0.94f,
                ),
                center = c, radius = r * 0.94f,
            )

            // 吸积弧·内层(顺时针) —— 不对称弧才看得出旋转
            val arcInset = r * 0.34f
            rotate(spin1, pivot = c) {
                drawArc(
                    color = TealSoft.copy(alpha = 0.30f * breath),
                    startAngle = 15f, sweepAngle = 105f, useCenter = false,
                    topLeft = Offset(c.x - (r - arcInset), c.y - (r - arcInset)),
                    size = Size((r - arcInset) * 2, (r - arcInset) * 2),
                    style = Stroke(width = 4.dp.toPx(), cap = StrokeCap.Round),
                )
                drawArc(
                    color = TealSoft.copy(alpha = 0.15f * breath),
                    startAngle = 195f, sweepAngle = 70f, useCenter = false,
                    topLeft = Offset(c.x - (r - arcInset) * 0.86f, c.y - (r - arcInset) * 0.86f),
                    size = Size((r - arcInset) * 1.72f, (r - arcInset) * 1.72f),
                    style = Stroke(width = 2.5f.dp.toPx(), cap = StrokeCap.Round),
                )
            }
            // 吸积弧·外层(逆时针，更淡更慢)
            val outInset = r * 0.16f
            rotate(spin2, pivot = c) {
                drawArc(
                    color = TealSoft.copy(alpha = 0.12f),
                    startAngle = 60f, sweepAngle = 140f, useCenter = false,
                    topLeft = Offset(c.x - (r - outInset), c.y - (r - outInset)),
                    size = Size((r - outInset) * 2, (r - outInset) * 2),
                    style = Stroke(width = 2.dp.toPx(), cap = StrokeCap.Round),
                )
            }

            // 传输进度环
            if (transferPct >= 0f) {
                val ringR = r * 0.97f
                drawCircle(
                    color = TealDim.copy(alpha = 0.5f),
                    radius = ringR, center = c,
                    style = Stroke(width = 5.dp.toPx()),
                )
                drawArc(
                    color = Teal,
                    startAngle = -90f, sweepAngle = 3.6f * pct, useCenter = false,
                    topLeft = Offset(c.x - ringR, c.y - ringR),
                    size = Size(ringR * 2, ringR * 2),
                    style = Stroke(width = 5.dp.toPx(), cap = StrokeCap.Round),
                )
            }
        }
    }
}

// ==================== 主界面 ====================

@Composable
fun MainScreen(
    statusMsg: String,
    peers: List<Peer>,
    selectedPeer: String?,
    receivedFiles: List<ReceivedFile>,
    transferPct: Float,
    transferKind: String,
    pendingCount: Int,
    onDeviceClick: (Peer) -> Unit,
    onSendClick: () -> Unit,
    onOpenInbox: () -> Unit,
    onOpenFile: (ReceivedFile) -> Unit,
    onClearHistory: () -> Unit,
    onSettingsClick: () -> Unit,
) {
    Scaffold(
        topBar = { InkTopBar(onSettingsClick) },
        containerColor = BgDark,
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 18.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            // 墨洞英雄区：轻点选文件发送
            InkHoleHero(
                transferPct = transferPct,
                searching = peers.isEmpty(),
                onTap = onSendClick,
            )

            // 状态文字(平滑切换)
            AnimatedContent(
                targetState = statusMsg,
                transitionSpec = { fadeIn(tween(220)) togetherWith fadeOut(tween(160)) },
                label = "status",
            ) { msg ->
                Text(
                    msg,
                    color = if (transferPct >= 0f) Teal else TextSecondary,
                    fontSize = 13.sp,
                    textAlign = TextAlign.Center,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            Text(
                when {
                    pendingCount > 0 && selectedPeer == null -> "有 $pendingCount 个待发文件 · 点选下方设备立刻发出"
                    selectedPeer != null -> "轻点墨洞选择文件 → 吞入即发送"
                    peers.isNotEmpty() -> "点选下方设备作为目标"
                    else -> "等待附近的墨洞上线…"
                },
                color = TextDim,
                fontSize = 11.sp,
                modifier = Modifier.padding(top = 3.dp),
            )

            Spacer(Modifier.height(14.dp))

            // 设备横排
            if (peers.isNotEmpty()) {
                LazyRow(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    items(peers, key = { it.serviceName.ifEmpty { it.name } }) { peer ->
                        DeviceChip(
                            peer = peer,
                            selected = peer.name == selectedPeer,
                            onClick = { onDeviceClick(peer) },
                        )
                    }
                }
                Spacer(Modifier.height(16.dp))
            }

            // 收件区
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("已吐出", color = TextSecondary, fontSize = 13.sp, fontWeight = FontWeight.Medium)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (receivedFiles.isNotEmpty()) {
                        IconButton(onClick = onClearHistory, modifier = Modifier.size(30.dp)) {
                            Icon(Icons.Outlined.DeleteSweep, contentDescription = "清空记录",
                                tint = TextDim, modifier = Modifier.size(17.dp))
                        }
                    }
                    TextButton(onClick = onOpenInbox) {
                        Icon(Icons.Outlined.Folder, contentDescription = null,
                            tint = Teal, modifier = Modifier.size(15.dp))
                        Spacer(Modifier.width(4.dp))
                        Text("下载目录", color = Teal, fontSize = 12.sp)
                    }
                }
            }
            if (receivedFiles.isEmpty()) {
                Text(
                    "还没有收到过文件",
                    color = TextDim, fontSize = 12.sp,
                    modifier = Modifier.padding(vertical = 18.dp),
                )
            } else {
                LazyColumn(
                    modifier = Modifier.weight(1f),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    items(receivedFiles, key = { "${it.time}-${it.name}" }) { rec ->
                        FileCard(rec) { onOpenFile(rec) }
                    }
                    item { Spacer(Modifier.height(12.dp)) }
                }
            }
        }
    }
}

// ==================== 子组件 ====================

@Composable
private fun InkTopBar(onSettingsClick: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 18.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("墨洞", color = TextPrimary, fontSize = 21.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.width(8.dp))
        Text("InkHole", color = TextDim, fontSize = 12.sp)
        Spacer(Modifier.weight(1f))
        IconButton(onClick = onSettingsClick) {
            Icon(Icons.Default.Settings, contentDescription = "设置", tint = TextSecondary)
        }
    }
}

/** 根据设备名猜测是手机还是电脑(仅影响图标)。 */
private fun looksLikePhone(name: String): Boolean {
    val n = name.lowercase()
    return listOf("pixel", "xiaomi", "redmi", "huawei", "honor", "oppo", "vivo",
        "oneplus", "samsung", "sm-", "iphone", "mi ", "meizu", "realme", "iqoo")
        .any { n.contains(it) }
}

@Composable
private fun DeviceChip(peer: Peer, selected: Boolean, onClick: () -> Unit) {
    val bg by animateColorAsState(
        if (selected) BgCardActive else BgCard, label = "chipBg")
    val borderC by animateColorAsState(
        if (selected) Teal.copy(alpha = 0.65f) else BorderColor, label = "chipBorder")

    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(22.dp))
            .background(bg)
            .border(1.dp, borderC, RoundedCornerShape(22.dp))
            .clickable { onClick() }
            .padding(horizontal = 14.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            if (looksLikePhone(peer.name)) Icons.Outlined.Smartphone else Icons.Outlined.Computer,
            contentDescription = null,
            tint = if (selected) Teal else TextSecondary,
            modifier = Modifier.size(16.dp),
        )
        Spacer(Modifier.width(8.dp))
        Column {
            Text(
                peer.name,
                color = if (selected) TextPrimary else TextSecondary,
                fontSize = 13.sp,
                fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
            )
            // 显示 instance_id 后4位（唯一标识，避免重名混淆）
            val instanceId = peer.serviceName.takeIf { it.contains("-") }
                ?.substringAfterLast("-")?.takeLast(4) ?: ""
            if (instanceId.isNotEmpty()) {
                Text(instanceId, color = TextDim, fontSize = 10.sp, maxLines = 1)
            }
        }
        if (selected) {
            Spacer(Modifier.width(8.dp))
            Box(
                Modifier
                    .size(7.dp)
                    .clip(CircleShape)
                    .background(Teal))
        }
    }
}

private fun fileEmoji(name: String): String {
    return when (name.substringAfterLast('.', "").lowercase()) {
        "jpg", "jpeg", "png", "gif", "webp", "heic", "bmp" -> "🖼️"
        "mp4", "mov", "mkv", "avi", "webm" -> "🎬"
        "mp3", "flac", "wav", "m4a", "ogg" -> "🎵"
        "pdf" -> "📕"
        "doc", "docx", "txt", "md" -> "📄"
        "xls", "xlsx", "csv" -> "📊"
        "ppt", "pptx" -> "📽️"
        "zip", "rar", "7z", "tar", "gz" -> "🗜️"
        "apk" -> "🤖"
        "py", "js", "ts", "kt", "java", "c", "cpp", "go", "rs", "html", "css" -> "💻"
        else -> "📦"
    }
}

private fun formatSize(bytes: Long): String = when {
    bytes <= 0 -> ""
    bytes < 1024 -> "${bytes}B"
    bytes < 1024 * 1024 -> "%.0fKB".format(bytes / 1024.0)
    bytes < 1024L * 1024 * 1024 -> "%.1fMB".format(bytes / 1024.0 / 1024)
    else -> "%.2fGB".format(bytes / 1024.0 / 1024 / 1024)
}

private fun formatTime(ts: Long): String {
    if (ts <= 0) return ""
    val diff = System.currentTimeMillis() - ts
    return when {
        diff < 60_000 -> "刚刚"
        diff < 3600_000 -> "${diff / 60_000} 分钟前"
        diff < 86400_000 -> "${diff / 3600_000} 小时前"
        diff < 2 * 86400_000 -> "昨天"
        else -> java.text.SimpleDateFormat("M月d日 HH:mm", java.util.Locale.CHINA)
            .format(java.util.Date(ts))
    }
}

@Composable
private fun FileCard(rec: ReceivedFile, onClick: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(BgCard)
            .border(1.dp, BorderColor, RoundedCornerShape(12.dp))
            .clickable { onClick() }
            .padding(horizontal = 13.dp, vertical = 11.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(fileEmoji(rec.name), fontSize = 18.sp)
        Spacer(Modifier.width(11.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                rec.name,
                color = TextPrimary, fontSize = 13.sp,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
            )
            Row {
                val meta = listOf(formatSize(rec.size), formatTime(rec.time))
                    .filter { it.isNotEmpty() }.joinToString(" · ")
                Text(meta, color = TextDim, fontSize = 10.5.sp, maxLines = 1)
            }
        }
        Spacer(Modifier.width(8.dp))
        Text("打开", color = Teal.copy(alpha = 0.8f), fontSize = 11.sp)
    }
}

// ==================== Preview 预览 ====================

@Preview(showBackground = true, backgroundColor = 0xFF05070A)
@Composable
fun PreviewMainScreen() {
    val mockPeers = listOf(
        Peer("MacBook Pro", "192.168.1.100", 8848),
        Peer("Pixel 8", "192.168.1.101", 8848),
    )
    InkHoleTheme {
        MainScreen(
            statusMsg = "↓ 年度报告.pdf 63%",
            peers = mockPeers,
            selectedPeer = "MacBook Pro",
            receivedFiles = listOf(
                ReceivedFile("年度报告.pdf", null, "application/pdf", 2_400_000,
                    System.currentTimeMillis() - 120_000),
                ReceivedFile("截图_001.png", null, "image/png", 890_000,
                    System.currentTimeMillis() - 7200_000),
            ),
            transferPct = 63f,
            transferKind = "recv",
            pendingCount = 0,
            onDeviceClick = {}, onSendClick = {}, onOpenInbox = {},
            onOpenFile = {}, onClearHistory = {}, onSettingsClick = {},
        )
    }
}

@Preview(showBackground = true, backgroundColor = 0xFF05070A, name = "空状态")
@Composable
fun PreviewEmptyState() {
    InkHoleTheme {
        MainScreen(
            statusMsg = "搜索设备中…",
            peers = emptyList(),
            selectedPeer = null,
            receivedFiles = emptyList(),
            transferPct = -1f,
            transferKind = "",
            pendingCount = 0,
            onDeviceClick = {}, onSendClick = {}, onOpenInbox = {},
            onOpenFile = {}, onClearHistory = {}, onSettingsClick = {},
        )
    }
}
