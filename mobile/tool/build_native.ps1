$ErrorActionPreference = 'Stop'

if (-not (Get-Command cargo-ndk -ErrorAction SilentlyContinue)) {
    throw 'cargo-ndk is required: cargo install cargo-ndk'
}
if (-not (Get-Command rustup -ErrorAction SilentlyContinue)) {
    throw 'rustup is required to install Android Rust targets'
}

rustup target add aarch64-linux-android armv7-linux-androideabi x86_64-linux-android

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$out = Join-Path $repo 'mobile/native/android'
New-Item -ItemType Directory -Force -Path $out | Out-Null

Push-Location (Join-Path $repo 'rust')
try {
    cargo ndk -t arm64-v8a -t armeabi-v7a -t x86_64 -o $out build --release -p inkhole-ffi
} finally {
    Pop-Location
}
