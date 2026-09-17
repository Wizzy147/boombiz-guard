package com.boombiz.guard.ui

import android.app.Activity
import android.graphics.Bitmap
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.WindowManager
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.media3.common.util.UnstableApi
import com.boombiz.guard.GuardApp
import com.boombiz.guard.ai.Frame
import com.boombiz.guard.camera.RtspFrameSource
import com.boombiz.guard.camera.StreamUrls
import com.boombiz.guard.data.Camera
import com.boombiz.guard.data.Vault
import com.boombiz.guard.watch.WatchService

/**
 * Watch one camera live, ON THIS DEVICE only — for aiming a camera, drawing
 * shelf and exit areas, and checking the shop from behind the counter. The
 * picture never leaves the phone or TV box (owner decision 2026-09-17: live
 * video over the internet would break that promise and cost data every month).
 *
 * It shows the frames the watcher is already decoding, so watching costs the
 * phone nothing extra. For a camera the watcher isn't on (switched off, or over
 * the two-camera limit) it opens its own stream while the screen is open.
 *
 * ~5 pictures a second, same as the AI. The screen stays on while it's open.
 */
@UnstableApi
class LiveViewActivity : Activity() {
    private val app get() = application as GuardApp
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var cam: Camera
    private lateinit var view: ImageView
    private lateinit var status: TextView
    private var bitmap: Bitmap? = null
    private var own: RtspFrameSource? = null
    @Volatile private var ownFrame: Frame? = null
    @Volatile private var ownError: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        cam = intent.getLongExtra("id", 0L).let { app.store.camera(it) } ?: return finish()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val p = page("Live · ${cam.name}")
        status = p.text("Connecting to the camera…", 15f, color = C.MUTED)
        view = ImageView(this).apply { adjustViewBounds = true }
        p.addView(view, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        p.text("This picture stays on this device. It is not recorded and it never goes to Boombiz — " +
               "only alert snapshots do. Press Back when you're done.", 14f, color = C.MUTED)
        p.button("Areas: shelves, exit, cashier, restricted, ignore", primary = false) {
            startActivity(android.content.Intent(this, ZoneEditorActivity::class.java).putExtra("id", cam.id))
        }
    }

    override fun onStart() {
        super.onStart()
        ui.post(tick)
    }

    override fun onStop() {
        super.onStop()
        ui.removeCallbacks(tick)
        own?.stop(); own = null
        ownFrame = null
    }

    override fun onDestroy() {
        own?.stop()
        super.onDestroy()
    }

    private val tick = object : Runnable {
        override fun run() {
            val watched = WatchService.status.lastFrame[cam.id]?.takeIf { fresh(it) }
            val f = watched ?: ownFrame
            if (watched == null && own == null) openOwnStream()
            if (f != null) {
                show(f)
                val people = WatchService.status.peopleNow[cam.id]?.takeIf { watched != null && it > 0 }
                status.text = if (people != null) "$people person(s) in view" else "Live"
                status.setTextColor(C.INK)
            } else {
                status.text = ownError ?: "Connecting to the camera…"
                status.setTextColor(if (ownError != null) C.RED else C.MUTED)
            }
            ui.postDelayed(this, 200)
        }
    }

    /** The watcher keeps its last frame even after a camera drops: don't show a stale picture as live. */
    private fun fresh(f: Frame) = SystemClock.elapsedRealtime() / 1000.0 - f.ts < 3.0

    private fun openOwnStream() {
        val url = StreamUrls.url(cam.host, cam.rtspPort, cam.brand, cam.channel, cam.customPath, cam.username,
            cam.passwordSealed?.let { Vault.open(it) })
        own = RtspFrameSource(this, url, 5.0, onFrame = { f -> ownFrame = f; ownError = null }, onState = { s, msg ->
            ownError = when (s) {
                RtspFrameSource.State.AUTH_ERROR -> "The camera refused the username or password."
                RtspFrameSource.State.OFFLINE -> "No picture from this camera. ${msg ?: ""}".trim()
                else -> null
            }
        }).also { it.start() }
    }

    private fun show(f: Frame) {
        val b = bitmap?.takeIf { it.width == f.width && it.height == f.height }
            ?: Bitmap.createBitmap(f.width, f.height, Bitmap.Config.ARGB_8888).also { bitmap = it }
        b.setPixels(f.argb, 0, f.width, 0, 0, f.width, f.height)
        view.setImageBitmap(b)
        view.invalidate()
    }
}
