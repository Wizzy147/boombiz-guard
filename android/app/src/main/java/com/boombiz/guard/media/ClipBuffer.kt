package com.boombiz.guard.media

import android.graphics.Bitmap
import android.graphics.Rect
import android.graphics.YuvImage
import com.boombiz.guard.ai.Frame
import java.io.ByteArrayOutputStream

/**
 * The last few seconds of one camera, as JPEG frames — the PC agent's
 * buffer/rolling_buffer.py. An alert opens a capture: the frames already in the
 * ring (up to [PRE_S] before it) plus everything until [POST_S] after it. The
 * capture, not the ring, is what becomes the clip, so a later alert can't eat
 * this one's frames.
 *
 * JPEG, not raw pixels: 15 s of 640×360 raw ARGB would be 46 MB of phone memory;
 * as JPEG the whole ring is a few hundred KB.
 */
class ClipBuffer(private val seconds: Double = RING_S, private val maxFrames: Int = 150) {

    class Shot(val jpeg: ByteArray, val ts: Double)

    class Capture(val incidentId: String, val triggerTs: Double, val preS: Double, val postS: Double) {
        val frames = ArrayList<Shot>()
        val endTs get() = triggerTs + postS
        fun wants(ts: Double) = ts in (triggerTs - preS)..endTs
        fun done(now: Double) = now > endTs + GRACE_S
    }

    private val ring = ArrayDeque<Shot>()
    private val captures = ArrayList<Capture>()

    /** Feed every decoded frame. Cheap when no clip is ever taken: one JPEG per frame, ~20 KB. */
    fun add(f: Frame, quality: Int = 60) = addShot(Shot(jpeg(f, quality), f.ts))

    @Synchronized
    internal fun addShot(shot: Shot) {
        ring.addLast(shot)
        while (ring.size > maxFrames || (ring.size > 1 && shot.ts - ring.first().ts > seconds)) ring.removeFirst()
        for (c in captures) if (c.wants(shot.ts)) c.frames += shot
    }

    /** Start a clip for an alert; → the capture, or null when the buffer has never seen a frame. */
    @Synchronized
    fun startCapture(incidentId: String, triggerTs: Double, preS: Double = PRE_S, postS: Double = POST_S): Capture? {
        if (ring.isEmpty()) return null
        val cap = Capture(incidentId, triggerTs, preS, minOf(postS, MAX_CLIP_S - preS))
        cap.frames += ring.filter { cap.wants(it.ts) }
        captures += cap
        return cap
    }

    /** Captures whose post-event window has passed, removed from the buffer. */
    @Synchronized
    fun ready(now: Double): List<Capture> {
        val out = captures.filter { it.done(now) }
        captures.removeAll(out.toSet())
        return out
    }

    @Synchronized
    fun clear() { ring.clear(); captures.clear() }

    companion object {
        const val RING_S = 10.0
        const val PRE_S = 5.0
        const val POST_S = 10.0
        const val MAX_CLIP_S = 15.0
        private const val GRACE_S = 1.0

        /** ARGB frame → JPEG, through YuvImage: no Bitmap allocation per frame on the hot path. */
        fun jpeg(f: Frame, quality: Int): ByteArray {
            val out = ByteArrayOutputStream(48 * 1024)
            val w = f.width and 1.inv(); val h = f.height and 1.inv()
            YuvImage(Yuv.nv21(f.argb, f.width, w, h), android.graphics.ImageFormat.NV21, w, h, null)
                .compressToJpeg(Rect(0, 0, w, h), quality, out)
            return out.toByteArray()
        }

        fun bitmap(jpeg: ByteArray): Bitmap? =
            android.graphics.BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)
    }
}

/** ARGB → YUV 4:2:0, the two layouts Android's JPEG and video encoders want. */
object Yuv {
    fun nv21(argb: IntArray, stride: Int, w: Int, h: Int): ByteArray = semiPlanar(argb, stride, w, h, vFirst = true)

    /** NV12 (U before V), what a MediaCodec AVC encoder takes as COLOR_FormatYUV420SemiPlanar. */
    fun nv12(argb: IntArray, stride: Int, w: Int, h: Int): ByteArray = semiPlanar(argb, stride, w, h, vFirst = false)

    private fun semiPlanar(argb: IntArray, stride: Int, w: Int, h: Int, vFirst: Boolean): ByteArray {
        val out = ByteArray(w * h * 3 / 2)
        var uv = w * h
        for (y in 0 until h) {
            for (x in 0 until w) {
                val p = argb[y * stride + x]
                val r = (p shr 16) and 0xFF; val g = (p shr 8) and 0xFF; val b = p and 0xFF
                out[y * w + x] = clamp((77 * r + 150 * g + 29 * b) shr 8)
                if (y % 2 == 0 && x % 2 == 0) {
                    val u = clamp(((-43 * r - 85 * g + 128 * b) shr 8) + 128)
                    val v = clamp(((128 * r - 107 * g - 21 * b) shr 8) + 128)
                    out[uv] = if (vFirst) v else u
                    out[uv + 1] = if (vFirst) u else v
                    uv += 2
                }
            }
        }
        return out
    }

    private fun clamp(v: Int): Byte = v.coerceIn(0, 255).toByte()
}
