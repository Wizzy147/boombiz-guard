package com.boombiz.guard.cloud

import org.json.JSONArray
import org.json.JSONObject

/**
 * What a heartbeat carries (Phase 4 §44): version, camera NAMES + up/down,
 * memory/disk, AI FPS, queue size, last incident time.
 * What it never carries (§79): video, snapshots, camera IP addresses, stream
 * URLs, usernames or passwords — the builder isn't even given them.
 */
data class CameraHealth(val id: Long, val name: String, val online: Boolean)

object HeartbeatReport {
    fun build(
        version: String, cameras: List<CameraHealth>, ramPercent: Double?, diskFreeGb: Double?,
        aiFps: Double, aiRunning: Boolean, queueSize: Int, lastIncidentAt: String?, applied: List<String>,
    ): JSONObject = JSONObject()
        .put("agent_version", version)
        .put("device_status", "ACTIVE")
        .put("camera_online", cameras.count { it.online })
        .put("camera_total", cameras.size)
        .put("cameras", JSONArray().apply {
            cameras.forEach { put(JSONObject().put("id", it.id.toString()).put("name", it.name.take(80)).put("online", it.online)) }
        })
        .put("cpu_percent", JSONObject.NULL) // Android 8+ hides whole-device CPU from apps
        .put("ram_percent", ramPercent ?: JSONObject.NULL)
        .put("disk_free_gb", diskFreeGb ?: JSONObject.NULL)
        .put("ai_fps", Math.round(aiFps * 10) / 10.0)
        .put("ai_running", aiRunning)
        .put("alarm_available", true)
        .put("queue_size", queueSize)
        .put("last_incident_at", lastIncidentAt ?: JSONObject.NULL)
        .apply { if (applied.isNotEmpty()) put("applied_commands", JSONArray(applied)) }

    /** Predefined commands only (§58): never a shell, never code. Unknown types are acknowledged and ignored. */
    val ALLOWED_COMMANDS = setOf("INCIDENT_ACKNOWLEDGE")
}
