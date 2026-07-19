pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
        // Chaquopy's plugin + runtime (also mirrored on Maven Central for 15+).
        maven(url = "https://chaquo.com/maven")
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        maven(url = "https://chaquo.com/maven")
    }
}

rootProject.name = "InstaReactorV3"
include(":app")
