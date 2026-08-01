Pod::Spec.new do |s|
  s.name = 'InkHoleCore'
  s.version = '2.0.0'
  s.summary = 'Rust transport core for InkHole'
  s.description = 'Static Rust XCFramework exposing the InkHole FFI ABI.'
  s.homepage = 'https://github.com/RexVane/InkHole'
  s.ios.deployment_target = '12.0'
  s.static_framework = true
  s.vendored_frameworks = 'InkHoleCore.xcframework'
  s.user_target_xcconfig = {
    'OTHER_LDFLAGS' => '$(inherited) -all_load',
  }
end
