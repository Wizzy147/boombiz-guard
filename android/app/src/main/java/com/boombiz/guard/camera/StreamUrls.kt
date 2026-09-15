package com.boombiz.guard.camera

import java.net.URLEncoder

/**
 * RTSP addresses for the recorders Guard builds for first — the same path
 * conventions as the PC agent's adapters. Always the SUB-stream: the AI runs
 * at 416×416, so decoding 1080p would only heat the phone.
 *
 *   Hikvision  /Streaming/Channels/<ch>02
 *   Dahua      /cam/realmonitor?channel=<ch>&subtype=1
 *   Other      the path the installer types (from the camera's manual)
 *
 * Credentials are joined in only here, right before the player opens it.
 */
object StreamUrls {
    val BRANDS = listOf("HIKVISION", "DAHUA", "OTHER")

    fun path(brand: String, channel: Int, customPath: String?): String = when (brand) {
        "HIKVISION" -> "/Streaming/Channels/${channel}02"
        "DAHUA" -> "/cam/realmonitor?channel=$channel&subtype=1"
        else -> (customPath ?: "").trim().let { if (it.startsWith("/")) it else "/$it" }
    }

    fun url(host: String, port: Int, brand: String, channel: Int, customPath: String?, user: String, password: String?): String {
        val auth = if (user.isNotEmpty()) "${enc(user)}:${enc(password ?: "")}@" else ""
        return "rtsp://$auth${host.trim()}:$port${path(brand, channel, customPath)}"
    }

    /** Same URL with the password hidden — the only form that may be shown or logged. */
    fun safe(host: String, port: Int, brand: String, channel: Int, customPath: String?): String =
        "rtsp://${host.trim()}:$port${path(brand, channel, customPath)}"

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8").replace("+", "%20")

    /** A private LAN address: recorders are never reached over the internet. */
    fun isLan(host: String): Boolean {
        val h = host.trim()
        val p = h.split(".").mapNotNull { it.toIntOrNull() }
        if (p.size != 4 || p.any { it !in 0..255 }) return false
        return p[0] == 10 || (p[0] == 192 && p[1] == 168) || (p[0] == 172 && p[1] in 16..31)
    }
}
