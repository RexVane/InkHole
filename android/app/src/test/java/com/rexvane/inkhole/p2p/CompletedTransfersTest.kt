package com.rexvane.inkhole.p2p

import java.nio.file.Files
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CompletedTransfersTest {
    @Test
    fun findsOnlyExistingDestinationsInsidePrivateInbox() {
        val root = Files.createTempDirectory("inkhole-completed").toFile()
        val outside = Files.createTempFile("inkhole-outside", ".txt").toFile()
        try {
            val destination = root.resolve("received.txt").apply { writeText("done") }
            val transferId = "a".repeat(64)
            root.resolve(".inkhole-$transferId.done.json").writeText(
                JSONObject().apply {
                    put("filename", destination.name)
                    put("path", destination.absolutePath)
                    put("completed_at", 123L)
                }.toString(),
            )
            val outsideId = "b".repeat(64)
            root.resolve(".inkhole-$outsideId.done.json").writeText(
                JSONObject().apply {
                    put("filename", outside.name)
                    put("path", outside.absolutePath)
                    put("completed_at", 122L)
                }.toString(),
            )

            val pending = CompletedTransfers.pending(root)
            assertEquals(1, pending.size)
            assertEquals(transferId, pending.single().transferId)
            assertEquals(destination.canonicalPath, pending.single().path)

            destination.delete()
            assertTrue(CompletedTransfers.pending(root).isEmpty())
        } finally {
            root.deleteRecursively()
            outside.delete()
        }
    }
}
