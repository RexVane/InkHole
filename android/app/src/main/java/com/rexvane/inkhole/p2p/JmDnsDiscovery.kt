package com.rexvane.inkhole.p2p

import android.util.Log
import java.net.InetAddress
import javax.jmdns.JmDNS
import javax.jmdns.ServiceEvent
import javax.jmdns.ServiceListener

internal data class JmDnsServiceRecord(
    val name: String,
    val port: Int,
    val addresses: List<String>,
    val attributes: Map<String, String>,
)

/** Explicit-interface mDNS browser for hotspot networks omitted by Android's NsdManager. */
internal class JmDnsDiscovery(
    private val serviceType: String,
    private val onResolved: (JmDnsServiceRecord) -> Unit,
    private val onLost: (String) -> Unit,
) {
    companion object {
        private const val TAG = "InkHoleJmDns"
    }

    private val monitor = Any()
    private val instances = LinkedHashMap<String, JmDNS>()
    private var stopped = false

    fun restart(bindAddresses: List<InetAddress>, force: Boolean = false) {
        val desired = bindAddresses.mapNotNull { address ->
            address.hostAddress?.let { host -> host to address }
        }.distinctBy { it.first }.toMap()
        synchronized(monitor) {
            if (stopped || (!force && instances.keys == desired.keys)) return
            closeLocked()
            desired.forEach { (host, address) ->
                try {
                    val jmDns = JmDNS.create(address, "InkHole-$host")
                    jmDns.addServiceListener(serviceType, listener())
                    instances[host] = jmDns
                } catch (error: Exception) {
                    Log.w(TAG, "Unable to browse mDNS on $host", error)
                }
            }
        }
    }

    fun stop() {
        synchronized(monitor) {
            stopped = true
            closeLocked()
        }
    }

    private fun closeLocked() {
        instances.values.forEach { jmDns ->
            try {
                jmDns.close()
            } catch (error: Exception) {
                Log.w(TAG, "Unable to close mDNS browser", error)
            }
        }
        instances.clear()
    }

    private fun listener(): ServiceListener = object : ServiceListener {
        override fun serviceAdded(event: ServiceEvent) {
            try {
                event.dns.requestServiceInfo(event.type, event.name, true)
            } catch (error: Exception) {
                Log.w(TAG, "Unable to resolve ${event.name}", error)
            }
        }

        override fun serviceRemoved(event: ServiceEvent) {
            onLost(event.name)
        }

        override fun serviceResolved(event: ServiceEvent) {
            val info = event.info ?: return
            val addresses = info.inetAddresses.mapNotNull { it.hostAddress }.distinct()
            if (addresses.isEmpty() || info.port !in 1..65535) return
            val attributes = LinkedHashMap<String, String>()
            val names = info.propertyNames
            while (names.hasMoreElements()) {
                val name = names.nextElement()
                info.getPropertyString(name)?.let { attributes[name] = it }
            }
            onResolved(JmDnsServiceRecord(info.name, info.port, addresses, attributes))
        }
    }
}
