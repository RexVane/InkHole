package com.rexvane.inkhole.p2p

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.EOFException
import java.io.InterruptedIOException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
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

    @Test
    fun shortDataStreamIsRejectedInsteadOfProducingPartialFrame() {
        assertThrows(EOFException::class.java) {
            WHPP.writeFrame(
                ByteArrayOutputStream(), "short.bin", 8, false,
                ByteArrayInputStream(byteArrayOf(1, 2, 3)),
            )
        }
    }

    @Test
    fun cancellationStopsFrameBetweenTransferChunks() {
        val size = WHPP.BUFFER_SIZE * 2
        val output = ByteArrayOutputStream()
        var checks = 0

        assertThrows(InterruptedIOException::class.java) {
            WHPP.writeFrame(
                output, "cancel.bin", size.toLong(), false,
                ByteArrayInputStream(ByteArray(size)),
                shouldCancel = { ++checks > 1 },
            )
        }

        assertEquals(2, checks)
        assertTrue(output.size() < size)
    }
}
