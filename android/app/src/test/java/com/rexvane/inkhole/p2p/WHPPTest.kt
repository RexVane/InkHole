package com.rexvane.inkhole.p2p

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.json.JSONObject

class WHPPTest {
    @Test
    fun duplicateCheckpointWaitsUntilActiveTransactionFinishes() {
        val gate = CheckpointGate()
        assertTrue(gate.acquire("transfer", 1_000))
        val attempting = CountDownLatch(1)
        val finished = CountDownLatch(1)
        val acquired = AtomicBoolean(false)
        val waiter = Thread {
            attempting.countDown()
            acquired.set(gate.acquire("transfer", 1_000))
            finished.countDown()
        }.apply { start() }

        assertTrue(attempting.await(1, TimeUnit.SECONDS))
        assertFalse(finished.await(100, TimeUnit.MILLISECONDS))
        gate.release("transfer")
        assertTrue(finished.await(1, TimeUnit.SECONDS))
        assertTrue(acquired.get())
        gate.release("transfer")
        waiter.join(1_000)
    }

    @Test
    fun completionReceiptDoesNotDependOnExportedPath() {
        val expected = JSONObject().apply {
            put("version", WHPP.PROTOCOL_VERSION)
            put("filename", "exported.txt")
            put("plain_size", 12L)
            put("sha256", "a".repeat(64))
            put("kind", "file")
            put("mtime_ms", 0L)
            put("sender_instance_id", "b".repeat(32))
            put("sender_fingerprint", "c".repeat(64))
        }
        val receipt = JSONObject(expected.toString()).apply {
            put("path", "/private/inbox/already-exported-and-removed.txt")
            put("completed_at", 123L)
        }

        assertTrue(WHPP.metadataMatches(receipt, expected))
        receipt.put("sha256", "d".repeat(64))
        assertFalse(WHPP.metadataMatches(receipt, expected))
    }

    @Test
    fun deviceAuthMessagesMatchDesktopVectors() {
        val nonce = ByteArray(32) { it.toByte() }
        val instanceId = "0123456789abcdef0123456789abcdef"
        val header = WHPP.Header(
            filename = "测试.txt",
            plainSize = 123456789,
            transferId = "a".repeat(64),
            sha256 = "b".repeat(64),
            encrypted = true,
            kind = "file",
            modifiedMs = 1_700_000_000_000,
            senderInstanceId = instanceId,
            senderPublicKey = "unused",
        )
        fun digest(value: ByteArray) = DeviceAuth.hex(
            MessageDigest.getInstance("SHA-256").digest(value))

        assertEquals(
            "c74fb2982e30e6b84c7c187377638e20fef5df26a586564347c1a1bd8a9b82bb",
            digest(DeviceAuth.capabilityMessage(
                nonce, instanceId, "安卓", WHPP.CAP_VERSION,
                listOf(WHPP.FOLDER_KIND, WHPP.RELIABLE_KIND))),
        )
        assertEquals(
            "2350202421a349a8078d1d14d82b426078fb0b2335bac7174c29004c7df4a29f",
            digest(DeviceAuth.transferMessage(nonce, header, 4096)),
        )
        assertEquals(
            "0280143df64be4022b9f6c4bd4a9e8efb7b3207bfc598c37050abff2301942a1",
            digest(DeviceAuth.receiverMessage(nonce, header, 4096, "f".repeat(32))),
        )
    }

    @Test
    fun headerRoundTripPreservesProtocolFields() {
        val expected = WHPP.Header(
            filename = "report.txt",
            plainSize = 1200,
            transferId = "a".repeat(64),
            sha256 = "b".repeat(64),
            encrypted = true,
            wantAck = true,
            encMode = "chunked",
            kind = WHPP.FOLDER_KIND,
            modifiedMs = 1_700_000_000_000,
            senderInstanceId = "0123456789abcdef0123456789abcdef",
            senderPublicKey = "public-key",
        )
        val output = ByteArrayOutputStream()
        WHPP.writeHeader(output, expected)
        assertEquals(expected, WHPP.readHeader(ByteArrayInputStream(output.toByteArray())))
    }

    @Test
    fun capabilityResponseRejectsLegacyVersion() {
        val body = """{"version":1,"caps":["folder-v1"]}"""
            .toByteArray(Charsets.UTF_8)
        val output = ByteArrayOutputStream()
        DataOutputStream(output).apply {
            write(WHPP.CAP_MAGIC)
            writeInt(body.size)
            write(body)
        }

        assertThrows(IOException::class.java) {
            WHPP.readCapabilities(
                ByteArrayInputStream(output.toByteArray()), ByteArray(32))
        }
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
