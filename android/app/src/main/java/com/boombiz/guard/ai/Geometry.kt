package com.boombiz.guard.ai

/**
 * Zone geometry on NORMALISED coordinates (0–1), same as the PC agent's
 * zones/geometry.py, so a zone drawn on the sub-stream fits any resolution.
 */
data class Pt(val x: Float, val y: Float)

data class BBox(val x1: Float, val y1: Float, val x2: Float, val y2: Float) {
    val w get() = x2 - x1
    val h get() = y2 - y1

    /** Where the person stands, not where their head is. */
    fun foot(frac: Float = 0.9f) = Pt((x1 + x2) / 2f, y1 + h * frac)
}

fun iou(a: BBox, b: BBox): Float {
    val ix = maxOf(0f, minOf(a.x2, b.x2) - maxOf(a.x1, b.x1))
    val iy = maxOf(0f, minOf(a.y2, b.y2) - maxOf(a.y1, b.y1))
    val inter = ix * iy
    val union = a.w * a.h + b.w * b.h - inter
    return if (union > 0f) inter / union else 0f
}

/** Ray casting; points on the edge count as inside. */
fun contains(poly: List<Pt>, p: Pt): Boolean {
    var inside = false
    var j = poly.size - 1
    for (i in poly.indices) {
        val (xi, yi) = poly[i]
        val (xj, yj) = poly[j]
        if ((yi > p.y) != (yj > p.y)) {
            val xCross = (xj - xi) * (p.y - yi) / (yj - yi) + xi
            if (p.x <= xCross) inside = !inside
        }
        j = i
    }
    return inside
}

fun area(poly: List<Pt>): Float {
    var s = 0f
    for (i in poly.indices) {
        val a = poly[i]
        val b = poly[(i + 1) % poly.size]
        s += a.x * b.y - b.x * a.y
    }
    return s / 2f
}

enum class ZoneType { RESTRICTED, IGNORE }

data class Zone(val id: Long, val cameraId: Long, val name: String, val type: ZoneType, val polygon: List<Pt>) {
    fun encode(): String = polygon.joinToString(";") { "${it.x},${it.y}" }

    companion object {
        const val MIN_AREA = 0.0005f

        fun decode(s: String): List<Pt> = s.split(";").filter { it.isNotBlank() }.map {
            val (x, y) = it.split(",")
            Pt(x.toFloat(), y.toFloat())
        }

        /** A dragged rectangle → polygon, clamped to the picture. Null when it's a mis-tap. */
        fun rect(a: Pt, b: Pt): List<Pt>? {
            val x1 = minOf(a.x, b.x).coerceIn(0f, 1f); val x2 = maxOf(a.x, b.x).coerceIn(0f, 1f)
            val y1 = minOf(a.y, b.y).coerceIn(0f, 1f); val y2 = maxOf(a.y, b.y).coerceIn(0f, 1f)
            val poly = listOf(Pt(x1, y1), Pt(x2, y1), Pt(x2, y2), Pt(x1, y2))
            return if (kotlin.math.abs(area(poly)) < MIN_AREA) null else poly
        }
    }
}
