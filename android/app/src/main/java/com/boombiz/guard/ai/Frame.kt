package com.boombiz.guard.ai

/** One decoded camera frame, ARGB_8888 packed ints, plus its capture time (seconds, monotonic). */
class Frame(val width: Int, val height: Int, val argb: IntArray, val ts: Double)

object FrameOps {
    /**
     * YOLOX input: BGR, 0–255, letterboxed top-left with pad value 114, NCHW
     * float — exactly the PC agent's detector._preprocess. Nearest-neighbour
     * sampling (the model is scale-robust; a bilinear pass costs 4× here).
     * → (tensor, ratio)
     */
    fun letterbox(f: Frame, inW: Int, inH: Int): Pair<FloatArray, Float> {
        val r = minOf(inH.toFloat() / f.height, inW.toFloat() / f.width)
        val rw = (f.width * r).toInt(); val rh = (f.height * r).toInt()
        val plane = inW * inH
        val t = FloatArray(3 * plane) { 114f }
        for (y in 0 until rh) {
            val sy = minOf(f.height - 1, (y / r).toInt())
            val row = sy * f.width
            for (x in 0 until rw) {
                val p = f.argb[row + minOf(f.width - 1, (x / r).toInt())]
                val i = y * inW + x
                t[i] = (p and 0xFF).toFloat()                     // B
                t[plane + i] = ((p shr 8) and 0xFF).toFloat()     // G
                t[2 * plane + i] = ((p shr 16) and 0xFF).toFloat() // R
            }
        }
        return t to r
    }

    /** Small greyscale copy (area-average) for the tamper check. */
    fun grey(f: Frame, w: Int, h: Int): FloatArray {
        val out = FloatArray(w * h)
        val cnt = IntArray(w * h)
        for (y in 0 until f.height) {
            val oy = y * h / f.height
            for (x in 0 until f.width) {
                val p = f.argb[y * f.width + x]
                val g = 0.299f * ((p shr 16) and 0xFF) + 0.587f * ((p shr 8) and 0xFF) + 0.114f * (p and 0xFF)
                val o = oy * w + x * w / f.width
                out[o] += g; cnt[o]++
            }
        }
        for (i in out.indices) if (cnt[i] > 0) out[i] = out[i] / cnt[i]
        return out
    }
}
