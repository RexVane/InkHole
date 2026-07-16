package com.rexvane.inkhole.p2p

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class WHPPTest {
    @Test
    fun headerRoundTripPreservesProtocolFields() {
        val expected = WHPP.Header(
            filename = "report.txt",
            size = 1234,
            encrypted = true,
            wantAck = true,
            encMode = "chunked",
        )
        val output = ByteArrayOutputStream()
        WHPP.writeHeader(output, expected)
        assertEquals(expected, WHPP.readHeader(ByteArrayInputStream(output.toByteArray())))
    }

    @Test
    fun oversizedHeaderIsRejectedBeforeAllocation() {
        val output = ByteArrayOutputStream()
        DataOutputStream(output).apply {
            write(WHPP.MAGIC)
            writeInt(WHPP.MAX_HEADER + 1)
        }
        assertThrows(IllegalArgumentException::class.java) {
            WHPP.readHeader(ByteArrayInputStream(output.toByteArray()))
        }
    }
}
