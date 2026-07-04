package com.rexvane.inkhole

import android.content.Context
import android.net.Uri
import com.rexvane.inkhole.p2p.Peer
import com.rexvane.inkhole.p2p.InkHoleListener
import com.rexvane.inkhole.p2p.InkHoleNode
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
)

/**
 * Service(拥有 P2P 节点) 与 Activity(纯 UI) 之间的桥。
 *
 * 节点生命周期归 InkHoleService：锁屏/转屏/切后台都不断线。
 * Activity 重建时从这里恢复最近状态，并把自己挂到 uiListener 接收后续事件。
 * 接收历史持久化到 SharedPreferences(最近 50 条)，重启 App 不丢。
 */
object InkHoleBus {
    @Volatile var node: InkHoleNode? = null
    @Volatile var uiListener: InkHoleListener? = null

    // 最近状态缓存：Activity 重建时恢复 UI 用
    @Volatile var lastPeers: List<Peer> = emptyList()
    @Volatile var lastStatus: String = "正在启动…"
    val receivedFiles = CopyOnWriteArrayList<ReceivedFile>()   // 最新的在最前

    private const val HISTORY_KEY = "history"
    private const val HISTORY_MAX = 50

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
                ))
            }
            receivedFiles.addAll(loaded)
        } catch (_: Exception) {
        }
    }

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
                })
            }
            context.getSharedPreferences("inkhole", Context.MODE_PRIVATE)
                .edit().putString(HISTORY_KEY, arr.toString()).apply()
        } catch (_: Exception) {
        }
    }

    fun clearHistory(context: Context) {
        receivedFiles.clear()
        saveHistory(context)
    }
}
