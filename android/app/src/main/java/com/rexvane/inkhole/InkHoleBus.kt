package com.rexvane.inkhole

import android.annotation.SuppressLint
import android.content.Context
import android.net.Uri
import com.rexvane.inkhole.p2p.Peer
import com.rexvane.inkhole.p2p.InkHoleListener
import com.rexvane.inkhole.p2p.InkHoleNode
import com.rexvane.inkhole.transport.TransportEventListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList

/** 一条已接收的文件记录。uri 用于从 UI/通知打开(MediaStore 或 FileProvider)。 */
data class ReceivedFile(
    val name: String,
    val uri: Uri?,
    val mime: String,
    val size: Long = 0,
    val time: Long = 0,
    val transferId: String = "",
)

/**
 * Service(拥有 P2P 节点) 与 Activity(纯 UI) 之间的桥。
 *
 * 节点生命周期归 InkHoleService：锁屏/转屏/切后台都不断线。
 * Activity 重建时从这里恢复最近状态，并把自己挂到 uiListener 接收后续事件。
 * 接收历史持久化到 SharedPreferences(最近 50 条)，重启 App 不丢。
 */
object InkHoleBus {
    // 节点只持有 applicationContext，并在 Service.onDestroy 中清空。
    @SuppressLint("StaticFieldLeak")
    @Volatile var node: InkHoleNode? = null
    @Volatile var uiListener: InkHoleListener? = null
    @Volatile private var transportListener: TransportEventListener? = null

    private data class PendingTransportEvent(val event: String, val data: String)
    private val pendingTransportEvents = ArrayDeque<PendingTransportEvent>()
    private const val MAX_PENDING_TRANSPORT_EVENTS = 32

    // 最近状态缓存：Activity 重建时恢复 UI 用
    @Volatile var lastPeers: List<Peer> = emptyList()
    @Volatile var lastStatus: String = "正在启动…"
    // 设置变更重建节点时暂存选中目标的 serviceName，新节点起来后自动恢复选中
    @Volatile var pendingSelectedService: String? = null
    val receivedFiles = CopyOnWriteArrayList<ReceivedFile>()   // 最新的在最前

    /**
     * TransportManager belongs to the foreground service, while the Activity can disappear
     * briefly during rotation or while the app is backgrounded. Retain the small control
     * events so a ready tunnel or receive offer cannot be lost between Activity instances.
     */
    @Synchronized
    fun attachTransportListener(listener: TransportEventListener) {
        transportListener = listener
        val replay = pendingTransportEvents.toList()
        pendingTransportEvents.clear()
        replay.forEach { pending ->
            listener.onTransportEvent(pending.event, JSONObject(pending.data))
        }
    }

    @Synchronized
    fun detachTransportListener(listener: TransportEventListener) {
        if (transportListener === listener) transportListener = null
    }

    @Synchronized
    fun dispatchTransportEvent(event: String, data: JSONObject) {
        val current = transportListener
        if (current != null) {
            current.onTransportEvent(event, JSONObject(data.toString()))
            return
        }
        if (pendingTransportEvents.size >= MAX_PENDING_TRANSPORT_EVENTS) {
            pendingTransportEvents.removeFirst()
        }
        pendingTransportEvents.addLast(PendingTransportEvent(event, data.toString()))
    }

    private const val HISTORY_KEY = "history"
    private const val HISTORY_MAX = 50

    @Synchronized
    fun loadHistory(context: Context) {
        if (receivedFiles.isNotEmpty()) return
        try {
            val raw = context.getSharedPreferences("inkhole", Context.MODE_PRIVATE)
                .getString(HISTORY_KEY, null) ?: return
            val arr = JSONArray(raw)
            val loaded = ArrayList<ReceivedFile>(arr.length())
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                loaded.add(ReceivedFile(
                    name = o.getString("name"),
                    uri = o.optString("uri", "").takeIf { it.isNotEmpty() }?.let(Uri::parse),
                    mime = o.optString("mime", "application/octet-stream"),
                    size = o.optLong("size", 0),
                    time = o.optLong("time", 0),
                    transferId = o.optString("transfer_id", ""),
                ))
            }
            receivedFiles.addAll(loaded)
        } catch (_: Exception) {
        }
    }

    @Synchronized
    fun saveHistory(context: Context) {
        try {
            val arr = JSONArray()
            receivedFiles.take(HISTORY_MAX).forEach { r ->
                arr.put(JSONObject().apply {
                    put("name", r.name)
                    put("uri", r.uri?.toString() ?: "")
                    put("mime", r.mime)
                    put("size", r.size)
                    put("time", r.time)
                    if (r.transferId.isNotEmpty()) put("transfer_id", r.transferId)
                })
            }
            context.getSharedPreferences("inkhole", Context.MODE_PRIVATE)
                .edit().putString(HISTORY_KEY, arr.toString()).apply()
        } catch (_: Exception) {
        }
    }

    @Synchronized
    fun recordReceived(context: Context, record: ReceivedFile) {
        val merged = mergeHistory(receivedFiles, record)
        receivedFiles.clear()
        receivedFiles.addAll(merged)
        saveHistory(context)
    }

    internal fun mergeHistory(existing: List<ReceivedFile>, record: ReceivedFile): List<ReceivedFile> {
        val previous = if (record.transferId.isEmpty()) existing else {
            existing.filterNot { it.transferId == record.transferId }
        }
        return (listOf(record) + previous).take(HISTORY_MAX)
    }

    @Synchronized
    fun clearHistory(context: Context) {
        receivedFiles.clear()
        saveHistory(context)
    }
}
