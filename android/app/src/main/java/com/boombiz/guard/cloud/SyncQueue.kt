package com.boombiz.guard.cloud

import com.boombiz.guard.data.Incident
import com.boombiz.guard.data.Store
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest

/**
 * Incident sync, the PC agent's cloud/sync.py for the core watcher:
 * INCIDENT_UPSERT first (idempotent on local_incident_id), then SNAPSHOT_UPLOAD
 * once the cloud id exists. Lowest priority number first, so after an outage
 * the after-hours alert lands before camera-health noise. Never on the
 * detection path: if the cloud is down, jobs wait in SQLite.
 *
 * No clips in v1: the phone keeps no rolling video buffer.
 */
class SyncQueue(private val store: Store, private val cloud: CloudClient, private val mediaDir: File) {

    companion object {
        const val UPSERT = "INCIDENT_UPSERT"
        const val SNAPSHOT = "SNAPSHOT_UPLOAD"
        private val BACKOFF = longArrayOf(5, 15, 30, 60, 120, 300)
        private const val MEDIA_MAX_ATTEMPTS = 8
        private const val NOT_READY_S = 300L
        private const val PAUSED_S = 3600L

        fun payload(i: Incident): JSONObject = JSONObject()
            .put("local_incident_id", i.id).put("ref", i.ref).put("incident_type", i.type).put("severity", i.severity)
            .put("confidence", i.confidence ?: JSONObject.NULL).put("camera_id", i.cameraId?.toString() ?: JSONObject.NULL)
            .put("camera_name", i.cameraName ?: JSONObject.NULL).put("title", i.title)
            .put("description", i.description ?: JSONObject.NULL).put("occurred_at", i.occurredAt)
            .put("status", i.status).put("acknowledged_by", i.acknowledgedBy ?: JSONObject.NULL)
            .put("acknowledged_at", i.acknowledgedAt ?: JSONObject.NULL).put("keep_evidence", false)
            .put("timeline", JSONArray().put(JSONObject().put("event", i.type).put("at", i.occurredAt)))
            .put("snapshot_expected", i.snapshotPath != null).put("clip_expected", false)
    }

    var lastError: String? = null
        private set

    fun enqueue(i: Incident) {
        if (i.severity == "INFO") return
        store.want(UPSERT, i.id, com.boombiz.guard.watch.IncidentRules.priority(i.type), reset = true)
        if (i.snapshotPath != null) store.want(SNAPSHOT, i.id, com.boombiz.guard.watch.IncidentRules.SNAPSHOT_PRIORITY)
    }

    /** Run due jobs. Called every 5 s by the watch service, and at once for urgent incidents; blocking (IO thread). */
    @Synchronized
    fun process(now: Long = System.currentTimeMillis()) {
        if (!cloud.isActivated) return
        for (job in store.dueJobs(now)) {
            val inc = store.incident(job.incidentId)
            if (inc == null) { store.jobFail(job.id, "The incident was deleted on this phone."); continue }
            val cloudId = if (job.op == SNAPSHOT) store.cloudId(inc.id) ?: continue else null
            val attempts = job.attempts + 1
            val outcome = try {
                if (job.op == UPSERT) upsert(inc) else upload(inc, cloudId!!)
            } catch (e: CloudClient.Rejected) {
                lastError = e.message; return
            } catch (e: IOException) {
                store.jobRetry(job.id, attempts, now + backoff(attempts), "offline")
                lastError = "Can't reach Boombiz."; return // no point hammering a dead connection
            }
            when (outcome.kind) {
                "done" -> { store.jobDone(job.id, outcome.cloudId); lastError = null }
                "retry" -> {
                    val media = job.op == SNAPSHOT && outcome.delayS != PAUSED_S
                    if (media && attempts >= MEDIA_MAX_ATTEMPTS) store.jobFail(job.id, outcome.error ?: "Upload failed.")
                    else store.jobRetry(job.id, attempts, now + (outcome.delayS?.times(1000) ?: backoff(attempts)), outcome.error ?: "retry")
                }
                else -> store.jobFail(job.id, outcome.error ?: "Refused.")
            }
        }
    }

    private fun backoff(attempts: Int) = BACKOFF[minOf(attempts - 1, BACKOFF.size - 1)] * 1000

    private data class Outcome(val kind: String, val cloudId: String? = null, val error: String? = null, val delayS: Long? = null)

    private fun classify(code: Int, body: JSONObject?): Outcome {
        val err = body?.optString("error")?.ifBlank { null }
        return when {
            code == 200 || code == 201 -> Outcome("done")
            code == 402 -> Outcome("retry", error = err ?: "Cloud storage is paused until the Guard plan is renewed.", delayS = PAUSED_S)
            code == 404 -> Outcome("retry", error = "Boombiz isn't ready for incident sync yet.", delayS = NOT_READY_S)
            code in listOf(401, 408, 409, 425, 429) || code >= 500 -> Outcome("retry", error = err ?: "HTTP $code")
            else -> Outcome("failed", error = err ?: "HTTP $code")
        }
    }

    private fun upsert(i: Incident): Outcome {
        val (code, body) = cloud.call("POST", "/api/guard/v1/incidents", payload(i))
        val o = classify(code, body)
        return if (o.kind == "done") o.copy(cloudId = body?.optString("incident_id")) else o
    }

    private fun upload(i: Incident, cloudId: String): Outcome {
        val f = i.snapshotPath?.let { File(mediaDir, it) }
        if (f == null || !f.exists()) return Outcome("failed", error = "The file is no longer on this phone.")
        val data = f.readBytes()
        val sha = MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }
        val base = "/api/guard/v1/incidents/$cloudId/uploads"
        val (code, d) = cloud.call("POST", base, JSONObject().put("media_type", "SNAPSHOT")
            .put("content_type", "image/jpeg").put("size_bytes", data.size).put("sha256", sha))
        val o = classify(code, d)
        if (o.kind != "done" || d == null) return o
        if (d.optBoolean("already_uploaded")) return Outcome("done")
        val put = cloud.put(d.getString("upload_url"), data, d.optJSONObject("headers") ?: JSONObject().put("Content-Type", "image/jpeg"))
        if (put >= 400) return Outcome("retry", error = "Upload refused (HTTP $put).")
        val (c2, b2) = cloud.call("POST", "$base/complete", JSONObject().put("media_type", "SNAPSHOT"))
        return if (c2 == 422) Outcome("retry", error = b2?.optString("error") ?: "Upload check failed.") else classify(c2, b2)
    }
}
