package com.rexvane.inkhole.p2p

import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject

/** 手动添加的设备(跨网/固定 IP 直连)。mDNS 组播不穿虚拟网卡(如 Tailscale)， */
/** 这些设备由用户填 IP+端口登记，由探活循环维持在线状态。 */
data class ManualPeer(val name: String, val host: String, val port: Int) {
    val key: String get() = "manual|$host|$port"
}

object ManualPeers {

    private const val PREF_KEY = "manual_peers"

    fun load(prefs: SharedPreferences): List<ManualPeer> {
        val raw = prefs.getString(PREF_KEY, null) ?: return emptyList()
        return try {
            val arr = JSONArray(raw)
            (0 until arr.length()).mapNotNull { i ->
                val o = arr.optJSONObject(i) ?: return@mapNotNull null
                val host = normalizeHost(o.optString("host")) ?: return@mapNotNull null
                val port = o.optInt("port")
                if (port !in 1..65535) null
                else ManualPeer(o.optString("name").trim(), host, port)
            }
        } catch (_: Exception) {
            emptyList()
        }
    }

    fun save(prefs: SharedPreferences, list: List<ManualPeer>) {
        val arr = JSONArray()
        list.forEach { m ->
            arr.put(JSONObject().put("name", m.name).put("host", m.host).put("port", m.port))
        }
        prefs.edit().putString(PREF_KEY, arr.toString()).apply()
    }

    /**
     * 输入时实时分段（经典 IP 输入框行为）：段满 3 位、或再接一位就超过 255
     * 时自动落点开新段——连续输入 1001274626 实时变成 100.127.46.26。
     * 含字母(主机名)不做掩码；四段封顶，第四段满后的多余数字丢弃。
     */
    fun maskTyping(text: String): String {
        if (text.any { it.isLetter() }) return text
        val parts = mutableListOf(StringBuilder())
        for (ch in text) {
            when {
                ch == '.' || ch == '。' || ch == '，' || ch == ',' || ch == ' ' ->
                    if (parts.last().isNotEmpty() && parts.size < 4) parts.add(StringBuilder())
                ch.isDigit() -> {
                    val cand = parts.last().toString() + ch
                    when {
                        (cand.length > 3 || cand.toInt() > 255) && parts.size < 4 ->
                            parts.add(StringBuilder().append(ch))
                        cand.length <= 3 -> {
                            parts.last().clear()
                            parts.last().append(cand)
                        }
                    }
                }
            }
        }
        return parts.joinToString(".") { it.toString() }
    }

    /**
     * IP 输入自动纠正。与桌面端同一套规则：
     *  - 全角句号/逗号/空格视为分隔符（输入法常见误输）；
     *  - 含字母则按主机名原样放行（如 Tailscale MagicDNS 名）；
     *  - 缺分隔符的数字段尝试唯一合法拆分：100127.46.26 -> 100.127.46.26；
     *  - 有歧义或非法返回 null。
     */
    fun normalizeHost(raw: String): String? {
        var s = raw.trim()
        if (s.isEmpty()) return null
        if (s.any { it.isLetter() }) {
            val host = s.filterNot { it.isWhitespace() }.trimEnd('.')
            if (host.isEmpty() || host.length > 253) return null
            val labels = host.split('.')
            if (labels.any { label ->
                    label.isEmpty() || label.length > 63 ||
                        label.startsWith('-') || label.endsWith('-') ||
                        label.any { !it.isLetterOrDigit() && it != '-' }
                }) return null
            return host
        }
        s = s.replace('。', '.').replace('，', '.').replace(',', '.').replace(' ', '.')
        s = s.replace(Regex("\\.+"), ".").trim('.')
        if (s.isEmpty() || !s.matches(Regex("[0-9.]+"))) return null
        val segments = s.split('.')
        if (segments.size > 4 || segments.any { it.isEmpty() }) return null

        // 逐段拆分:每段拆成若干 1-3 位、数值 0-255、无前导零的片段,总数恰为 4,
        // 且全局唯一解才接受(有歧义宁可报错,绝不猜错 IP)
        val solutions = mutableListOf<List<String>>()
        fun search(segIndex: Int, acc: List<String>) {
            if (solutions.size > 1) return                    // 已有多解,提前剪枝
            if (segIndex == segments.size) {
                if (acc.size == 4) solutions.add(acc)
                return
            }
            val seg = segments[segIndex]
            fun cut(pos: Int, parts: List<String>) {
                if (solutions.size > 1) return
                if (pos == seg.length) {
                    search(segIndex + 1, acc + parts)
                    return
                }
                for (len in 1..3) {
                    if (pos + len > seg.length) break
                    val piece = seg.substring(pos, pos + len)
                    if (piece.length > 1 && piece[0] == '0') continue   // 前导零
                    if (piece.toInt() > 255) continue
                    if (acc.size + parts.size + 1 > 4) continue
                    cut(pos + len, parts + piece)
                }
            }
            cut(0, emptyList())
        }
        search(0, emptyList())
        return if (solutions.size == 1) solutions[0].joinToString(".") else null
    }
}
