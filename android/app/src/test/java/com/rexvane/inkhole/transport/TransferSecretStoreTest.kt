package com.rexvane.inkhole.transport

import org.junit.Assert.assertEquals
import org.junit.Test

class TransferSecretStoreTest {
    @Test
    fun secureValueWinsAndLegacyIsRemoved() {
        assertEquals(
            TransferSecretMigration("secure", TransferSecretMigrationAction.REMOVE_LEGACY),
            planTransferSecretMigration("secure", true, "legacy"),
        )
    }

    @Test
    fun plaintextValueIsMigratedWhenSecureValueIsMissing() {
        assertEquals(
            TransferSecretMigration(
                "legacy",
                TransferSecretMigrationAction.STORE_AND_REMOVE_LEGACY,
            ),
            planTransferSecretMigration(null, true, "legacy"),
        )
    }

    @Test
    fun emptyLegacyFieldIsOnlyRemoved() {
        assertEquals(
            TransferSecretMigration("", TransferSecretMigrationAction.REMOVE_LEGACY),
            planTransferSecretMigration(null, true, ""),
        )
    }

    @Test
    fun freshInstallNeedsNoMigration() {
        assertEquals(
            TransferSecretMigration("", TransferSecretMigrationAction.NONE),
            planTransferSecretMigration(null, false, ""),
        )
    }
}
