package com.rexvane.inkhole

import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.Send
import androidx.compose.material.icons.outlined.ContentCopy
import androidx.compose.material.icons.outlined.Download
import androidx.compose.material.icons.outlined.Key
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONObject

@Composable
fun CrossNetworkActionsDialog(
    joining: Boolean,
    sshReady: Boolean,
    initialReceiveCode: String,
    initialPairCode: String,
    onDismiss: () -> Unit,
    onOneTimeSend: () -> Unit,
    onJoinOneTime: (String) -> Unit,
    onCreateSSHPair: () -> Unit,
    onJoinSSHPair: (String) -> Unit,
) {
    var receiveCode by remember(initialReceiveCode) { mutableStateOf(initialReceiveCode) }
    var pairCode by remember(initialPairCode) { mutableStateOf(initialPairCode) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("跨网络传输") },
        text = {
            Column(
                modifier = Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Button(onClick = onOneTimeSend, modifier = Modifier.fillMaxWidth()) {
                    Icon(Icons.AutoMirrored.Outlined.Send, contentDescription = null)
                    Spacer(Modifier.width(7.dp))
                    Text("选择内容并生成短码")
                }
                OutlinedTextField(
                    value = receiveCode,
                    onValueChange = { receiveCode = it.trimStart().take(160) },
                    label = { Text("一次性接收短码") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedButton(
                    onClick = { onJoinOneTime(receiveCode.trim()) },
                    enabled = receiveCode.isNotBlank() && !joining,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Icon(Icons.Outlined.Download, contentDescription = null)
                    Spacer(Modifier.width(7.dp))
                    Text(if (joining) "正在连接…" else "连接并接收")
                }
                HorizontalDivider(modifier = Modifier.padding(vertical = 5.dp))
                Text("SSH 中继配对", fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
                OutlinedButton(
                    onClick = onCreateSSHPair,
                    enabled = sshReady,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Icon(Icons.Outlined.Key, contentDescription = null)
                    Spacer(Modifier.width(7.dp))
                    Text("生成配对码")
                }
                OutlinedTextField(
                    value = pairCode,
                    onValueChange = { pairCode = it.trimStart().take(180) },
                    label = { Text("SSH 配对码") },
                    singleLine = true,
                    enabled = sshReady,
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedButton(
                    onClick = { onJoinSSHPair(pairCode.trim()) },
                    enabled = sshReady && pairCode.isNotBlank(),
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("加入配对") }
            }
        },
        confirmButton = { TextButton(onClick = onDismiss) { Text("关闭") } },
    )
}

@Composable
fun ShortCodeDisplayDialog(
    title: String,
    code: String,
    uri: String,
    connected: Boolean,
    onCopy: () -> Unit,
    onDismiss: () -> Unit,
) {
    val qr = remember(uri) { createQrBitmap(uri.ifBlank { code }) }
    val compactHeight = LocalConfiguration.current.screenHeightDp < 500
    val qrSize = if (compactHeight) 96.dp else 196.dp
    val contentGap = if (compactHeight) 4.dp else 12.dp
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            Column(
                modifier = Modifier.verticalScroll(rememberScrollState()),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Image(
                    bitmap = qr.asImageBitmap(),
                    contentDescription = "配对二维码",
                    modifier = Modifier.size(qrSize),
                )
                Spacer(Modifier.height(contentGap))
                androidx.compose.foundation.text.selection.SelectionContainer {
                    Text(
                        code,
                        fontFamily = FontFamily.Monospace,
                        fontSize = if (compactHeight) 15.sp else 17.sp,
                        fontWeight = FontWeight.SemiBold,
                        textAlign = TextAlign.Center,
                    )
                }
                Spacer(Modifier.height(if (compactHeight) 4.dp else 10.dp))
                if (connected) {
                    Text("已安全配对", fontSize = 12.sp)
                } else {
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onCopy) {
                Icon(Icons.Outlined.ContentCopy, contentDescription = null)
                Spacer(Modifier.width(5.dp))
                Text("复制短码")
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("关闭") } },
    )
}

@Composable
fun WormholeOfferDialog(
    data: JSONObject,
    onAccept: () -> Unit,
    onReject: () -> Unit,
) {
    val summary = data.optJSONObject("summary") ?: JSONObject()
    val names = summary.optJSONArray("names")
    AlertDialog(
        onDismissRequest = onReject,
        title = { Text("接收一次性传输") },
        text = {
            Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                Text(summary.optString("device_name", "未知设备"), fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(6.dp))
                Text(
                    "${summary.optInt("item_count")} 项 · ${formatTransferBytes(summary.optLong("total_bytes"))}",
                    fontSize = 12.sp,
                )
                if (names != null && names.length() > 0) {
                    Spacer(Modifier.height(10.dp))
                    for (index in 0 until names.length()) {
                        Text(names.optString(index), fontSize = 12.sp)
                    }
                }
            }
        },
        confirmButton = { TextButton(onClick = onAccept) { Text("接收") } },
        dismissButton = { TextButton(onClick = onReject) { Text("拒绝") } },
    )
}

@Composable
fun FingerprintConfirmDialog(
    data: JSONObject,
    onConfirm: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    val fingerprint = data.optString("fingerprint")
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("确认 VPS 主机指纹") },
        text = {
            Column {
                Text(data.optString("server_version"), fontSize = 12.sp)
                Spacer(Modifier.height(10.dp))
                androidx.compose.foundation.text.selection.SelectionContainer {
                    Text(fingerprint, fontFamily = FontFamily.Monospace, fontSize = 12.sp)
                }
            }
        },
        confirmButton = {
            TextButton(onClick = { onConfirm(fingerprint) }) { Text("确认并固定") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

private fun formatTransferBytes(bytes: Long): String = when {
    bytes < 1024 -> "$bytes B"
    bytes < 1024 * 1024 -> "%.1f KB".format(bytes / 1024.0)
    bytes < 1024L * 1024 * 1024 -> "%.1f MB".format(bytes / 1024.0 / 1024)
    else -> "%.2f GB".format(bytes / 1024.0 / 1024 / 1024)
}
