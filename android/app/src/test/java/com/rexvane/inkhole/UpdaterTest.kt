package com.rexvane.inkhole

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class UpdaterTest {
    @Test
    fun releaseNotesKeepOnlyUserFacingChanges() {
        val notes = """
            ## Changes
            - First change
            - Second change
            ## Download
            - Installer
        """.trimIndent()
        assertEquals("• First change\n• Second change", Updater.summarizeReleaseNotes(notes))
    }

    @Test
    fun semanticVersionsCompareNumerically() {
        assertTrue(Updater.versionNewer("v1.3.18", "1.3.17"))
        assertFalse(Updater.versionNewer("v1.3.17", "1.3.17"))
        assertFalse(Updater.versionNewer("v1.3.9", "1.3.17"))
    }
}
