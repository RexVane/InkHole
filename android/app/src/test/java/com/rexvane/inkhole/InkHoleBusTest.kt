package com.rexvane.inkhole

import com.rexvane.inkhole.transport.TransportEventListener
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class InkHoleBusTest {
    @Test
    fun transportEventsAreReplayedAfterActivityRecreation() {
        val first = mutableListOf<String>()
        val firstListener = TransportEventListener { event, _ -> first += event }
        InkHoleBus.attachTransportListener(firstListener)
        InkHoleBus.dispatchTransportEvent("wormhole.code", JSONObject().put("code", "1-a-b"))
        InkHoleBus.detachTransportListener(firstListener)

        InkHoleBus.dispatchTransportEvent(
            "wormhole.ready", JSONObject().put("session_id", "wh-1"))

        val replayed = mutableListOf<Pair<String, String>>()
        val secondListener = TransportEventListener { event, data ->
            replayed += event to data.optString("session_id")
        }
        InkHoleBus.attachTransportListener(secondListener)
        InkHoleBus.detachTransportListener(secondListener)

        assertEquals(listOf("wormhole.code"), first)
        assertEquals(listOf("wormhole.ready" to "wh-1"), replayed)
    }
}
