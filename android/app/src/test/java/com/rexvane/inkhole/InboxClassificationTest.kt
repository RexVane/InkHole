package com.rexvane.inkhole

import org.junit.Assert.assertEquals
import org.junit.Test

class InboxClassificationTest {
    @Test
    fun classifiesTopLevelItems() {
        assertEquals(InboxClassification.MEDIA,
            InboxClassification.categoryFor("photo.HEIC", false))
        assertEquals(InboxClassification.MEDIA,
            InboxClassification.categoryFor("clip.mp4", false))
        assertEquals(InboxClassification.ARCHIVE,
            InboxClassification.categoryFor("backup.tar.gz", false))
        assertEquals(InboxClassification.FILE,
            InboxClassification.categoryFor("notes.pdf", false))
        assertEquals(InboxClassification.FOLDER,
            InboxClassification.categoryFor("photos.zip", true))
    }

    @Test
    fun sanitizesConfiguredDirectoryNames() {
        assertEquals("图片和视频",
            InboxClassification.directoryName(InboxClassification.MEDIA, ""))
        assertEquals("相册2026",
            InboxClassification.directoryName(InboxClassification.MEDIA, "相册/2026"))
        assertEquals("压缩包",
            InboxClassification.directoryName(InboxClassification.ARCHIVE, "..."))
    }
}
