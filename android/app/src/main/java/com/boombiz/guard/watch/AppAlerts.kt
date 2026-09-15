package com.boombiz.guard.watch

import org.json.JSONArray
import org.json.JSONObject

/**
 * 4G / solar cameras (V380 Pro, CamHi, UBox…) have no local video Guard can
 * reach: they talk to their maker's cloud over their own SIM, and sleep until
 * their motion sensor wakes them. What they do have is their own phone app,
 * which posts a "human detected" notification when they wake.
 *
 * So the installer puts that app on the Guard phone and links it here; Guard
 * reads only that app's notifications and turns an alarm while the shop is
 * closed into the same after-hours incident a DVR camera raises — same cloud
 * type, alert rules, WhatsApp templates and siren.
 *
 * Guard's own AI never sees these pictures: false alarms from the camera
 * (a cat, headlights) come through too. The setup screen says so.
 *
 * Pure logic: no Android types, so it runs in JVM unit tests.
 */
object AppAlerts {
    /** Settings key holding the linked apps (JSON array). */
    const val KEY = "app_alert_sources"
    const val EVENT = "APP_ALERT_AFTER_HOURS"
    const val MAX_SOURCES = 4

    /** One camera app linked on this phone, and the name the shop calls the camera behind it. */
    data class Source(val pkg: String, val label: String, val cameraName: String)

    enum class Kind { ALARM, IGNORE }

    /** Label/package fragments of the camera apps sold with these cameras in Nigeria — only used to list them first. */
    private val HINTS = listOf("v380", "macrovideo", "camhi", "hichip", "ubox", "ycc365", "icsee", "xmeye", "carecam")

    fun looksLikeCameraApp(label: String, pkg: String): Boolean {
        val s = "$label $pkg".lowercase()
        return HINTS.any { it in s }
    }

    /**
     * Camera apps also send adverts, update prompts and account notices. Anything
     * that isn't clearly one of those is treated as an alarm: the wording of real
     * alarms differs per app, firmware and language ("Human detected", "Motion
     * detection alarm", "PIR", Chinese text), so an allow-list would miss them.
     */
    private val NOT_ALARMS = listOf(
        "update", "upgrade", "new version", "firmware", "promotion", "offer", "discount", "coupon", "% off",
        "subscribe", "subscription", "renew", "expire", "cloud storage", "free trial", "vip",
        "log in", "login", "logged", "sign in", "password", "verification code", "account", "welcome", "download",
        "is running", "running in the background", "syncing",
    )

    fun classify(title: String?, text: String?): Kind {
        val s = listOfNotNull(title, text).joinToString(" ").trim().lowercase()
        if (s.isEmpty()) return Kind.IGNORE
        return if (NOT_ALARMS.any { it in s }) Kind.IGNORE else Kind.ALARM
    }

    /** Incident description: what the camera's app said, and that Guard's AI didn't check it. */
    fun detail(source: Source, title: String?, text: String?): String {
        val said = listOfNotNull(title?.trim()?.ifEmpty { null }, text?.trim()?.ifEmpty { null }).distinct().joinToString(" — ")
        return "The ${source.label} app reported: ${said.take(160)}. Movement while the shop is closed."
    }

    /**
     * A stable id for dedup, below zero so it never clashes with a real camera
     * row (ids start at 1) or the device itself (0). Never sent to the cloud.
     */
    fun dedupId(pkg: String): Long = -(pkg.hashCode().toLong() and 0x7fffffffL) - 1

    fun encode(sources: List<Source>): String = JSONArray().apply {
        sources.forEach { put(JSONObject().put("pkg", it.pkg).put("label", it.label).put("camera", it.cameraName)) }
    }.toString()

    fun decode(json: String?): List<Source> = runCatching {
        val a = JSONArray(json ?: return emptyList())
        (0 until a.length()).mapNotNull { i ->
            val o = a.optJSONObject(i) ?: return@mapNotNull null
            val pkg = o.optString("pkg").ifBlank { return@mapNotNull null }
            Source(pkg, o.optString("label").ifBlank { pkg }, o.optString("camera").ifBlank { o.optString("label") })
        }
    }.getOrDefault(emptyList())
}
