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
        assertTrue(Updater.versionNewer("v1.3.23", "1.3.22"))
        assertFalse(Updater.versionNewer("v1.3.22", "1.3.22"))
        assertFalse(Updater.versionNewer("v1.3.9", "1.3.22"))
    }

    @Test
    fun updateAssetMatchesDeviceAbiInsteadOfReleaseOrder() {
        val assets = linkedMapOf(
            "InkHole-v1.7.1-armeabi-v7a.apk" to "armeabi",
            "InkHole-v1.7.1-arm64-v8a.apk" to "arm64",
            "InkHole-v1.7.1.apk" to "arm64-alias",
        )
        assertEquals(
            "arm64",
            Updater.selectApkUrl("v1.7.1", assets, listOf("arm64-v8a", "armeabi-v7a")),
        )
        assertEquals(
            "armeabi",
            Updater.selectApkUrl("v1.7.1", assets, listOf("armeabi-v7a")),
        )
    }

    @Test
    fun updateAssetUsesGenericApkOnlyForLegacyUniversalReleases() {
        assertEquals(
            "universal",
            Updater.selectApkUrl(
                "v1.7.0",
                mapOf("InkHole-v1.7.0.apk" to "universal"),
                listOf("armeabi-v7a"),
            ),
        )
        assertEquals(
            "",
            Updater.selectApkUrl(
                "v1.7.1",
                mapOf(
                    "InkHole-v1.7.1.apk" to "arm64-alias",
                    "InkHole-v1.7.1-arm64-v8a.apk" to "arm64",
                ),
                listOf("x86"),
            ),
        )
    }
}
