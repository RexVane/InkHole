package com.rexvane.wormhole

import android.net.Uri
import com.rexvane.wormhole.p2p.Peer
import com.rexvane.wormhole.p2p.WormholeListener
import com.rexvane.wormhole.p2p.WormholeNode
import java.util.concurrent.CopyOnWriteArrayList

/** 一条已接收的文件记录。uri 用于从 UI/通知打开(MediaStore 或 FileProvider)。 */
data class ReceivedFile(val name: String, val uri: Uri?, val mime: String)

/**
 * Service(拥有 P2P 节点) 与 Activity(纯 UI) 之间的桥。
 *
 * 节点生命周期归 WormholeService：锁屏/转屏/切后台都不断线。
 * Activity 重建时从这里恢复最近状态，并把自己挂到 uiListener 接收后续事件。
 */
object WormholeBus {
    @Volatile var node: WormholeNode? = null
    @Volatile var uiListener: WormholeListener? = null

    // 最近状态缓存：Activity 重建时恢复 UI 用
    @Volatile var lastPeers: List<Peer> = emptyList()
    @Volatile var lastStatus: String = "正在启动…"
    val receivedFiles = CopyOnWriteArrayList<ReceivedFile>()   // 最新的在最前
}
