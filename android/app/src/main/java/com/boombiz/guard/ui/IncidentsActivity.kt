package com.boombiz.guard.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.BitmapFactory
import android.os.Bundle
import android.widget.ImageView
import android.widget.LinearLayout
import androidx.media3.common.util.UnstableApi
import com.boombiz.guard.GuardApp
import com.boombiz.guard.cloud.SyncQueue
import com.boombiz.guard.data.Incident
import com.boombiz.guard.watch.IncidentRules
import com.boombiz.guard.watch.WatchService
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** What this device saw, newest first. Acknowledging here stops the siren and syncs to the cloud. */
@UnstableApi
class IncidentsActivity : Activity() {
    private val app get() = application as GuardApp
    private val fmt = DateTimeFormatter.ofPattern("EEE d MMM, HH:mm").withZone(ZoneId.systemDefault())

    override fun onResume() {
        super.onResume()
        val p = page("Incidents")
        val list = app.store.incidents()
        if (list.isEmpty()) p.text("Nothing yet.", color = C.MUTED)
        for (i in list) {
            val sev = if (i.severity == "CRITICAL" || i.severity == "HIGH") "⚠ " else ""
            p.button("$sev${i.title}\n${i.cameraName ?: ""} · ${fmt.format(Instant.parse(i.occurredAt))} · ${label(i.status)}",
                primary = false) { open(i) }
        }
    }

    private fun label(s: String) = when (s) { "UNREVIEWED" -> "not checked yet"; "ACKNOWLEDGED" -> "checked"; else -> s.lowercase() }

    private fun open(i: Incident) {
        val body = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(16), dp(8), dp(16), 0) }
        i.snapshotPath?.let { File(File(filesDir, "media"), it) }?.takeIf { it.exists() }?.let { f ->
            body.addView(ImageView(this).apply { adjustViewBounds = true; setImageBitmap(BitmapFactory.decodeFile(f.path)) })
        }
        body.text("${i.ref}\n${fmt.format(Instant.parse(i.occurredAt))}\n${i.description ?: ""}", 14f)
        i.acknowledgedBy?.let { body.text("Checked by $it", 14f, color = C.MUTED) }
        val d = AlertDialog.Builder(this).setTitle(i.title).setView(body).setNegativeButton("Close", null)
        if (i.status == "UNREVIEWED") d.setPositiveButton("I've checked it") { _, _ ->
            if (app.store.acknowledge(i.id, "On this device", Instant.now().toString())) {
                startService(Intent(this, WatchService::class.java).setAction(WatchService.ACTION_STOP_SIREN))
                app.store.want(SyncQueue.UPSERT, i.id, IncidentRules.priority(i.type), reset = true)
            }
            onResume()
        }
        d.show()
    }
}
