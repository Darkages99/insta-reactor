package com.instareactor.v3

import android.Manifest
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.material3.MaterialTheme
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.instareactor.v3.ui.ReactorApp
import kotlinx.coroutines.launch

/**
 * The single Compose host. Setup / run / results / review all live in one
 * scrolling screen (see ui/AppUi.kt). This replaces V2's `webui.py` — same
 * capabilities, now on-device with no PC and no browser.
 */
class MainActivity : ComponentActivity() {

    private val vm: MainViewModel by viewModels()

    private val notifPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestNotifPermissionIfNeeded()
        setContent {
            MaterialTheme { ReactorApp(vm) }
        }
    }

    override fun onResume() {
        super.onResume()
        // The service may have just been enabled in Settings; reflect it and
        // pick up any review items a finished run wrote.
        lifecycleScope.launch { vm.refreshReview() }
    }

    private fun requestNotifPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val granted = ContextCompat.checkSelfPermission(
                this, Manifest.permission.POST_NOTIFICATIONS,
            ) == android.content.pm.PackageManager.PERMISSION_GRANTED
            if (!granted) notifPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
}
