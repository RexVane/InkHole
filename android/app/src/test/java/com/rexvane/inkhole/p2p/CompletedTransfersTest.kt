package com.rexvane.inkhole.p2p

import java.io.File
import java.nio.file.Files
import java.security.MessageDigest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CompletedTransfersTest {
    private fun metadata(payload: ByteArray, kind: String = "file") = JSONObject().apply {
        put("version", WHPP.PROTOCOL_VERSION)
        put("filename", "received.txt")
        put("plain_size", payload.size.toLong())
        put("sha256", MessageDigest.getInstance("SHA-256").digest(payload)
            .joinToString("") { "%02x".format(it) })
        put("kind", kind)
        put("mtime_ms", 0L)
        put("sender_instance_id", "b".repeat(32))
        put("sender_fingerprint", "c".repeat(64))
    }

    private fun journal(metadata: JSONObject, destination: File) =
        JSONObject(metadata.toString()).apply {
            put("path", destination.absolutePath)
            put("completed_at", 123L)
        }

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

    @Test
    fun recoversPublishedFileOnlyWhenDigestStillMatches() {
        val root = Files.createTempDirectory("inkhole-commit-file").toFile()
        try {
            val payload = "verified-content".toByteArray()
            val expected = metadata(payload)
            val destination = root.resolve("received.txt").apply { writeBytes(payload) }
            val commit = journal(expected, destination)
            val part = root.resolve("checkpoint.part")

            assertNotNull(ReceiveCommits.recover(root, commit, part, expected, false))
            destination.writeBytes("x".repeat(payload.size).toByteArray())
            assertNull(ReceiveCommits.recover(root, commit, part, expected, false))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun rejectsCommitDestinationOutsideInbox() {
        val root = Files.createTempDirectory("inkhole-commit-root").toFile()
        val outside = Files.createTempFile("inkhole-commit-outside", ".txt").toFile()
        try {
            val payload = "outside".toByteArray()
            outside.writeBytes(payload)
            val expected = metadata(payload)
            assertNull(ReceiveCommits.recover(
                root, journal(expected, outside), root.resolve("checkpoint.part"),
                expected, false))
        } finally {
            root.deleteRecursively()
            outside.delete()
        }
    }

    @Test
    fun folderRecoveryRequiresVerifiedWhfCheckpoint() {
        val root = Files.createTempDirectory("inkhole-commit-folder").toFile()
        try {
            val payload = "WHF1-checkpoint".toByteArray()
            val expected = metadata(payload, WHPP.FOLDER_KIND)
            val destination = root.resolve("received.txt").apply { mkdir() }
            val part = root.resolve("checkpoint.part").apply { writeBytes(payload) }
            val commit = journal(expected, destination)

            assertNotNull(ReceiveCommits.recover(root, commit, part, expected, true))
            part.writeBytes("x".repeat(payload.size).toByteArray())
            assertNull(ReceiveCommits.recover(root, commit, part, expected, true))
        } finally {
            root.deleteRecursively()
        }
    }
}
