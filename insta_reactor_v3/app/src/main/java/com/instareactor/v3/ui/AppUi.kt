package com.instareactor.v3.ui

import android.content.Intent
import android.provider.Settings
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.instareactor.v3.MainViewModel
import com.instareactor.v3.RunPhase
import com.instareactor.v3.RunState
import org.json.JSONObject

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ReactorApp(vm: MainViewModel) {
    val phase by RunState.phase.collectAsState()

    Scaffold(topBar = { TopAppBar(title = { Text("INSTA REACTOR") }) }) { pad ->
        Column(
            Modifier
                .padding(pad)
                .padding(16.dp)
                .fillMaxSize()
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            PermissionCard(vm)
            SetupCard(vm)
            RunCard(vm, phase)
            ResultCard(phase)
            ReviewCard(vm)
        }
    }
}

@Composable
private fun SectionCard(title: String, content: @Composable () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            content()
        }
    }
}

@Composable
private fun PermissionCard(vm: MainViewModel) {
    val context = LocalContext.current
    val enabled = vm.serviceEnabled()
    SectionCard("Permission") {
        Text(
            if (enabled) "Accessibility service: ON ✓ — ready to run."
            else "Accessibility service is OFF. Enable “INSTA REACTOR” under " +
                "Settings ▸ Accessibility so the app can read reels and tap for you.",
            style = MaterialTheme.typography.bodyMedium,
        )
        if (!enabled) {
            Button(onClick = {
                context.startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }) { Text("Open Accessibility settings") }
        }
    }
}

@Composable
private fun SetupCard(vm: MainViewModel) {
    SectionCard("Setup") {
        OutlinedTextField(
            value = vm.emojiPrefs, onValueChange = { vm.emojiPrefs = it },
            label = { Text("Favourite emojis (comma-separated, most-liked first)") },
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = vm.commonReplies, onValueChange = { vm.commonReplies = it },
            label = { Text("Your common replies (one per line)") },
            modifier = Modifier.fillMaxWidth(),
        )
        Text("Reply style", style = MaterialTheme.typography.labelLarge)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("single" to "💀", "double" to "💀💀", "text_emoji" to "bro 💀").forEach { (v, label) ->
                FilterChip(
                    selected = vm.replyStyle == v,
                    onClick = { vm.replyStyle = v },
                    label = { Text(label) },
                )
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            OutlinedTextField(
                value = vm.minComments, onValueChange = { vm.minComments = it.filter(Char::isDigit) },
                label = { Text("Min comments") }, modifier = Modifier.weight(1f),
            )
            OutlinedTextField(
                value = vm.maxNewReels, onValueChange = { vm.maxNewReels = it.filter(Char::isDigit) },
                label = { Text("Max new reels") }, modifier = Modifier.weight(1f),
            )
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(checked = vm.useModel, onCheckedChange = { vm.useModel = it })
            Spacer(Modifier.height(0.dp))
            Text("  Use offline model (optional — rules stay authoritative)")
        }
        HorizontalDivider()
        OutlinedTextField(
            value = vm.enabledChats, onValueChange = { vm.enabledChats = it },
            label = { Text("Chats to process (exact DM titles, one per line)") },
            modifier = Modifier.fillMaxWidth(),
        )
        Button(onClick = { vm.saveConfig() }) { Text("Save setup") }
        vm.saveMsg?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
    }
}

@Composable
private fun RunCard(vm: MainViewModel, phase: RunPhase) {
    val running = phase is RunPhase.Running
    SectionCard("Run") {
        Text(
            "Dry run decides and queues everything but never sends. Do this first " +
                "and check the review queue; switch to “Run & send” once it looks right.",
            style = MaterialTheme.typography.bodySmall,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            OutlinedButton(
                onClick = { vm.startRun(send = false) },
                enabled = !running && vm.serviceEnabled(),
            ) { Text("Dry run") }
            Button(
                onClick = { vm.startRun(send = true) },
                enabled = !running && vm.serviceEnabled(),
            ) { Text("Run & send") }
        }
        if (running) {
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                CircularProgressIndicator()
                Text("Running… keep this phone unlocked and on Instagram.")
            }
        }
    }
}

@Composable
private fun ResultCard(phase: RunPhase) {
    when (phase) {
        is RunPhase.Done -> SectionCard("Last run") { RunSummary(phase.summaryJson) }
        is RunPhase.Failed -> SectionCard("Last run") {
            Text("Failed: ${phase.message}", style = MaterialTheme.typography.bodyMedium)
        }
        else -> {}
    }
}

@Composable
private fun RunSummary(json: String) {
    val obj = runCatching { JSONObject(json) }.getOrNull()
    if (obj == null || !obj.optBoolean("ok")) {
        Text("Could not read the run result:\n$json",
            style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace)
        return
    }
    val counts = obj.optJSONObject("counts") ?: JSONObject()
    val sent = if (obj.optBoolean("send")) "sent" else "would send (dry run)"
    Text("Auto-replies $sent: ${counts.optInt("auto_replied")}",
        style = MaterialTheme.typography.bodyMedium)
    Text("Flagged for review: ${counts.optInt("flagged")}",
        style = MaterialTheme.typography.bodyMedium)

    val replied = obj.optJSONArray("auto_replied")
    if (replied != null && replied.length() > 0) {
        HorizontalDivider()
        for (i in 0 until replied.length()) {
            val d = replied.getJSONObject(i)
            Text("• ${d.optString("chat_name")} → ${d.optString("reply_text")}  " +
                "(${d.optDouble("confidence")})",
                style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun ReviewCard(vm: MainViewModel) {
    SectionCard("Review queue") {
        OutlinedButton(onClick = { vm.refreshReview() }) { Text("Refresh") }
        if (vm.reviewItems.isEmpty()) {
            Text("Nothing waiting for review.", style = MaterialTheme.typography.bodyMedium)
        } else {
            vm.reviewItems.forEach { item ->
                HorizontalDivider()
                Text("${item.chat} — ${item.kind}", style = MaterialTheme.typography.bodyMedium)
                Text(item.reason, style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}
