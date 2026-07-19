package com.instareactor.v3

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Process-wide holder for the current/last run, so the foreground [RunService]
 * (which does the work) and the Compose UI (which observes it) stay decoupled.
 */
sealed interface RunPhase {
    data object Idle : RunPhase
    data object Running : RunPhase
    /** Finished — `summaryJson` is app_entry.run()'s JSON result. */
    data class Done(val summaryJson: String) : RunPhase
    data class Failed(val message: String) : RunPhase
}

object RunState {
    private val _phase = MutableStateFlow<RunPhase>(RunPhase.Idle)
    val phase: StateFlow<RunPhase> = _phase.asStateFlow()

    fun set(phase: RunPhase) {
        _phase.value = phase
    }

    val isRunning: Boolean get() = _phase.value is RunPhase.Running
}
