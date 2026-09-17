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
 *   Shelf      — products: a hand reaching in and the shelf changing is watched
 *   Exit       — the door: leaving after a shelf change sounds the alarm
 *   Cashier    — the pay point: passing it means the person may have paid
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
        p.text("To catch theft in the day, draw each shelf of products, the exit door and the cashier. " +
            "Guard alerts when someone reaches into a shelf, the shelf looks different afterwards, and they head for the exit. " +
            "It can't tell which product, and works best when the camera looks straight at the shelf.", 14f, color = C.MUTED)
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
            p.button("Remove “${z.name}” (${z.type.label})", primary = false) {
                app.store.deleteZone(z.id); WatchService.reload(this); recreate()
            }
        }
    }

    private fun ask(a: Pt, b: Pt) {
        val poly = Zone.rect(a, b) ?: return
        val types = listOf(
            ZoneType.SHELF to "Shelf of products",
            ZoneType.EXIT to "Exit door",
            ZoneType.CASHIER to "Cashier / pay point",
            ZoneType.RESTRICTED to "Restricted (staff only)",
            ZoneType.IGNORE to "Ignore (TV, mirror, street)",
        )
        AlertDialog.Builder(this).setTitle("What is this area?")
            .setItems(types.map { it.second }.toTypedArray()) { _, i -> askName(types[i].first, poly) }
            .setOnCancelListener { canvas.clearDraft() }
            .show()
    }

    private fun askName(type: ZoneType, poly: List<Pt>) {
        val name = EditText(this).apply { hint = "Name, like ${type.defaultName} 1"; setTextColor(C.INK) }
        AlertDialog.Builder(this).setTitle("Name this ${type.label} area").setView(name)
            .setPositiveButton("Save") { _, _ -> save(name.text.toString(), type, poly) }
            .setNegativeButton("Cancel") { _, _ -> canvas.clearDraft() }
            .show()
    }

    private fun save(n: String, type: ZoneType, poly: List<Pt>) {
        app.store.addZone(Zone(0, cameraId, n.trim().ifEmpty { type.defaultName }.take(60), type, poly))
        WatchService.reload(this)
        recreate()
    }

    class ZoneCanvas(ctx: Context, private val bmp: Bitmap, private val zones: List<Zone>, private val done: (Pt, Pt) -> Unit) : View(ctx) {
        private var start: Pt? = null
        private var end: Pt? = null
        private val fills = mapOf(
            ZoneType.RESTRICTED to 0x55DC2626, ZoneType.IGNORE to 0x55334155, ZoneType.SHELF to 0x552563EB,
            ZoneType.EXIT to 0x55F59E0B, ZoneType.CASHIER to 0x5516A34A,
        ).mapValues { (_, c) -> Paint().apply { color = c; style = Paint.Style.FILL } }
        private val stroke = Paint().apply { color = Color.YELLOW; style = Paint.Style.STROKE; strokeWidth = 5f }

        fun clearDraft() { start = null; end = null; invalidate() }

        private fun norm(e: MotionEvent) = Pt((e.x / width).coerceIn(0f, 1f), (e.y / height).coerceIn(0f, 1f))

        private fun rect(a: Pt, b: Pt) = RectF(minOf(a.x, b.x) * width, minOf(a.y, b.y) * height, maxOf(a.x, b.x) * width, maxOf(a.y, b.y) * height)

        override fun onDraw(c: Canvas) {
            c.drawBitmap(bmp, null, RectF(0f, 0f, width.toFloat(), height.toFloat()), null)
            for (z in zones) {
                val xs = z.polygon.map { it.x }; val ys = z.polygon.map { it.y }
                c.drawRect(rect(Pt(xs.min(), ys.min()), Pt(xs.max(), ys.max())), fills.getValue(z.type))
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
