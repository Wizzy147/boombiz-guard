package com.boombiz.guard.watch

import android.app.ActivityManager
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.os.BatteryManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.media3.common.util.UnstableApi
import com.boombiz.guard.GuardApp
import com.boombiz.guard.ai.Frame
import com.boombiz.guard.ai.PersonDetector
import com.boombiz.guard.ai.Schedule
import com.boombiz.guard.camera.CameraSpeaker
import com.boombiz.guard.camera.RtspFrameSource
import com.boombiz.guard.camera.StreamUrls
import com.boombiz.guard.cloud.CameraHealth
import com.boombiz.guard.cloud.HeartbeatReport
import com.boombiz.guard.cloud.SyncQueue
import com.boombiz.guard.data.Camera
import com.boombiz.guard.data.Incident
import com.boombiz.guard.data.Vault
import com.boombiz.guard.ui.MainActivity
import java.io.File
import java.io.FileOutputStream
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The watcher. A foreground service that runs 24/7 on the spare phone or
 * Android TV box in the shop:
 *
 *   cameras (RTSP, hardware decode) ─► CameraWatcher ─► incident rules + dedup
 *        ─► snapshot + local record ─► phone siren + notification ─► sync queue ─► cloud
 *   heartbeat every 2 min · sync every 5 s · offline check every 5 s · retention daily
 *
 * Detection, the local record and the siren never wait on the internet.
 */
@UnstableApi
class WatchService : Service() {

    private val app get() = application as GuardApp
    private val io = Executors.newScheduledThreadPool(2)
    private val analysis = Executors.newSingleThreadExecutor() // one frame at a time: keeps the phone cool
    private val speakers = Executors.newFixedThreadPool(MAX_CAMERAS) // a camera siren blocks while it plays
    private val speaking = ConcurrentHashMap<Long, CameraSpeaker>()
    private val busy = AtomicBoolean(false)
    private val sources = ConcurrentHashMap<Long, RtspFrameSource>()
    private val watchers = ConcurrentHashMap<Long, CameraWatcher>()
    private val cams = ConcurrentHashMap<Long, Camera>()
    private val state = ConcurrentHashMap<Long, RtspFrameSource.State>()
    private val everOnline = ConcurrentHashMap.newKeySet<Long>()
    private val offlineReported = ConcurrentHashMap.newKeySet<Long>()
    private val dedup = IncidentRules.Dedup()
    private val applied = mutableListOf<String>()
    private lateinit var siren: Siren
    private lateinit var sync: SyncQueue
    private lateinit var mediaDir: File
    private var detector: PersonDetector? = null
    private var wake: PowerManager.WakeLock? = null
    private var wifi: WifiManager.WifiLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startInForeground("Starting…")
        siren = Siren(this)
        mediaDir = File(filesDir, "media").apply { mkdirs() }
        sync = SyncQueue(app.store, app.cloud, mediaDir)
        wake = getSystemService(PowerManager::class.java).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "guard:watch").apply { acquire() }
        @Suppress("DEPRECATION")
        wifi = getSystemService(WifiManager::class.java).createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "guard:wifi").apply { acquire() }
        running = true
        registerReceiver(power, IntentFilter().apply {
            addAction(Intent.ACTION_POWER_CONNECTED); addAction(Intent.ACTION_POWER_DISCONNECTED)
        })
        status.charging = chargingNow()
        instance = this
        io.execute { boot() }
        io.execute { guard("app alerts") { drainAppAlerts() } } // alerts that arrived while Guard was stopped
        io.scheduleWithFixedDelay({ guard("offline") { checkOffline() } }, 5, 5, TimeUnit.SECONDS)
        io.scheduleWithFixedDelay({ guard("sync") { sync.process(); status.syncError = sync.lastError } }, 10, 5, TimeUnit.SECONDS)
        io.scheduleWithFixedDelay({ guard("heartbeat") { heartbeat() } }, 15, 120, TimeUnit.SECONDS)
        io.scheduleWithFixedDelay({ guard("retention") { retention() } }, 1, 24 * 60, TimeUnit.MINUTES)
        Watchdog.schedule(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_RELOAD) io.execute { guard("reload") { reloadCameras() } }
        if (intent?.action == ACTION_STOP_SIREN) stopSirens()
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        instance = null
        runCatching { unregisterReceiver(power) }
        sources.values.forEach { it.stop() }
        speaking.values.forEach { it.stop() }
        io.shutdownNow(); analysis.shutdownNow(); speakers.shutdownNow()
        detector?.close()
        wake?.takeIf { it.isHeld }?.release()
        wifi?.takeIf { it.isHeld }?.release()
        super.onDestroy()
    }

    private inline fun guard(what: String, block: () -> Unit) {
        try { block() } catch (t: Throwable) { Log.w(TAG, "$what failed", t) }
    }

    // ── cameras ──────────────────────────────────────────────────────
    private fun boot() {
        detector = try { PersonDetector.load(this) } catch (e: Exception) {
            status.problem = e.message ?: "The Guard AI couldn't start on this device."; null
        }
        reloadCameras()
    }

    private fun reloadCameras() {
        sources.values.forEach { it.stop() }
        sources.clear(); watchers.clear(); cams.clear(); state.clear()
        val det = detector ?: return
        val enabled = app.store.cameras().filter { it.enabled }.take(MAX_CAMERAS)
        for (c in enabled) {
            cams[c.id] = c
            watchers[c.id] = CameraWatcher(c.id, app.store.zones(c.id), det::detect)
            val url = StreamUrls.url(c.host, c.rtspPort, c.brand, c.channel, c.customPath, c.username, c.passwordSealed?.let { Vault.open(it) })
            val src = RtspFrameSource(this, url, FPS, onFrame = { f -> onFrame(c.id, f) }, onState = { s, msg -> onState(c.id, s, msg) })
            sources[c.id] = src
            src.start()
        }
        status.cameraCount = enabled.size
        updateNotification()
    }

    private fun onState(cameraId: Long, s: RtspFrameSource.State, msg: String?) {
        state[cameraId] = s
        if (s == RtspFrameSource.State.ONLINE) {
            everOnline += cameraId
            if (offlineReported.remove(cameraId)) dedup.clear("CAMERA_OFFLINE", cameraId)
        }
        status.cameraErrors[cameraId] = if (s == RtspFrameSource.State.ONLINE) "" else (msg ?: "")
        updateNotification()
    }

    private fun onFrame(cameraId: Long, f: Frame) {
        status.lastFrame[cameraId] = f // the camera screen shows it; never leaves the phone
        if (!busy.compareAndSet(false, true)) return // AI behind: drop, never backlog
        analysis.execute {
            try {
                val w = watchers[cameraId] ?: return@execute
                val open = Schedule.isOpen(app.store.hours(), LocalDateTime.now())
                for (e in w.process(f, open)) onEvent(e, f, open)
                status.aiFps = watchers.values.map { it.aiFps }.average().takeIf { !it.isNaN() } ?: 0.0
                status.peopleNow[cameraId] = w.lastPeople
            } catch (t: Throwable) {
                Log.w(TAG, "analysis failed", t)
            } finally { busy.set(false) }
        }
    }

    /** Camera gone for 30 s after it had worked: smashed, unplugged, cable cut, or the recorder is off. */
    private fun checkOffline() {
        val now = SystemClock.elapsedRealtime()
        val open = Schedule.isOpen(app.store.hours(), LocalDateTime.now())
        for ((id, src) in sources) {
            src.kickIfStalled(15)
            if (id !in everOnline || id in offlineReported) continue
            val last = src.lastFrameAt
            if (last > 0 && now - last > OFFLINE_AFTER_MS) {
                offlineReported += id
                onEvent(CameraWatcher.Event("CAMERA_OFFLINE", id, null, "No video from this camera for 30 seconds."), status.lastFrame[id], open)
            }
        }
    }

    // ── the watcher itself taken off its charger ─────────────────────
    private val power = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            val plugged = i.action == Intent.ACTION_POWER_CONNECTED
            status.charging = if (hasBattery()) plugged else null
            io.execute { guard("power") { onPower(plugged) } }
        }
    }

    private fun battery(): Intent? = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))

    /** A TV box has no battery: unplugging it just switches it off, so there's nothing to report from here. */
    private fun hasBattery() = battery()?.getBooleanExtra(BatteryManager.EXTRA_PRESENT, false) == true

    private fun chargingNow(): Boolean? = battery()?.takeIf { it.getBooleanExtra(BatteryManager.EXTRA_PRESENT, false) }
        ?.let { it.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0 }

    private fun onPower(plugged: Boolean) {
        if (plugged) { dedup.clear(IncidentRules.UNPLUGGED, 0); return }
        if (!hasBattery()) return
        val open = Schedule.isOpen(app.store.hours(), LocalDateTime.now())
        // Newest frame from any camera: whoever is standing there when the cable comes out.
        val frame = status.lastFrame.values.maxByOrNull { it.ts }
        onEvent(CameraWatcher.Event(if (open) "DEVICE_UNPLUGGED_OPEN" else "DEVICE_UNPLUGGED_CLOSED", 0, null,
            "The Guard phone was taken off its charger."), frame, open)
        sync.process() // don't wait for the 5 s loop: the phone may be switched off in seconds
    }

    // ── 4G / solar cameras: alarms from their own phone app ──────────
    private fun drainAppAlerts() {
        while (true) onAppAlert(pendingAlerts.poll() ?: return)
    }

    /** Only while the shop is closed: in the day these cameras wake for every customer. */
    private fun onAppAlert(a: AppAlert) {
        status.appAlertHeard[a.source.pkg] = a.at
        val open = Schedule.isOpen(app.store.hours(), LocalDateTime.now())
        if (open) return
        onEvent(CameraWatcher.Event(AppAlerts.EVENT, AppAlerts.dedupId(a.source.pkg), null, AppAlerts.detail(a.source, a.title, a.text)),
            null, open, picture = a.picture, cameraName = a.source.cameraName)
        sync.process() // an intruder alert shouldn't wait for the 5 s loop
    }

    // ── incidents ────────────────────────────────────────────────────
    /** [picture]/[cameraName]: for cameras Guard doesn't stream itself (camera-app alerts). */
    @Synchronized
    private fun onEvent(e: CameraWatcher.Event, frame: Frame?, storeOpen: Boolean, picture: Bitmap? = null, cameraName: String? = null) {
        val rule = IncidentRules.RULES[e.kind] ?: return
        if (!dedup.isNew(rule, e.cameraId, e.trackId, SystemClock.elapsedRealtime() / 1000.0)) return
        val id = UUID.randomUUID().toString()
        val day = LocalDate.now().format(DateTimeFormatter.BASIC_ISO_DATE)
        val ref = "BG-${app.store.get("location_code") ?: "LOC"}-$day-%06d".format(app.store.nextSeq(day))
        // An offline camera's last frame is stale — the snapshot would mislead.
        val snap = when {
            picture != null -> saveBitmap(id, picture)
            e.kind != "CAMERA_OFFLINE" -> frame?.let { saveSnapshot(id, it) }
            else -> null
        }
        val cam = cams[e.cameraId]
        val camId = e.cameraId.takeIf { it > 0L } // 0 = the device itself, below 0 = a camera app: no camera row
        val inc = Incident(id, ref, rule.type, e.severity ?: rule.severity, camId, cam?.name ?: cameraName, rule.title,
            e.detail, Instant.now().toString(), "UNREVIEWED", null, null, snap, e.confidence)
        app.store.insertIncident(inc)
        sync.enqueue(inc)

        IncidentRules.ALARMS[rule.type]?.let { a ->
            if (!(a.afterClosingOnly && storeOpen) && siren.sound(rule.type, a.seconds, a.cooldownS)) cameraSirens(a.seconds)
        }
        if (rule.type in URGENT) io.execute { guard("sync") { sync.process() } } // don't wait for the 5 s loop, or block the AI
        alertNotification(inc)
    }

    /** The same siren through every camera whose speaker the installer switched on (ONVIF back-channel). */
    private fun cameraSirens(seconds: Int) {
        for (c in cams.values.filter { it.speaker }) {
            if (speaking.containsKey(c.id)) continue
            val s = CameraSpeaker(c.host, c.rtspPort, StreamUrls.path(c.brand, c.channel, c.customPath, c.username,
                c.passwordSealed?.let { Vault.open(it) }), c.username, c.passwordSealed?.let { Vault.open(it) })
            speaking[c.id] = s
            speakers.execute {
                try { s.play(seconds); status.speakerErrors.remove(c.id) } catch (t: Throwable) {
                    status.speakerErrors[c.id] = t.message ?: "no sound"
                    Log.w(TAG, "camera speaker failed", t)
                } finally { speaking.remove(c.id) }
            }
        }
    }

    private fun stopSirens() {
        siren.stop()
        speaking.values.forEach { it.stop() }
    }

    private fun saveSnapshot(id: String, f: Frame): String? {
        val bmp = Bitmap.createBitmap(f.argb, f.width, f.height, Bitmap.Config.ARGB_8888)
        return saveBitmap(id, bmp).also { bmp.recycle() }
    }

    private fun saveBitmap(id: String, bmp: Bitmap): String? = runCatching {
        val name = "$id.jpg"
        FileOutputStream(File(mediaDir, name)).use { bmp.compress(Bitmap.CompressFormat.JPEG, 85, it) }
        name
    }.getOrNull()

    // ── cloud ────────────────────────────────────────────────────────
    private fun heartbeat() {
        if (!app.cloud.isActivated) return
        val am = getSystemService(ActivityManager::class.java)
        val mi = ActivityManager.MemoryInfo().also { am.getMemoryInfo(it) }
        val cameras = cams.values.map { CameraHealth(it.id, it.name, state[it.id] == RtspFrameSource.State.ONLINE) }
        val sending = synchronized(applied) { applied.toList() }
        val report = HeartbeatReport.build(
            GuardApp.VERSION, cameras, 100.0 * (mi.totalMem - mi.availMem) / mi.totalMem,
            filesDir.usableSpace / 1e9, status.aiFps, detector != null && cams.isNotEmpty(),
            app.store.pendingJobs(), app.store.lastIncidentAt(), sending,
        )
        val commands = try { app.cloud.heartbeat(report) } catch (e: Exception) { status.cloudOk = false; return }
        status.cloudOk = true
        synchronized(applied) { applied.removeAll(sending.toSet()) }
        for (i in 0 until commands.length()) {
            val c = commands.optJSONObject(i) ?: continue
            val cid = c.optString("id").ifBlank { null } ?: continue
            if (c.optString("type") in HeartbeatReport.ALLOWED_COMMANDS) {
                val localId = c.optString("local_incident_id")
                if (app.store.acknowledge(localId, "${c.optString("by", "Boombiz").take(60)} (remote)", c.optString("at", Instant.now().toString()))) {
                    stopSirens()
                    app.store.incident(localId)?.let { sync.enqueue(it) }
                }
            }
            synchronized(applied) { if (cid !in applied) applied += cid } // unknown types too: acknowledged, never executed
        }
    }

    private fun retention() {
        for (p in app.store.pruneIncidents(RETENTION_DAYS)) File(mediaDir, p).delete()
    }

    // ── notifications ────────────────────────────────────────────────
    private fun openApp() = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)

    private fun watchNotification(text: String): Notification =
        Notification.Builder(this, GuardApp.CH_WATCH).setContentTitle("Boombiz Guard is watching")
            .setContentText(text).setSmallIcon(android.R.drawable.ic_menu_view).setOngoing(true)
            .setContentIntent(openApp()).build()

    private fun startInForeground(text: String) {
        if (Build.VERSION.SDK_INT >= 34) startForeground(NOTE_WATCH, watchNotification(text), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else startForeground(NOTE_WATCH, watchNotification(text))
    }

    private fun updateNotification() {
        val on = state.values.count { it == RtspFrameSource.State.ONLINE }
        val apps = AppAlerts.decode(app.store.get(AppAlerts.KEY)).size
        val appText = if (apps > 0) "$apps camera app${if (apps == 1) "" else "s"} linked" else null
        val text = status.problem ?: listOfNotNull(if (cams.isEmpty()) null else "$on of ${cams.size} cameras working", appText)
            .joinToString(" · ").ifEmpty { "No cameras set up yet." }
        getSystemService(NotificationManager::class.java).notify(NOTE_WATCH, watchNotification(text))
    }

    private fun alertNotification(i: Incident) {
        val n = Notification.Builder(this, GuardApp.CH_ALERT).setContentTitle(i.title)
            .setContentText(listOfNotNull(i.cameraName, i.description).joinToString(" · "))
            .setSmallIcon(android.R.drawable.ic_dialog_alert).setAutoCancel(true).setContentIntent(openApp()).build()
        getSystemService(NotificationManager::class.java).notify(i.ref.hashCode(), n)
    }

    /** Live state for the status screen (same process). */
    class Status {
        @Volatile var problem: String? = null
        @Volatile var cameraCount = 0
        @Volatile var aiFps = 0.0
        @Volatile var cloudOk: Boolean? = null
        @Volatile var syncError: String? = null
        /** null = no battery (TV box) or unknown. */
        @Volatile var charging: Boolean? = null
        val cameraErrors = ConcurrentHashMap<Long, String>()
        /** Why the last siren couldn't play through a camera's own speaker. */
        val speakerErrors = ConcurrentHashMap<Long, String>()
        val peopleNow = ConcurrentHashMap<Long, Int>()
        val lastFrame = ConcurrentHashMap<Long, Frame>()
        /** Camera app package → when Guard last heard an alarm from it (ms). */
        val appAlertHeard = ConcurrentHashMap<String, Long>()
    }

    /** An alarm from a linked camera app, handed over by [AppAlertListener]. */
    class AppAlert(val source: AppAlerts.Source, val title: String?, val text: String?, val picture: Bitmap?,
                   val at: Long = System.currentTimeMillis())

    companion object {
        private const val TAG = "GuardWatch"
        const val ACTION_RELOAD = "com.boombiz.guard.RELOAD"
        const val ACTION_STOP_SIREN = "com.boombiz.guard.STOP_SIREN"
        const val MAX_CAMERAS = 2          // ~5 fps each on a 4 GB phone; more needs a PC
        const val FPS = 5.0
        private const val OFFLINE_AFTER_MS = 30_000L
        private val URGENT = setOf("POSSIBLE_UNPAID_EXIT", "POSSIBLE_PRODUCT_REPLACEMENT")
        private const val RETENTION_DAYS = 30
        private const val NOTE_WATCH = 1
        val status = Status()
        @Volatile var running = false
            private set
        @Volatile private var instance: WatchService? = null
        private val pendingAlerts = ConcurrentLinkedQueue<AppAlert>()

        fun startIfConfigured(ctx: Context) {
            val app = ctx.applicationContext as GuardApp
            if (app.store.cameras().none { it.enabled } && AppAlerts.decode(app.store.get(AppAlerts.KEY)).isEmpty()) return
            ctx.startForegroundService(Intent(ctx, WatchService::class.java))
        }

        /** From the notification listener (main thread): queue it, and wake the watcher if Android stopped it. */
        fun appAlert(ctx: Context, a: AppAlert) {
            pendingAlerts += a
            while (pendingAlerts.size > 20) pendingAlerts.poll() // never a backlog of stale alarms
            val s = instance
            if (s != null) s.io.execute { s.guard("app alert") { s.drainAppAlerts() } }
            else runCatching { ctx.startForegroundService(Intent(ctx, WatchService::class.java)) }
                .onFailure { Log.w(TAG, "couldn't start the watcher for a camera-app alert", it) }
        }

        fun reload(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, WatchService::class.java).setAction(ACTION_RELOAD))
        }
    }
}
