# Flutter Android host

This host only embeds Flutter and loads `libinkhole_ffi.so` from
`../../native/android/<abi>/`. It contains no transport implementation.

Run `flutter create --platforms android,ios .` once if the Flutter SDK has not
generated the Gradle wrapper and plugin metadata yet, then run
`../tool/build_native.ps1` (or the shell equivalent) before assembling an APK.
