package com.boombiz.guard.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.widget.EditText
import androidx.media3.common.util.UnstableApi
import com.boombiz.guard.GuardApp
import com.boombiz.guard.watch.AppAlertListener
import com.boombiz.guard.watch.AppAlerts
import com.boombiz.guard.watch.WatchService
import java.text.DateFormat
import java.util.Date

/**
 * 4G / solar cameras (V380 Pro, CamHi, UBox): link the camera's own app so
 * its alarms become Guard alerts. See [AppAlerts] for why it works this way.
 */
@UnstableApi
class CameraAppsActivity : Activity() {
    private val app get() = application as GuardApp

    override fun onResume() { super.onResume(); build() } // back from Settings → re-check access

    private fun sources() = AppAlerts.decode(app.store.get(AppAlerts.KEY))

    private fun save(list: List<AppAlerts.Source>) {
        app.store.put(AppAlerts.KEY, if (list.isEmpty()) null else AppAlerts.encode(list))
        WatchService.startIfConfigured(this)
        build()
    }

    private fun build() {
        val p = page("4G / solar cameras")
        p.text("These cameras use a SIM card and their own phone app (V380 Pro, CamHi or UBox), so Guard can't see their video. " +
               "Instead, Guard listens for the camera app's \"person detected\" alert on this phone. " +
               "While the shop is closed, that alert sounds the alarm here and alerts the owner, just like a CCTV camera.")

        p.heading("1. Put the camera app on this phone")
        p.text("Install the same app the camera uses, sign in with the shop's account, and check you can see the camera live. " +
               "In the app, turn on human/motion detection and alarm notifications for the camera.", color = C.MUTED)
        p.text("Tecno, Infinix and itel: also allow the camera app in Phone Master → App auto-start, and turn off battery saving for it. " +
               "If the camera app is stopped, its alerts stop too.", 14f, color = C.MUTED)

        p.heading("2. Let Guard read camera app alerts")
        if (AppAlertListener.granted(this)) {
            p.text("Allowed. Guard only reads the apps you link below; it ignores every other app.", color = C.GREEN)
        } else {
            p.text("Not allowed yet. In the next screen, switch on Boombiz Guard.", color = C.RED)
            p.button("Open notification access") {
                runCatching { startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }
            }
        }

        p.heading("3. Link the camera app")
        val list = sources()
        if (list.isEmpty()) p.text("No camera app linked yet.", color = C.MUTED)
        for (s in list) {
            val heard = WatchService.status.appAlertHeard[s.pkg]
                ?.let { "last alert ${DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(it))}" } ?: "no alert yet"
            p.button("${s.cameraName} · ${s.label} · $heard", primary = false) { confirmRemove(s) }
        }
        if (list.size < AppAlerts.MAX_SOURCES) p.button("Link a camera app") { pickApp() }

        p.heading("4. Test it")
        p.text("Set opening hours so the shop is closed now (or wait until closing), then walk in front of the camera. " +
               "Within a few seconds this phone should sound the alarm and the owner should get the alert. " +
               "The \"last alert\" time above shows Guard heard it.", color = C.MUTED)

        p.heading("What to expect")
        p.text("• Alerts only while the shop is closed. In the day these cameras wake for every customer.\n" +
               "• The camera decides what counts as a person, not Guard's AI, so a cat or car headlights can set it off.\n" +
               "• The alert carries a picture only when the camera app sends one.\n" +
               "• If the camera's SIM runs out of data or its battery is flat, no alerts come. Keep the SIM topped up.",
               14f, color = C.MUTED)
    }

    private fun pickApp() {
        val linked = sources().map { it.pkg }.toSet()
        val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val apps = packageManager.queryIntentActivities(launcher, 0)
            .map { it.activityInfo.packageName to it.loadLabel(packageManager).toString() }
            .filter { (pkg, _) -> pkg != packageName && pkg !in linked }
            .distinctBy { it.first }
            .sortedWith(compareBy({ !AppAlerts.looksLikeCameraApp(it.second, it.first) }, { it.second.lowercase() }))
        if (apps.isEmpty()) {
            AlertDialog.Builder(this).setMessage("No apps found. Install the camera app first.").setPositiveButton("OK", null).show()
            return
        }
        AlertDialog.Builder(this).setTitle("Which app does the camera use?")
            .setItems(apps.map { it.second }.toTypedArray()) { _, i -> nameCamera(apps[i].first, apps[i].second) }
            .setNegativeButton("Cancel", null).show()
    }

    private fun nameCamera(pkg: String, label: String) {
        val input = EditText(this).apply { hint = "Front gate"; setSingleLine(); setTextColor(C.INK) }
        AlertDialog.Builder(this).setTitle("What does the shop call this camera?").setView(input)
            .setPositiveButton("Link") { _, _ ->
                val name = input.text.toString().trim().take(60).ifEmpty { "$label camera" }
                save(sources() + AppAlerts.Source(pkg, label, name))
            }
            .setNegativeButton("Cancel", null).show()
    }

    private fun confirmRemove(s: AppAlerts.Source) {
        AlertDialog.Builder(this).setMessage("Stop turning ${s.label} alerts into Guard alerts for ${s.cameraName}?")
            .setPositiveButton("Unlink") { _, _ -> save(sources().filter { it.pkg != s.pkg }) }
            .setNegativeButton("Cancel", null).show()
    }
}
