package com.boombiz.guard.ui

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.media3.common.util.UnstableApi
import com.boombiz.guard.GuardApp
import com.boombiz.guard.watch.AppAlertListener
import com.boombiz.guard.watch.AppAlerts
import com.boombiz.guard.watch.WatchService
import com.boombiz.guard.watch.Watchdog
import kotlin.concurrent.thread

/**
 * Home screen: is Guard watching, which cameras work, is the cloud linked —
 * and the setup steps in order (activate → cameras → hours → keep running).
 */
@UnstableApi
class MainActivity : Activity() {
    private val app get() = application as GuardApp
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var statusText: TextView
    private val refresh = object : Runnable {
        override fun run() { renderStatus(); ui.postDelayed(this, 2000) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        Watchdog.schedule(this)
    }

    override fun onResume() {
        super.onResume()
        build()
        WatchService.startIfConfigured(this)
        ui.post(refresh)
    }

    override fun onPause() { ui.removeCallbacks(refresh); super.onPause() }

    private fun build() {
        val p = page("Boombiz Guard")
        statusText = p.text("", 16f)
        renderStatus()

        p.heading("1. Connect to Boombiz")
        if (app.cloud.isActivated) {
            val where = listOfNotNull(app.store.get("business_name"), app.store.get("location_name")).joinToString(" · ")
            p.text("Connected${if (where.isNotEmpty()) " to $where" else ""}.")
            p.button("Disconnect this device", primary = false) { PinLock.guard(this) { confirmForget() } }
        } else {
            p.text("Ask your Boombiz installer for an activation code (it looks like GARD-7K82-HQ41).", color = C.MUTED)
            val code = p.field("Activation code", hint = "GARD-XXXX-XXXX")
            val name = p.field("Name for this device", "Guard phone")
            p.button("Activate") { activate(code, name) }
        }

        p.heading("2. Cameras")
        val cams = app.store.cameras()
        if (cams.isEmpty()) p.text("No cameras yet. Guard watches up to ${WatchService.MAX_CAMERAS} cameras on a phone or TV box.", color = C.MUTED)
        for (c in cams) {
            p.button("${c.name}${if (!c.enabled) " (off)" else ""}", primary = false) {
                PinLock.guard(this) { startActivity(Intent(this, CameraEditActivity::class.java).putExtra("id", c.id)) }
            }
            // No PIN to watch (owner decision 2026-09-17): staff behind the counter
            // should be able to look at a camera without the settings PIN.
            p.button("Watch ${c.name} live", primary = false) {
                startActivity(Intent(this, LiveViewActivity::class.java).putExtra("id", c.id))
            }
        }
        if (cams.size < WatchService.MAX_CAMERAS) p.button("Add a camera") {
            PinLock.guard(this) { startActivity(Intent(this, CameraEditActivity::class.java)) }
        }
        val appCams = AppAlerts.decode(app.store.get(AppAlerts.KEY))
        p.button(if (appCams.isEmpty()) "4G / solar camera (V380, CamHi, UBox)" else "4G / solar cameras: ${appCams.size} linked", primary = false) {
            PinLock.guard(this) { startActivity(Intent(this, CameraAppsActivity::class.java)) }
        }

        p.heading("3. Opening hours")
        val hours = app.store.hours()
        p.text(if (hours.isEmpty()) "Not set. Until you set them, Guard treats the shop as always open and after-hours alerts stay off."
               else "Set. Anyone seen while the shop is closed sounds the alarm and alerts the owner.",
               color = if (hours.isEmpty()) C.RED else C.INK)
        p.button("Set opening hours", primary = hours.isEmpty()) { PinLock.guard(this) { startActivity(Intent(this, HoursActivity::class.java)) } }

        p.heading("4. Keep Guard running")
        val pm = getSystemService(PowerManager::class.java)
        if (pm.isIgnoringBatteryOptimizations(packageName)) p.text("Battery saving is off for Guard.", color = C.GREEN)
        else {
            p.text("This device may stop Guard to save battery. Turn that off for Guard.", color = C.RED)
            p.button("Allow Guard to run all the time") {
                runCatching { startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))) }
            }
        }
        p.text("Tecno, Infinix and itel phones: also open Phone Master → App auto-start and allow Boombiz Guard. " +
               "On a TV box, leave the box on — the TV screen can be off, but the alarm only plays through the TV speaker when the TV is on.",
               14f, color = C.MUTED)

        p.heading("5. Where to put it")
        p.text("Out of sight, but inside a camera's view — for example locked in the recorder cabinet, with a camera able to see the cabinet. " +
               "Keep it plugged in and on the shop Wi-Fi. If someone takes it off its charger while the shop is closed, " +
               "it sounds the alarm and alerts the owner at once.", 14f, color = C.MUTED)

        p.heading("6. Lock the settings")
        if (PinLock.isSet(this)) {
            p.text("Locked with a PIN. Anyone can see this status screen; cameras, hours, incidents and disconnecting need the PIN.", color = C.GREEN)
            p.button("Change the PIN", primary = false) { PinLock.guard(this) { PinLock.setPin(this) { build() } } }
        } else {
            p.text("No PIN yet — anyone in the shop can change or switch off Guard here.", color = C.RED)
            p.button("Set a PIN") { PinLock.setPin(this) { build() } }
        }

        p.divider()
        p.button("Incidents", primary = false) { PinLock.guard(this) { startActivity(Intent(this, IncidentsActivity::class.java)) } }
        p.button("Test the alarm sound", primary = false) {
            PinLock.guard(this) { com.boombiz.guard.watch.Siren(this).test() }
        }
    }

    /** Home button / another app: the next person at the device needs the PIN again. */
    override fun onUserLeaveHint() { PinLock.lockNow(); super.onUserLeaveHint() }

    private fun renderStatus() {
        val s = WatchService.status
        val cams = app.store.cameras().filter { it.enabled }
        val appCams = AppAlerts.decode(app.store.get(AppAlerts.KEY))
        val lines = mutableListOf<String>()
        lines += when {
            s.problem != null -> "⚠ ${s.problem}"
            cams.isEmpty() && appCams.isNotEmpty() -> if (WatchService.running) "Listening for camera app alerts" else "Starting…"
            cams.isEmpty() -> "Not watching yet — add a camera."
            !WatchService.running -> "Starting…"
            else -> "Watching ${cams.size} camera${if (cams.size == 1) "" else "s"} · AI ${"%.1f".format(s.aiFps)} checks/s"
        }
        for (c in cams) {
            val err = s.cameraErrors[c.id]
            lines += "• ${c.name}: " + when {
                err == null -> "connecting…"
                err.isEmpty() -> "working" + (s.peopleNow[c.id]?.takeIf { it > 0 }?.let { " · $it in view" } ?: "")
                else -> err
            } + (s.speakerErrors[c.id]?.let { " · camera speaker: $it" } ?: "")
        }
        for (a in appCams) lines += "• ${a.cameraName} (${a.label} app): " +
            if (!AppAlertListener.granted(this)) "Guard isn't allowed to read its alerts — open 4G / solar cameras"
            else s.appAlertHeard[a.pkg]?.let { "last alert ${android.text.format.DateFormat.getTimeFormat(this).format(java.util.Date(it))}" } ?: "listening"
        if (s.charging == false) lines += "⚠ Not charging — plug this phone in and leave it plugged in."
        if (app.cloud.isActivated) lines += when (s.cloudOk) {
            true -> "Boombiz cloud: connected"
            false -> "Boombiz cloud: can't reach it — alerts wait on this device and send when the internet is back"
            null -> "Boombiz cloud: checking…"
        }
        statusText.text = lines.joinToString("\n")
        statusText.setTextColor(if (s.problem != null) C.RED else C.INK)
    }

    private fun activate(code: EditText, name: EditText) {
        val c = code.text.toString()
        if (c.isBlank()) { code.error = "Type the activation code."; return }
        val dialog = AlertDialog.Builder(this).setMessage("Connecting…").setCancelable(false).show()
        thread {
            val result = runCatching {
                app.cloud.activate(c, name.text.toString().ifBlank { "Guard phone" }, GuardApp.VERSION,
                    "Android ${Build.VERSION.RELEASE} (${Build.MANUFACTURER} ${Build.MODEL})")
            }
            ui.post {
                dialog.dismiss()
                result.onSuccess { where ->
                    AlertDialog.Builder(this).setMessage("This device is now connected${if (where.isNotEmpty()) " to $where" else ""}.")
                        .setPositiveButton("OK", null).show()
                    build()
                }.onFailure { e ->
                    AlertDialog.Builder(this).setMessage(e.message ?: "That didn't work. Try again.").setPositiveButton("OK", null).show()
                }
            }
        }
    }

    private fun confirmForget() {
        AlertDialog.Builder(this)
            .setMessage("Disconnect this device from Boombiz? It keeps watching and sounding the alarm here, but the owner stops getting alerts until it's activated again.")
            .setPositiveButton("Disconnect") { _, _ -> app.cloud.forget(); build() }
            .setNegativeButton("Cancel", null).show()
    }
}
