// V3 delivery shell: the Chaquopy-hosted app that runs the reused Python engine
// on-device (no PC). Toolchain matches the P0 spike (AGP 8.5.2 / Kotlin 1.9.24 /
// Gradle 8.14.5) so the two modules build with the same JDK; Chaquopy is the one
// addition — it embeds CPython and pip-installs the `insta_reactor` package.
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
    id("com.chaquo.python") version "16.0.0" apply false
}
