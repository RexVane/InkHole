package com.rexvane.inkhole.p2p

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ReceiveFilesTest {
    @get:Rule
    val temp = TemporaryFolder()

    @Test
    fun safeNameRemovesPathsAndInvalidCharacters() {
        assertEquals("report_.txt", ReceiveFiles.safeName("../dir\\report?.txt"))
        assertEquals("unknown", ReceiveFiles.safeName("../.."))
        assertTrue(ReceiveFiles.safeName("a".repeat(300)).toByteArray().size <= 240)
    }

    @Test
    fun uniqueFileNeverOverwritesExistingFiles() {
        val dir = temp.newFolder("inbox")
        File(dir, "report.txt").writeText("one")
        assertEquals("report (2).txt", ReceiveFiles.uniqueFile(dir, "report.txt").name)
        File(dir, "report (2).txt").writeText("two")
        assertEquals("report (3).txt", ReceiveFiles.uniqueFile(dir, "report.txt").name)
    }
}
