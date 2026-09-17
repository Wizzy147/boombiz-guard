package com.boombiz.guard.camera

import java.net.URLEncoder

/**
 * RTSP addresses for the recorders and Wi-Fi cameras Guard knows — the same
 * path conventions as the PC agent's adapters, plus the common Wi-Fi camera
 * apps. Always the SUB-stream: the AI runs at 416×416, so decoding 1080p would
 * only heat the phone.
 *
 *   Hikvision  /Streaming/Channels/<ch>02
 *   Dahua      /cam/realmonitor?channel=<ch>&subtype=1
 *   EZVIZ      /h264/ch<ch>/sub/av_stream               (Hikvision's home brand)
 *   Imou       /cam/realmonitor?channel=<ch>&subtype=1  (Dahua's home brand)
 *   Tapo       /stream2
 *   Reolink    /h264Preview_<ch, 2 digits>_sub
 *   CamHi      /12  (second lens of a two-lens unit: /22)
 *   Yoosee     /onvif2
 *   iCSee      /user=<u>&password=<p>&channel=<ch>&stream=1.sdp?real_stream
 *   Other      the path the installer types (from the camera's manual)
 *
 * None of the Wi-Fi presets has been checked on real hardware yet
 * (docs/supported-devices.md) — the installer's "Test" button is the proof.
 * Credentials are joined in only here, right before the player opens it.
 */
object StreamUrls {
    data class Brand(val key: String, val label: String, val login: String)

    val BRAND_LIST = listOf(
        Brand("HIKVISION", "Hikvision recorder or camera", "The recorder's admin username and password."),
        Brand("DAHUA", "Dahua recorder or camera", "The recorder's admin username and password."),
        Brand("EZVIZ", "EZVIZ Wi-Fi camera",
            "Username admin. Password: the verification code on the camera's sticker. Some EZVIZ cameras need local streaming turned on in the EZVIZ app first."),
        Brand("IMOU", "Imou Wi-Fi camera",
            "Username admin. Password: the safety code on the camera's sticker, or the device password set in the Imou app."),
        Brand("TAPO", "TP-Link Tapo Wi-Fi camera",
            "In the Tapo app open the camera's Settings, then Advanced settings, then Camera account. Use that username and password, not your Tapo login."),
        Brand("REOLINK", "Reolink Wi-Fi camera (plugged in)",
            "The camera's admin username and password. Battery Reolink cameras can't be watched."),
        Brand("CAMHI", "CamHi Wi-Fi camera",
            "Often admin / admin from the factory: change it in the CamHi app. A two-lens camera is two cameras in Guard: channel 1 and channel 2."),
        Brand("YOOSEE", "Yoosee Wi-Fi camera",
            "Username admin. Password: the device password set in the Yoosee app, not your Yoosee login."),
        Brand("ICSEE", "iCSee / XMEye Wi-Fi camera",
            "The username and password set in the iCSee app (admin with no password from the factory)."),
        Brand("OTHER", "Other (type the RTSP path)", "The username and password from the camera's manual."),
    )
    val BRANDS = BRAND_LIST.map { it.key }

    fun brand(key: String): Brand = BRAND_LIST.firstOrNull { it.key == key } ?: BRAND_LIST.last()

    fun path(brand: String, channel: Int, customPath: String?, user: String = "", password: String? = null): String = when (brand) {
        "HIKVISION" -> "/Streaming/Channels/${channel}02"
        "DAHUA", "IMOU" -> "/cam/realmonitor?channel=$channel&subtype=1"
        "EZVIZ" -> "/h264/ch$channel/sub/av_stream"
        "TAPO" -> "/stream2"
        "REOLINK" -> "/h264Preview_%02d_sub".format(channel)
        "CAMHI" -> "/${channel}2"
        "YOOSEE" -> "/onvif2"
        // iCSee firmware reads the login from the path, not from the RTSP handshake.
        "ICSEE" -> "/user=${enc(user)}&password=${enc(password ?: "")}&channel=$channel&stream=1.sdp?real_stream"
        else -> (customPath ?: "").trim().let { if (it.startsWith("/")) it else "/$it" }
    }

    fun url(host: String, port: Int, brand: String, channel: Int, customPath: String?, user: String, password: String?): String {
        val auth = if (user.isNotEmpty()) "${enc(user)}:${enc(password ?: "")}@" else ""
        return "rtsp://$auth${host.trim()}:$port${path(brand, channel, customPath, user, password)}"
    }

    /** Same URL with the login hidden — the only form that may be shown or logged. */
    fun safe(host: String, port: Int, brand: String, channel: Int, customPath: String?): String =
        "rtsp://${host.trim()}:$port${path(brand, channel, customPath, "user", "hidden")}"

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8").replace("+", "%20")

    /** A private LAN address: recorders are never reached over the internet. */
    fun isLan(host: String): Boolean {
        val h = host.trim()
        val p = h.split(".").mapNotNull { it.toIntOrNull() }
        if (p.size != 4 || p.any { it !in 0..255 }) return false
        return p[0] == 10 || (p[0] == 192 && p[1] == 168) || (p[0] == 172 && p[1] in 16..31)
    }
}
