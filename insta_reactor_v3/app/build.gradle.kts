plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.instareactor.v3"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.instareactor.v3"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.3.0"

        // Chaquopy ships CPython per-ABI. arm64 = real phones; x86_64 = emulator.
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }

    composeOptions {
        // Compose compiler paired with Kotlin 1.9.24.
        kotlinCompilerExtensionVersion = "1.5.14"
    }
}

// Stage a CLEAN copy of just the engine package (+ its pyproject) into the build
// dir. Chaquopy fingerprints the pip-install source as a task input, so we must
// NOT point it at the repo root — that would try to hash the whole repo,
// including the locked .gradle dir. This isolated copy is the install source.
val stageEngine = tasks.register<Copy>("stageEngine") {
    val repoRoot = rootProject.file("..")
    into(layout.buildDirectory.dir("engine"))
    from(repoRoot) {
        include("insta_reactor/**")   // the reused engine (pure stdlib)
        include("pyproject.toml")      // dependencies = [], include = insta_reactor*
        include("README.md")           // pyproject's readme = "README.md"
    }
}

// Chaquopy config lives in its own top-level block (its Kotlin DSL doesn't add
// `python` onto android.defaultConfig).
chaquopy {
    defaultConfig {
        // The engine requires >=3.10 (pyproject.toml). 3.11 is a Chaquopy target.
        version = "3.11"
        pip {
            // Install the reused engine from the staged copy (see stageEngine).
            // dependencies = [] means this resolves with no third-party downloads.
            install(layout.buildDirectory.dir("engine").get().asFile.absolutePath)
        }
    }
}

// The pip-requirements task must see the staged copy first.
tasks.matching { it.name.contains("PythonRequirements") }.configureEach {
    dependsOn(stageEngine)
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.6")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.6")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.6")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    val composeBom = platform("androidx.compose:compose-bom:2024.09.00")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    debugImplementation("androidx.compose.ui:ui-tooling")
}
