package com.boombiz.guard.watch

/**
 * Which watcher events become incidents — the PC agent's incidents/classifier.py
 * rules for the core watcher, with the same types, severities and titles so the
 * cloud, the alert rules and the WhatsApp templates treat a phone exactly like a PC.
 */
data class IncidentRule(val type: String, val severity: String, val windowS: Double, val title: String, val perTrack: Boolean)

object IncidentRules {
    const val UNPLUGGED = "GUARD_DEVICE_UNPLUGGED"

    val RULES = mapOf(
        "AFTER_HOURS_PERSON" to IncidentRule("AFTER_HOURS_INTRUSION", "CRITICAL", 60.0, "Person inside after hours", false),
        // A 4G / solar camera's own app reported movement (AppAlerts). Same cloud type as a
        // DVR camera, so alert rules and WhatsApp work unchanged; the title says it's movement,
        // because the camera — not Guard's AI — decided there was a person.
        AppAlerts.EVENT to IncidentRule("AFTER_HOURS_INTRUSION", "CRITICAL", 60.0, "Movement after hours", false),
        "RESTRICTED_ZONE_ENTRY" to IncidentRule("RESTRICTED_AREA_INCIDENT", "HIGH", 30.0, "Restricted area entered", true),
        "CAMERA_OFFLINE" to IncidentRule("CAMERA_OFFLINE", "HIGH", 1e9, "Camera stopped sending video", false),
        "CAMERA_TAMPERED" to IncidentRule("CAMERA_TAMPERED", "HIGH", 600.0, "Camera covered or turned away", false),
        // The watcher itself pulled off its charger (phone only; a TV box just loses power).
        // Shop closed = someone is probably taking it: CRITICAL + siren at once, before
        // they can switch it off. Shop open = someone moved it: a quiet LOW notice.
        "DEVICE_UNPLUGGED_CLOSED" to IncidentRule(UNPLUGGED, "CRITICAL", 600.0, "Guard phone unplugged after hours", false),
        "DEVICE_UNPLUGGED_OPEN" to IncidentRule(UNPLUGGED, "LOW", 600.0, "Guard phone unplugged", false),
    )

    /** Sync order after an outage (PC cloud/sync.py): after-hours first, then restricted, then health. */
    fun priority(type: String): Int = when (type) {
        "AFTER_HOURS_INTRUSION", UNPLUGGED -> 20
        "RESTRICTED_AREA_INCIDENT" -> 35
        "CAMERA_OFFLINE", "CAMERA_TAMPERED" -> 40
        else -> 70
    }

    const val SNAPSHOT_PRIORITY = 50

    /** How long the siren sounds (s), and whether it only sounds with the shop closed. */
    data class Alarm(val seconds: Int, val cooldownS: Int, val afterClosingOnly: Boolean)

    val ALARMS = mapOf(
        "AFTER_HOURS_INTRUSION" to Alarm(10, 60, false),
        "RESTRICTED_AREA_INCIDENT" to Alarm(5, 30, false),
        // Camera damage sounds only after closing: in the day staff move and clean cameras.
        "CAMERA_OFFLINE" to Alarm(10, 0, true),
        "CAMERA_TAMPERED" to Alarm(10, 0, true),
        // Long and loud: the thief is holding the siren.
        UNPLUGGED to Alarm(30, 0, true),
    )

    /**
     * Merge window per (rule, camera[, person]): a second trigger inside the
     * window is the same incident, not a new alert.
     */
    class Dedup {
        private val last = HashMap<String, Double>()

        fun isNew(rule: IncidentRule, cameraId: Long, trackId: Long?, now: Double): Boolean {
            val key = "${rule.type}:$cameraId:${if (rule.perTrack) trackId else "-"}"
            val prev = last[key]
            if (prev != null && now - prev < rule.windowS) return false
            last[key] = now
            return true
        }

        /** A camera that comes back ends its offline episode, so the next outage alerts again. */
        fun clear(type: String, cameraId: Long) { last.keys.removeAll { it.startsWith("$type:$cameraId:") } }
    }
}
