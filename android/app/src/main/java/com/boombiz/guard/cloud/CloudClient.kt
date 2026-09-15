package com.boombiz.guard.cloud

import com.boombiz.guard.BuildConfig
import com.boombiz.guard.data.Store
import com.boombiz.guard.data.Vault
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

/**
 * Link to the Boombiz cloud — the same API the Guard PC uses, unchanged:
 *
 *   POST /api/guard/v1/devices/activate        GARD code → device_id + device_secret (once)
 *   POST /api/guard/v1/device/auth             secret → 30-min access token (memory only)
 *   POST /api/guard/v1/devices/{id}/heartbeat  every 2 min
 *   POST /api/guard/v1/incidents               idempotent per local incident id
 *   POST /api/guard/v1/incidents/{id}/uploads  → presigned PUT (SHA-256 signed in)
 *   POST /api/guard/v1/incidents/{id}/uploads/complete
 *
 * Outbound only: the cloud never reaches into the shop network. The secret is
 * Keystore-sealed and sent ONLY to device/auth.
 */
class CloudClient(private val store: Store, private val base: String = BuildConfig.CLOUD_URL) {

    class Rejected(msg: String) : Exception(msg)
    class Http(val code: Int, val body: JSONObject?) : Exception("HTTP $code")

    private var token: String? = null
    private var tokenExpiresAt = 0L

    val deviceId: String? get() = store.get("cloud_device_id")
    val isActivated: Boolean get() = deviceId != null && store.get("cloud_device_secret") != null

    fun installationId(): String = store.get("installation_id") ?: "inst_${UUID.randomUUID().toString().replace("-", "")}"
        .also { store.put("installation_id", it) }

    private fun secret(): String? = store.get("cloud_device_secret")?.let { Vault.open(it) }

    // ── transport ────────────────────────────────────────────────────
    private fun request(method: String, path: String, body: JSONObject?, auth: String?): Pair<Int, JSONObject?> {
        val c = (URL(if (path.startsWith("http")) path else base + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 15_000; readTimeout = 20_000
            setRequestProperty("Accept", "application/json")
            auth?.let { setRequestProperty("Authorization", "Bearer $it") }
            if (body != null) { doOutput = true; setRequestProperty("Content-Type", "application/json") }
        }
        try {
            body?.let { c.outputStream.use { o -> o.write(it.toString().toByteArray()) } }
            val code = c.responseCode
            val text = (if (code < 400) c.inputStream else c.errorStream)?.use { it.readBytes().decodeToString() }
            return code to text?.let { runCatching { JSONObject(it) }.getOrNull() }
        } finally { c.disconnect() }
    }

    // ── activation ───────────────────────────────────────────────────
    /** → "Business · Branch" on success. Throws Rejected with a plain message. */
    fun activate(code: String, deviceName: String, version: String, osVersion: String): String {
        val body = JSONObject().put("activation_code", code.trim().take(20)).put("installation_id", installationId())
            .put("device_name", deviceName.take(60)).put("agent_version", version).put("os_version", osVersion.take(100))
        val (status, d) = try { request("POST", "/api/guard/v1/devices/activate", body, null) }
            catch (e: IOException) { throw Rejected("Can't reach Boombiz. Check this phone's internet connection and try again.") }
        if (status != 200 || d == null) throw Rejected(d?.optString("error")?.ifBlank { null }
            ?: "Boombiz couldn't activate this phone. Try again in a minute.")
        store.put("cloud_device_id", d.getString("device_id"))
        store.put("cloud_device_secret", Vault.seal(d.getString("device_secret")))
        store.put("business_name", d.optString("business_name").ifBlank { null })
        store.put("location_name", d.optString("location_name").ifBlank { null })
        store.put("device_name", deviceName.take(60))
        store.put("sync_from_ms", System.currentTimeMillis().toString()) // never push history
        token = null
        return listOfNotNull(store.get("business_name"), store.get("location_name")).joinToString(" · ")
    }

    fun forget() {
        listOf("cloud_device_id", "cloud_device_secret", "business_name", "location_name").forEach { store.put(it, null) }
        token = null
    }

    // ── access token ─────────────────────────────────────────────────
    @Synchronized
    private fun accessToken(): String {
        token?.let { if (System.currentTimeMillis() < tokenExpiresAt - 120_000) return it }
        val s = secret() ?: throw Rejected("This phone isn't activated.")
        val (status, d) = request("POST", "/api/guard/v1/device/auth", JSONObject().put("installation_id", installationId()), s)
        if (status == 401) throw Rejected(d?.optString("error")?.ifBlank { null }
            ?: "This phone is no longer connected to Boombiz. Activate it again.")
        if (status != 200 || d == null) throw Http(status, d)
        token = d.getString("access_token")
        tokenExpiresAt = System.currentTimeMillis() + d.optLong("expires_in", 1800) * 1000
        d.optString("business_name").ifBlank { null }?.let { store.put("business_name", it) }
        d.optString("location_name").ifBlank { null }?.let { store.put("location_name", it) }
        return token!!
    }

    /** Authorised call; a 401 re-authenticates once (token rotated or revoked). */
    fun call(method: String, path: String, body: JSONObject?): Pair<Int, JSONObject?> {
        var r = request(method, path, body, accessToken())
        if (r.first == 401) { token = null; r = request(method, path, body, accessToken()) }
        return r
    }

    // ── heartbeat ────────────────────────────────────────────────────
    /** → pending commands; throws on network/auth trouble. */
    fun heartbeat(report: JSONObject): JSONArray {
        val id = deviceId ?: return JSONArray()
        val (status, d) = call("POST", "/api/guard/v1/devices/$id/heartbeat", report)
        if (status != 200 || d == null) throw Http(status, d)
        d.optString("business_name").ifBlank { null }?.let { store.put("business_name", it) }
        d.optString("location_name").ifBlank { null }?.let { store.put("location_name", it) }
        store.put("health_status", d.optString("status"))
        store.put("last_heartbeat_ms", System.currentTimeMillis().toString())
        return d.optJSONArray("commands") ?: JSONArray()
    }

    /** Presigned PUT straight to the private bucket; the headers must be sent as given (checksum is signed in). */
    fun put(url: String, data: ByteArray, headers: JSONObject): Int {
        val c = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "PUT"; doOutput = true; connectTimeout = 15_000; readTimeout = 180_000
            setFixedLengthStreamingMode(data.size)
            headers.keys().forEach { k -> setRequestProperty(k, headers.getString(k)) }
        }
        try {
            c.outputStream.use { it.write(data) }
            return c.responseCode
        } finally { c.disconnect() }
    }
}
