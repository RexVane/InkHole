package com.rexvane.inkhole.p2p

/** Common transport surface for mutually exclusive LAN and relay engines. */
interface TransportNode {
    fun start()
    fun stop()
    fun sendFile(filePath: String): Boolean
    fun getPeers(): List<Peer>
    fun selectPeer(name: String?)
    fun getSelectedPeer(): String?
    fun getSelectedServiceName(): String?
    fun restoreSelectedService(serviceName: String?)
}
