[app]
title = MKS Android Merger
package.name = mksandroidmerger
package.domain = org.hoseiny
source.dir = .
source.include_exts = py,kv,md
version = 0.1.0
requirements = python3,kivy,plyer
orientation = portrait
fullscreen = 0
android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE

# این خط حیاتی است
android.accept_sdk_license = True

# استفاده از SDK پیش‌فرض گیت‌هاب
android.sdk_path = /usr/local/lib/android/sdk

android.api = 33
android.minapi = 23
android.archs = arm64-v8a, armeabi-v7a
android.build_tools = 33.0.2

[buildozer]
log_level = 2
warn_on_root = 1
