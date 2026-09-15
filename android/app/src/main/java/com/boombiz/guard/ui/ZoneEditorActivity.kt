package com.boombiz.guard.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.os.Bundle
import android.view.MotionEvent
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import androidx.media3.common.util.UnstableApi
import com.boombiz.guard.GuardApp
import com.boombiz.guard.ai.Pt
import com.boombiz.guard.ai.Zone
import com.boombiz.guard.ai.ZoneType
import com.boombiz.guard.watch.WatchService

/**
 * Draw areas on the camera's picture by dragging a box:
 *   Restricted — anyone stepping in (feet inside) sounds the alarm, day or night
 *   Ignore     — people standing here are never counted (a TV screen, a mirror, the pavement)
 * Needs a touchscreen; on a TV box, draw zones from a phone before moving the setup.
 */
@UnstableApi
class ZoneEditorActivity : Activity() {
    private val app get() = application as GuardApp
    private var cameraId = 0L
    private lateinit var canvas: ZoneCanvas

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        cameraId = intent.getLongExtra("id", 0L)
        val cam = app.store.camera(cameraId) ?: return finish()
        val p = page("Areas · ${cam.name}")
        p.text("Drag a box over the area on the picture, then choose what it is.", color = C.MUTED)
        val frame = WatchService.status.lastFrame[cameraId]
        if (frame == null) {
            p.text("No picture from this camera yet. Save the camera, wait until it says “working” on the home screen, then come back.", color = C.RED)
            return
        }
        val bmp = Bitmap.createBitmap(frame.argb, frame.width, frame.height, Bitmap.Config.ARGB_8888)
        canvas = ZoneCanvas(this, bmp, app.store.zones(cameraId)) { a, b -> ask(a, b) }
        p.addView(canvas, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
            (resources.displayMetrics.widthPixels - dp(40)) * frame.height / frame.width))
        for (z in app.store.zones(cameraId)) {
            p.button("Remove “${z.name}” (${if (z.type == ZoneType.RESTRICTED) "restricted" else "ignore"})", primary = false) {
                app.store.deleteZone(z.id); WatchService.reload(this); recreate()
            }
        }
    }

    private fun ask(a: Pt, b: Pt) {
        val poly = Zone.rect(a, b) ?: return
        val name = EditText(this).apply { hint = "Name, like Stockroom door"; setTextColor(C.INK) }
        AlertDialog.Builder(this).setTitle("What is this area?").setView(name)
            .setPositiveButton("Restricted") { _, _ -> save(name.text.toString(), ZoneType.RESTRICTED, poly) }
            .setNeutralButton("Ignore") { _, _ -> save(name.text.toString(), ZoneType.IGNORE, poly) }
            .setNegativeButton("Cancel") { _, _ -> canvas.clearDraft() }
            .show()
    }

    private fun save(n: String, type: ZoneType, poly: List<Pt>) {
        app.store.addZone(Zone(0, cameraId, n.trim().ifEmpty { if (type == ZoneType.RESTRICTED) "Restricted area" else "Ignored area" }.take(60), type, poly))
        WatchService.reload(this)
        recreate()
    }

    class ZoneCanvas(ctx: Context, private val bmp: Bitmap, private val zones: List<Zone>, private val done: (Pt, Pt) -> Unit) : View(ctx) {
        private var start: Pt? = null
        private var end: Pt? = null
        private val fillR = Paint().apply { color = 0x55DC2626; style = Paint.Style.FILL }
        private val fillI = Paint().apply { color = 0x55334155; style = Paint.Style.FILL }
        private val stroke = Paint().apply { color = Color.YELLOW; style = Paint.Style.STROKE; strokeWidth = 5f }

        fun clearDraft() { start = null; end = null; invalidate() }

        private fun norm(e: MotionEvent) = Pt((e.x / width).coerceIn(0f, 1f), (e.y / height).coerceIn(0f, 1f))

        private fun rect(a: Pt, b: Pt) = RectF(minOf(a.x, b.x) * width, minOf(a.y, b.y) * height, maxOf(a.x, b.x) * width, maxOf(a.y, b.y) * height)

        override fun onDraw(c: Canvas) {
            c.drawBitmap(bmp, null, RectF(0f, 0f, width.toFloat(), height.toFloat()), null)
            for (z in zones) {
                val xs = z.polygon.map { it.x }; val ys = z.polygon.map { it.y }
                c.drawRect(rect(Pt(xs.min(), ys.min()), Pt(xs.max(), ys.max())), if (z.type == ZoneType.RESTRICTED) fillR else fillI)
            }
            val s = start; val e = end
            if (s != null && e != null) c.drawRect(rect(s, e), stroke)
        }

        override fun onTouchEvent(e: MotionEvent): Boolean {
            when (e.action) {
                MotionEvent.ACTION_DOWN -> { start = norm(e); end = start }
                MotionEvent.ACTION_MOVE -> end = norm(e)
                MotionEvent.ACTION_UP -> { end = norm(e); val s = start; val en = end; if (s != null && en != null) done(s, en) }
            }
            invalidate()
            return true
        }
    }
}
