package com.boombiz.guard.ai

import kotlin.math.sqrt

/**
 * Camera tamper check — a port of the PC agent's ai/tamper.py.
 *
 *   COVERED  the picture lost almost all its detail (tape, a hand, paint, a bag)
 *   TURNED   still detailed, but its edges no longer line up with the scene
 *            this camera learned
 *
 * Edges, not colours, so lights-off / night vision don't read as "turned".
 * A state must hold CONFIRM_S before it's reported, once per episode. The PC
 * uses Canny; here a Sobel magnitude threshold on the same 160×90 copy — close
 * enough for "is there structure, and is it the usual structure".
 */
class TamperDetector(
    private val sampleIntervalS: Double = 1.0,
    private val learnS: Double = 60.0,
    private val confirmS: Double = 30.0,
    private val clearS: Double = 10.0,
    private val minDetail: Float = 7f,
    private val minEdgeDensity: Float = 0.006f,
    private val minMatch: Float = 0.3f,
    private val learnRate: Float = 0.02f,
) {
    companion object {
        const val W = 160
        const val H = 90
        private const val EDGE_T = 60f

        fun edges(grey: FloatArray): BooleanArray {
            val g = blur(grey)
            val e = BooleanArray(W * H)
            for (y in 1 until H - 1) for (x in 1 until W - 1) {
                val i = y * W + x
                val gx = g[i - W + 1] + 2 * g[i + 1] + g[i + W + 1] - g[i - W - 1] - 2 * g[i - 1] - g[i + W - 1]
                val gy = g[i + W - 1] + 2 * g[i + W] + g[i + W + 1] - g[i - W - 1] - 2 * g[i - W] - g[i - W + 1]
                e[i] = sqrt(gx * gx + gy * gy) > EDGE_T
            }
            return e
        }

        private fun blur(g: FloatArray): FloatArray {
            val o = g.copyOf()
            for (y in 1 until H - 1) for (x in 1 until W - 1) {
                val i = y * W + x
                o[i] = (g[i - W - 1] + 2 * g[i - W] + g[i - W + 1] + 2 * g[i - 1] + 4 * g[i] + 2 * g[i + 1] +
                        g[i + W - 1] + 2 * g[i + W] + g[i + W + 1]) / 16f
            }
            return o
        }

        fun dilate(m: BooleanArray): BooleanArray {
            val o = BooleanArray(m.size)
            for (y in 0 until H) for (x in 0 until W) {
                if (!m[y * W + x]) continue
                for (dy in -1..1) for (dx in -1..1) {
                    val yy = y + dy; val xx = x + dx
                    if (yy in 0 until H && xx in 0 until W) o[yy * W + xx] = true
                }
            }
            return o
        }

        /** 0 = no better than chance, 1 = the same; weaker of both directions (see tamper.py). */
        fun edgeMatch(ref: BooleanArray, cur: BooleanArray): Float {
            if (ref.none { it } || cur.none { it }) return 0f
            val refD = dilate(ref); val curD = dilate(cur)
            fun corrected(a: BooleanArray, cover: BooleanArray): Float {
                var hits = 0; var total = 0; var cov = 0
                for (i in a.indices) { if (a[i]) { total++; if (cover[i]) hits++ }; if (cover[i]) cov++ }
                val chance = cov.toFloat() / cover.size
                return (hits.toFloat() / total - chance) / maxOf(1e-6f, 1f - chance)
            }
            return minOf(corrected(cur, refD), corrected(ref, curD))
        }
    }

    private var ref: FloatArray? = null
    private var learned = 0.0
    private var lastT: Double? = null
    private var suspect: Pair<String, Double>? = null
    private var reported = false
    private var okSince: Double? = null
    var lastState = "OK"
        private set

    /** → "COVERED" | "TURNED" once, when confirmed; otherwise null. */
    fun observe(grey: FloatArray, now: Double): String? {
        lastT?.let { if (now - it < sampleIntervalS) return null }
        val dt = lastT?.let { minOf(5.0, now - it) } ?: 0.0
        lastT = now

        val mean = grey.average().toFloat()
        val detail = sqrt(grey.fold(0f) { a, v -> a + (v - mean) * (v - mean) } / grey.size)
        val e = edges(grey)
        val density = e.count { it }.toFloat() / e.size
        var state = if (detail < minDetail || density < minEdgeDensity) "COVERED" else "OK"
        val r = ref
        if (state == "OK" && r != null && learned >= learnS) {
            if (edgeMatch(BooleanArray(r.size) { r[it] > 0.5f }, e) < minMatch) state = "TURNED"
        }
        lastState = state

        if (state == "OK") {
            // Learn only from a normal picture, so a covered camera never becomes "the usual scene".
            ref = if (r == null) FloatArray(e.size) { if (e[it]) 1f else 0f }
                  else FloatArray(e.size) { (1 - learnRate) * r[it] + learnRate * (if (e[it]) 1f else 0f) }
            learned += dt
            suspect = null
            if (reported) {
                val since = okSince ?: now.also { okSince = it }
                if (now - since >= clearS) { reported = false; okSince = null }
            }
            return null
        }
        okSince = null
        val s = suspect
        if (s == null || s.first != state) { suspect = state to now; return null }
        if (!reported && now - s.second >= confirmS) { reported = true; return state }
        return null
    }
}
