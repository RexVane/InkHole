package com.rexvane.inkhole.transport

import android.content.Context

internal const val TRANSFER_SECRET_LEGACY_PREF = "secret"
internal const val TRANSFER_SECRET_SECURE_NAME = "transfer_secret_v1"

internal enum class TransferSecretMigrationAction {
    NONE,
    STORE_AND_REMOVE_LEGACY,
    REMOVE_LEGACY,
}

internal data class TransferSecretMigration(
    val value: String,
    val action: TransferSecretMigrationAction,
)

internal fun planTransferSecretMigration(
    secureValue: String?,
    legacyPresent: Boolean,
    legacyValue: String,
): TransferSecretMigration = when {
    secureValue != null -> TransferSecretMigration(
        secureValue,
        if (legacyPresent) TransferSecretMigrationAction.REMOVE_LEGACY
        else TransferSecretMigrationAction.NONE,
    )
    legacyPresent && legacyValue.isNotEmpty() -> TransferSecretMigration(
        legacyValue,
        TransferSecretMigrationAction.STORE_AND_REMOVE_LEGACY,
    )
    legacyPresent -> TransferSecretMigration("", TransferSecretMigrationAction.REMOVE_LEGACY)
    else -> TransferSecretMigration("", TransferSecretMigrationAction.NONE)
}

data class TransferSecretLoad(val value: String, val warning: String = "")

/** Stores the shared WHPP password with Android Keystore and removes pre-1.5 plaintext data. */
object TransferSecretStore {
    private const val PREFS = "inkhole"

    fun load(context: Context): TransferSecretLoad {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val legacyPresent = prefs.contains(TRANSFER_SECRET_LEGACY_PREF)
        val legacyValue = prefs.getString(TRANSFER_SECRET_LEGACY_PREF, "").orEmpty()
        val warnings = mutableListOf<String>()
        val secureValue = try {
            SecureStore.get(context, TRANSFER_SECRET_SECURE_NAME)
        } catch (_: Exception) {
            warnings += "无法读取系统安全存储"
            null
        }
        val migration = planTransferSecretMigration(
            secureValue, legacyPresent, legacyValue)

        if (migration.action == TransferSecretMigrationAction.STORE_AND_REMOVE_LEGACY) {
            try {
                SecureStore.put(context, TRANSFER_SECRET_SECURE_NAME, legacyValue)
            } catch (_: Exception) {
                warnings += "无法迁移旧传输口令，口令仅在本次运行中生效"
            }
        }
        if (migration.action != TransferSecretMigrationAction.NONE) {
            val removed = prefs.edit().remove(TRANSFER_SECRET_LEGACY_PREF).commit()
            if (!removed) warnings += "无法清除普通配置中的旧传输口令"
        }
        return TransferSecretLoad(migration.value, warnings.distinct().joinToString("；"))
    }

    /** The old value remains active if this method throws. */
    fun save(context: Context, value: String) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        if (prefs.contains(TRANSFER_SECRET_LEGACY_PREF) &&
            !prefs.edit().remove(TRANSFER_SECRET_LEGACY_PREF).commit()
        ) {
            throw IllegalStateException("无法清除普通配置中的旧传输口令")
        }
        SecureStore.put(context, TRANSFER_SECRET_SECURE_NAME, value)
    }
}
