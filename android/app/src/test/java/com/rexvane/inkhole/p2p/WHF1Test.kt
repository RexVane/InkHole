package com.rexvane.inkhole.p2p

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.assertThrows
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class WHF1Test {
    @get:Rule
    val temp = TemporaryFolder()

    private fun payload(vararg entries: Entry): ByteArray {
        val output = ByteArrayOutputStream()
        DataOutputStream(output).apply {
            write(WHF1.MAGIC)
            writeInt(entries.size)
            entries.forEach { entry ->
                val path = entry.path.toByteArray(Charsets.UTF_8)
                writeByte(if (entry.directory) WHF1.TYPE_DIRECTORY else WHF1.TYPE_FILE)
                writeInt(path.size)
                writeLong(if (entry.directory) 0 else entry.content.size.toLong())
                writeLong(0)
                write(path)
                if (!entry.directory) write(entry.content)
            }
        }
        return output.toByteArray()
    }

    private data class Entry(
        val path: String,
        val content: ByteArray = ByteArray(0),
        val directory: Boolean = false,
    )

    @Test
    fun receivePreservesNestedAndEmptyDirectories() {
        val bytes = payload(
            Entry("docs", directory = true),
            Entry("docs/readme.txt", "hello".toByteArray()),
            Entry("empty", directory = true),
        )
        val staging = temp.newFolder("staging")
        val result = WHF1.receive(ByteArrayInputStream(bytes), bytes.size.toLong(), staging)

        assertEquals(3, result.entryCount)
        assertEquals(1, result.fileCount)
        assertEquals(5, result.fileBytes)
        assertTrue(File(staging, "docs/readme.txt").readText() == "hello")
        assertTrue(File(staging, "empty").isDirectory)
    }

    @Test
    fun chunkedEncryptedFolderStreamsIntoParser() {
        val plain = payload(Entry("data.bin", ByteArray(4096) { (it % 251).toByte() }))
        val encryptor = Crypto.ChunkedEncryptor("folder-secret")
        val ciphertext = encryptor.encryptChunk(plain)
        val wire = ByteArrayOutputStream().also { output ->
            DataOutputStream(output).apply {
                write(encryptor.streamHeader)
                writeInt(ciphertext.size)
                write(ciphertext)
            }
        }.toByteArray()
        val staging = temp.newFolder("encrypted")
        val input = ChunkedFolderInputStream(
            ByteArrayInputStream(wire), wire.size.toLong(), plain.size.toLong(),
            "folder-secret", {})

        val result = WHF1.receive(input, plain.size.toLong(), staging)
        input.verifyComplete()
        assertEquals(1, result.fileCount)
        assertEquals(4096, File(staging, "data.bin").length())
    }

    @Test
    fun traversalAndCaseCollisionAreRejected() {
        val staging = temp.newFolder("staging")
        val traversal = payload(Entry("../escape.txt", "bad".toByteArray()))
        assertThrows(IllegalArgumentException::class.java) {
            WHF1.receive(ByteArrayInputStream(traversal), traversal.size.toLong(), staging)
        }
        assertTrue(staging.listFiles().isNullOrEmpty())

        val collision = payload(
            Entry("A.txt", "a".toByteArray()),
            Entry("a.txt", "b".toByteArray()),
        )
        assertThrows(IllegalArgumentException::class.java) {
            WHF1.receive(ByteArrayInputStream(collision), collision.size.toLong(), staging)
        }
    }
}
