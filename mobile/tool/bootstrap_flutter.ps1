$ErrorActionPreference = 'Stop'

if (-not (Get-Command flutter -ErrorAction SilentlyContinue)) {
    throw 'Flutter SDK is required. Install Flutter 3.24+ and rerun this script.'
}

$mobile = Split-Path -Parent $PSScriptRoot
Push-Location $mobile
try {
    flutter create --platforms=android,ios --org com.rexvane --project-name inkhole_mobile .
    Remove-Item -Force -ErrorAction SilentlyContinue `
        android/app/build.gradle.kts, `
        android/build.gradle.kts, `
        android/settings.gradle.kts, `
        android/app/src/main/kotlin/com/rexvane/inkhole_mobile/MainActivity.kt
    python tool/configure_ios_host.py
    Write-Host 'Flutter host projects generated. Keep lib/, pubspec.yaml and android/app/build.gradle from this tree.'
} finally {
    Pop-Location
}
