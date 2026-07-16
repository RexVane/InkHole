plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.rexvane.inkhole"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.rexvane.inkhole"
        minSdk = 24
        targetSdk = 34
        versionCode = 29
        versionName = "1.3.13"
    }

    // 固定发布签名:CI 从 secrets 解出 keystore 时启用,让每次构建的 APK
    // 签名一致,用户才能覆盖安装(debug 签名每台机器随机 -> 安装冲突)。
    // 本地无 keystore 时回退空,assembleDebug 仍走默认 debug 签名照常构建。
    val ksPath = System.getenv("ANDROID_KEYSTORE_PATH")
    val hasReleaseKeystore = ksPath != null && file(ksPath).exists()
    signingConfigs {
        if (hasReleaseKeystore) {
            create("release") {
                storeFile = file(ksPath!!)
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS")
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            // 不开混淆:此前一直发 debug 包(从未过 R8),保持行为一致,
            // 本次改动只为固定签名,避免 R8 裁掉 Compose/反射引发新问题。
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            if (hasReleaseKeystore) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }
    kotlinOptions {
        jvmTarget = "1.8"
    }
    buildFeatures {
        compose = true
    }
    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.12"
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.04.00")
    implementation(composeBom)
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.7.0")
    implementation("androidx.activity:activity-compose:1.9.0")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    debugImplementation("androidx.compose.ui:ui-tooling")
}
