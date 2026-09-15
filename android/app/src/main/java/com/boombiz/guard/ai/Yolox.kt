package com.boombiz.guard.ai

import kotlin.math.exp

/**
 * YOLOX raw-grid decoding + person-only NMS, mirroring the PC agent's
 * ai/detector.py: outputs (1, N, 85) decoded with strides 8/16/32, score =
 * objectness × person class, boxes mapped back through the letterbox ratio,
 * then normalised to the original frame. Pure Kotlin so it's unit-tested on
 * the JVM.
 */
object Yolox {
    private const val PERSON = 0
    private const val C = 85

    fun decode(
        out: FloatArray, inW: Int, inH: Int, ratio: Float, frameW: Int, frameH: Int,
        threshold: Float = 0.5f, nms: Float = 0.45f, minBoxFrac: Float = 0.02f,
    ): List<Detection> {
        val boxes = ArrayList<FloatArray>() // x, y, w, h in frame pixels
        val scores = ArrayList<Float>()
        var row = 0
        for (s in intArrayOf(8, 16, 32)) {
            val hs = inH / s; val ws = inW / s
            for (gy in 0 until hs) for (gx in 0 until ws) {
                val o = row * C
                row++
                if (o + C > out.size) break
                val score = out[o + 4] * out[o + 5 + PERSON]
                if (score < threshold) continue
                val cx = (out[o] + gx) * s; val cy = (out[o + 1] + gy) * s
                val w = exp(out[o + 2]) * s; val h = exp(out[o + 3]) * s
                boxes += floatArrayOf((cx - w / 2) / ratio, (cy - h / 2) / ratio, w / ratio, h / ratio)
                scores += score
            }
        }
        val keep = nms(boxes, scores, nms)
        return keep.mapNotNull { i ->
            val (x, y, bw, bh) = boxes[i].let { listOf(it[0], it[1], it[2], it[3]) }
            val b = BBox(maxOf(0f, x / frameW), maxOf(0f, y / frameH), minOf(1f, (x + bw) / frameW), minOf(1f, (y + bh) / frameH))
            if (b.h >= minBoxFrac) Detection(b, scores[i]) else null
        }
    }

    fun nms(boxes: List<FloatArray>, scores: List<Float>, thresh: Float): List<Int> {
        val order = scores.indices.sortedByDescending { scores[it] }
        val keep = ArrayList<Int>()
        val dead = BooleanArray(boxes.size)
        for (i in order) {
            if (dead[i]) continue
            keep += i
            val a = boxes[i].let { BBox(it[0], it[1], it[0] + it[2], it[1] + it[3]) }
            for (j in order) {
                if (dead[j] || j == i) continue
                val b = boxes[j].let { BBox(it[0], it[1], it[0] + it[2], it[1] + it[3]) }
                if (iou(a, b) > thresh) dead[j] = true
            }
        }
        return keep
    }
}
