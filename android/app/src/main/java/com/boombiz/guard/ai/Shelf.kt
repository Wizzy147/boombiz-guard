package com.boombiz.guard.ai

import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * Shelf interaction — a port of the PC agent's interactions/shelf.py (same
 * states, same tunables), in plain Kotlin on a small grey copy of the frame
 * instead of OpenCV.
 *
 * It never claims an item was taken. It answers two narrower questions:
 *  1. SHELF_INTERACTION — did a tracked, nearly-still person's arm reach into a
 *     shelf zone with hand motion there for ≥ 500 ms?
 *  2. UNRESOLVED_SHELF_INTERACTION — once they step away and the view settles,
 *     does the shelf look different from its clean "before" picture? And if so,
 *     was something TAKEN, REPLACED (possible product swap) or ADDED (put back)?
 *
 * The comparison is only trusted when the camera held still, the lighting held
 * and the scene is the same; otherwise the interaction closes without an alert.
 * If other people block the shelf for 5 s it stays UNRESOLVED but UNVERIFIED.
 *
 * Differences from the PC, all for a phone's CPU: the grey copy is 320 wide;
 * camera shift is a ±4 px block search on the 96×54 control picture instead of
 * phase correlation; and there is no pose model, so no concealment signal.
 */
class Grey(val w: Int, val h: Int, val px: FloatArray) {
    companion object {
        const val WIDTH = 320

        fun of(f: Frame): Grey {
            val h = maxOf(90, WIDTH * f.height / maxOf(1, f.width))
            return Grey(WIDTH, h, FrameOps.grey(f, WIDTH, h))
        }
    }
}

data class ShelfConfig(
    val reachOverlap: Float = 0.18f,
    val motionFrac: Float = 0.02f,
    val minInteractionS: Double = 0.5,
    val leaveGraceS: Double = 0.7,
    val settleS: Double = 1.0,
    val blockedTimeoutS: Double = 5.0,
    val changeFrac: Float = 0.015f,
    val strongChangeFrac: Float = 0.05f,
    val pixelDelta: Float = 28f,
    val maxDwellSpeed: Float = 0.06f,
    val maxCameraShiftPx: Float = 1.5f,
    val minShiftConfidence: Float = 0.08f,
    val maxBrightnessDelta: Float = 12f,
    val maxSceneChange: Float = 0.5f,
    val scenePixelDelta: Float = 40f,
    val bgSimilarity: Float = 0.35f,
    val swapDifference: Float = 0.5f,
    val minRingPx: Int = 20,
)

enum class InteractionState { APPROACHING, INTERACTING, PENDING_CHECK, RESOLVED, UNRESOLVED }

class Interaction(val id: Long, val trackId: Long, val shelf: Zone, val startedAt: Double) {
    var state = InteractionState.APPROACHING
    var interactingSince: Double? = null
    var lastOverlapAt = 0.0
    var endedAt: Double? = null
    var checkAfter: Double? = null
    var announced = false
    var changeFrac: Float? = null
    /** STRONG | WEAK | UNVERIFIED */
    var strength: String? = null
    var verifiable: Boolean? = null
    /** TAKEN | REPLACED | ADDED */
    var verdict: String? = null
    internal var before: FloatArray? = null
    internal var controlBefore: FloatArray? = null
}

data class ShelfEvent(val kind: String, val interaction: Interaction)

class ShelfEngine(private val cfg: ShelfConfig = ShelfConfig()) {

    private class View(var zone: Zone) {
        var bx = 0; var by = 0; var bw = 0; var bh = 0
        var mask = BooleanArray(0)
        var maskCount = 1
        var baseline: FloatArray? = null
        var prev: FloatArray? = null
        private var forW = -1; private var forH = -1

        /** Blurred grey crop of the shelf's bounding box; builds the polygon mask on first use / size change. */
        fun crop(g: Grey): FloatArray {
            if (g.w != forW || g.h != forH) {
                forW = g.w; forH = g.h
                val xs = zone.polygon.map { it.x }; val ys = zone.polygon.map { it.y }
                bx = (xs.min() * g.w).toInt().coerceIn(0, g.w - 2); by = (ys.min() * g.h).toInt().coerceIn(0, g.h - 2)
                bw = maxOf(2, minOf(g.w - bx, (xs.max() * g.w).toInt() - bx)); bh = maxOf(2, minOf(g.h - by, (ys.max() * g.h).toInt() - by))
                mask = BooleanArray(bw * bh) { i -> contains(zone.polygon, Pt((bx + i % bw + 0.5f) / g.w, (by + i / bw + 0.5f) / g.h)) }
                maskCount = maxOf(1, mask.count { it })
                baseline = null; prev = null
            }
            val roi = FloatArray(bw * bh)
            for (y in 0 until bh) System.arraycopy(g.px, (by + y) * g.w + bx, roi, y * bw, bw)
            return Img.blur5(roi, bw, bh)
        }

        fun changedFrac(a: FloatArray, b: FloatArray, delta: Float): Float {
            if (a.size != b.size) return 0f
            var n = 0
            for (i in a.indices) if (mask[i] && abs(a[i] - b[i]) > delta) n++
            return n.toFloat() / maskCount
        }

        /** Motion inside (upper body ∩ shelf) since the previous frame. */
        fun regionMotion(now: FloatArray, box: BBox, g: Grey, delta: Float): Float {
            val p = prev ?: return 0f
            if (p.size != now.size) return 0f
            val ub = box.upperBody()
            val x1 = maxOf((ub.x1 * g.w).toInt() - bx, 0); val y1 = maxOf((ub.y1 * g.h).toInt() - by, 0)
            val x2 = minOf((ub.x2 * g.w).toInt() - bx, bw); val y2 = minOf((ub.y2 * g.h).toInt() - by, bh)
            if (x2 <= x1 || y2 <= y1) return 0f
            var moved = 0; var inside = 0
            for (y in y1 until y2) for (x in x1 until x2) {
                val i = y * bw + x
                if (!mask[i]) continue
                inside++
                if (abs(now[i] - p[i]) > delta) moved++
            }
            return moved.toFloat() / maxOf(1, inside)
        }
    }

    private val views = LinkedHashMap<Long, View>()
    private val active = LinkedHashMap<Pair<Long, Long>, Interaction>()
    private var nextId = 1L

    val hasShelves get() = views.isNotEmpty()

    fun setZones(shelves: List<Zone>) {
        val ids = shelves.map { it.id }.toSet()
        views.keys.retainAll(ids)
        for (z in shelves) views.getOrPut(z.id) { View(z) }.zone = z
        active.keys.removeAll { it.second !in ids }
    }

    private fun occupied(v: View, boxes: Collection<BBox>) = boxes.any { reachOverlap(it, v.zone.polygon) > 0.05f }

    /** [tracks]: confirmed people in view now; [speeds]: their foot speed in frame-widths per second. */
    fun update(g: Grey, tracks: Map<Long, BBox>, now: Double, speeds: Map<Long, Float> = emptyMap()): List<ShelfEvent> {
        if (views.isEmpty()) return emptyList()
        val control = Img.control(g)
        val events = ArrayList<ShelfEvent>()
        for ((sid, view) in views) {
            val crop = view.crop(g)
            val occ = occupied(view, tracks.values)
            if (!occ) {
                val base = view.baseline
                if (base == null || base.size != crop.size) view.baseline = crop.copyOf()
                else if (active.none { (k, i) -> k.second == sid && i.state == InteractionState.PENDING_CHECK }) {
                    for (i in base.indices) base[i] = base[i] * 0.95f + crop[i] * 0.05f // absorbs lighting drift
                }
            }

            for ((tid, box) in tracks) {
                val ov = reachOverlap(box, view.zone.polygon)
                if (ov < cfg.reachOverlap) continue
                val ia = active.getOrPut(tid to sid) {
                    Interaction(nextId++, tid, view.zone, now).apply {
                        before = view.baseline?.copyOf() ?: crop.copyOf()
                        controlBefore = control
                    }
                }
                if (ia.state == InteractionState.PENDING_CHECK) { ia.state = InteractionState.INTERACTING; ia.checkAfter = null } // came back
                ia.lastOverlapAt = now
                val motion = view.regionMotion(crop, box, g, cfg.pixelDelta)
                val dwelling = (speeds[tid] ?: 0f) <= cfg.maxDwellSpeed
                if (!dwelling && !ia.announced) {
                    ia.interactingSince = null // walking past: the 500 ms clock restarts
                } else if (motion >= cfg.motionFrac) {
                    val since = ia.interactingSince ?: now
                    ia.interactingSince = since
                    if (!ia.announced && now - since >= cfg.minInteractionS) {
                        ia.state = InteractionState.INTERACTING
                        ia.announced = true
                        events += ShelfEvent("SHELF_INTERACTION", ia)
                    }
                }
            }
            view.prev = crop

            val iter = active.entries.iterator()
            while (iter.hasNext()) {
                val (key, it) = iter.next()
                if (key.second != sid) continue
                val gone = key.first !in tracks || now - it.lastOverlapAt >= cfg.leaveGraceS
                if (it.state == InteractionState.APPROACHING) { if (gone) iter.remove(); continue }
                if (it.state == InteractionState.INTERACTING && gone) {
                    it.state = InteractionState.PENDING_CHECK
                    it.endedAt = it.lastOverlapAt
                    it.checkAfter = now + cfg.settleS
                }
                if (it.state != InteractionState.PENDING_CHECK || now < (it.checkAfter ?: now)) continue
                if (!occ) {
                    val before = it.before ?: crop
                    val frac = view.changedFrac(before, crop, cfg.pixelDelta)
                    val (shift, conf) = Img.cameraShift(it.controlBefore, control)
                    val bright = Img.medianShift(before, crop, view.mask)
                    val scene = Img.sceneChange(it.controlBefore, control, cfg.scenePixelDelta)
                    val moved = shift > cfg.maxCameraShiftPx && (conf >= cfg.minShiftConfidence || shift > 3 * cfg.maxCameraShiftPx)
                    val verifiable = !moved && bright <= cfg.maxBrightnessDelta && scene <= cfg.maxSceneChange
                    it.changeFrac = frac
                    it.verifiable = verifiable
                    if (frac >= cfg.changeFrac && verifiable) it.verdict = verdict(view, before, crop)
                    if (frac >= cfg.changeFrac && verifiable && it.verdict != "ADDED") {
                        it.state = InteractionState.UNRESOLVED
                        it.strength = if (frac >= cfg.strongChangeFrac) "STRONG" else "WEAK"
                        events += ShelfEvent("UNRESOLVED_SHELF_INTERACTION", it)
                    } else {
                        it.state = InteractionState.RESOLVED
                        events += ShelfEvent("RESOLVED", it)
                        view.baseline = crop.copyOf()
                    }
                    iter.remove()
                } else if (now - (it.endedAt ?: now) > cfg.blockedTimeoutS) {
                    it.state = InteractionState.UNRESOLVED
                    it.strength = "UNVERIFIED"
                    events += ShelfEvent("UNRESOLVED_SHELF_INTERACTION", it)
                    iter.remove()
                }
            }
        }
        return events
    }

    /** TAKEN / REPLACED / ADDED for the biggest changed patch, or null if it can't tell. */
    private fun verdict(v: View, before: FloatArray, after: FloatArray): String? {
        val w = v.bw; val h = v.bh
        var changed = BooleanArray(w * h) { v.mask[it] && abs(before[it] - after[it]) > cfg.pixelDelta }
        changed = Img.erode(Img.dilate(changed, w, h, 2), w, h, 2) // close small gaps
        val comp = Img.largestComponent(changed, w, h) ?: return null
        val area = comp.count { it }
        val r = maxOf(3, (0.25 * sqrt(area.toDouble())).roundToInt())
        val grown = Img.dilate(comp, w, h, r)
        val ring = BooleanArray(w * h) { grown[it] && !changed[it] && v.mask[it] }
        if (ring.count { it } < cfg.minRingPx) return null
        val surroundings = Img.pick(after, ring)
        val beforeIsBg = Img.histDistance(Img.pick(before, comp), surroundings) < cfg.bgSimilarity
        val afterIsBg = Img.histDistance(Img.pick(after, comp), surroundings) < cfg.bgSimilarity
        return when {
            beforeIsBg && !afterIsBg -> "ADDED"
            !beforeIsBg && afterIsBg -> "TAKEN"
            !beforeIsBg && !afterIsBg && Img.histDistance(Img.pick(before, comp), Img.pick(after, comp)) >= cfg.swapDifference -> "REPLACED"
            else -> "TAKEN" // can't tell what: still a real change
        }
    }
}

/** The few image operations the shelf check needs, on FloatArray greys (0–255). */
internal object Img {
    const val CW = 96
    const val CH = 54
    private const val MIN_SHIFT_GAIN = 0.03f

    /** 5-tap binomial blur (≈ OpenCV GaussianBlur 5×5), edges clamped. */
    fun blur5(src: FloatArray, w: Int, h: Int): FloatArray {
        val k = floatArrayOf(1f, 4f, 6f, 4f, 1f)
        val tmp = FloatArray(src.size)
        for (y in 0 until h) for (x in 0 until w) {
            var s = 0f
            for (d in -2..2) s += k[d + 2] * src[y * w + (x + d).coerceIn(0, w - 1)]
            tmp[y * w + x] = s / 16f
        }
        val out = FloatArray(src.size)
        for (y in 0 until h) for (x in 0 until w) {
            var s = 0f
            for (d in -2..2) s += k[d + 2] * tmp[(y + d).coerceIn(0, h - 1) * w + x]
            out[y * w + x] = s / 16f
        }
        return out
    }

    /** 96×54 area-average of the whole picture, lightly blurred: only for "did the camera move / the light change". */
    fun control(g: Grey): FloatArray {
        val out = FloatArray(CW * CH); val cnt = IntArray(CW * CH)
        for (y in 0 until g.h) {
            val oy = y * CH / g.h
            for (x in 0 until g.w) { val o = oy * CW + x * CW / g.w; out[o] += g.px[y * g.w + x]; cnt[o]++ }
        }
        for (i in out.indices) if (cnt[i] > 0) out[i] = out[i] / cnt[i]
        val b = out.copyOf()
        for (y in 1 until CH - 1) for (x in 1 until CW - 1) {
            val i = y * CW + x
            b[i] = (out[i - CW - 1] + 2 * out[i - CW] + out[i - CW + 1] + 2 * out[i - 1] + 4 * out[i] + 2 * out[i + 1] +
                    out[i + CW - 1] + 2 * out[i + CW] + out[i + CW + 1]) / 16f
        }
        return b
    }

    /**
     * → (camera shift in px at 96 wide, confidence). The offset (±4 px) that best
     * lines the two control pictures up; confidence = how much better it fits than
     * no shift at all. A featureless picture gives ~0 confidence.
     */
    fun cameraShift(a: FloatArray?, b: FloatArray): Pair<Float, Float> {
        if (a == null || a.size != b.size) return 0f to 0f
        fun err(dx: Int, dy: Int): Float {
            var s = 0f; var n = 0
            for (y in 4 until CH - 4) for (x in 4 until CW - 4) { s += abs(a[y * CW + x] - b[(y + dy) * CW + x + dx]); n++ }
            return s / n
        }
        val e0 = err(0, 0)
        var best = e0; var bx = 0; var by = 0
        for (dy in -4..4) for (dx in -4..4) {
            if (dx == 0 && dy == 0) continue
            val e = err(dx, dy)
            if (e < best - 1e-4f) { best = e; bx = dx; by = dy }
        }
        val conf = if (e0 <= 1e-3f) 0f else (e0 - best) / e0
        // A near-tie is not a move: on a plain wall every offset fits about as well,
        // and a changed product alone must never read as "the camera moved".
        if (conf < MIN_SHIFT_GAIN) return 0f to conf
        return hypot(bx.toFloat(), by.toFloat()) to conf
    }

    /** Share of the whole control picture that changed a lot: a knocked or turned camera changes most of it. */
    fun sceneChange(a: FloatArray?, b: FloatArray, delta: Float): Float {
        if (a == null || a.size != b.size) return 0f
        return a.indices.count { abs(a[it] - b[it]) > delta }.toFloat() / a.size
    }

    /** |median(after − before)| on the shelf: one item gone ≈ 0, a light switched off moves every pixel. */
    fun medianShift(before: FloatArray, after: FloatArray, mask: BooleanArray): Float {
        if (before.size != after.size) return 0f
        val d = FloatArray(mask.count { it }); var k = 0
        for (i in mask.indices) if (mask[i]) d[k++] = after[i] - before[i]
        if (d.isEmpty()) return 0f
        d.sort()
        return abs(d[d.size / 2])
    }

    fun dilate(m: BooleanArray, w: Int, h: Int, r: Int): BooleanArray {
        val rows = BooleanArray(m.size)
        for (y in 0 until h) for (x in 0 until w) {
            if (!m[y * w + x]) continue
            for (xx in maxOf(0, x - r)..minOf(w - 1, x + r)) rows[y * w + xx] = true
        }
        val out = BooleanArray(m.size)
        for (y in 0 until h) for (x in 0 until w) {
            if (!rows[y * w + x]) continue
            for (yy in maxOf(0, y - r)..minOf(h - 1, y + r)) out[yy * w + x] = true
        }
        return out
    }

    fun erode(m: BooleanArray, w: Int, h: Int, r: Int): BooleanArray {
        val inv = BooleanArray(m.size) { !m[it] }
        val grown = dilate(inv, w, h, r)
        return BooleanArray(m.size) { !grown[it] }
    }

    /** Biggest 8-connected blob, or null when nothing changed. */
    fun largestComponent(m: BooleanArray, w: Int, h: Int): BooleanArray? {
        val label = IntArray(m.size)
        var bestLabel = 0; var bestSize = 0; var next = 0
        val stack = IntArray(m.size)
        for (start in m.indices) {
            if (!m[start] || label[start] != 0) continue
            next++
            var sp = 0; var size = 0
            stack[sp++] = start; label[start] = next
            while (sp > 0) {
                val i = stack[--sp]; size++
                val x = i % w; val y = i / w
                for (dy in -1..1) for (dx in -1..1) {
                    val xx = x + dx; val yy = y + dy
                    if (xx !in 0 until w || yy !in 0 until h) continue
                    val j = yy * w + xx
                    if (m[j] && label[j] == 0) { label[j] = next; stack[sp++] = j }
                }
            }
            if (size > bestSize) { bestSize = size; bestLabel = next }
        }
        if (bestLabel == 0) return null
        return BooleanArray(m.size) { label[it] == bestLabel }
    }

    fun pick(px: FloatArray, m: BooleanArray): FloatArray {
        val out = FloatArray(m.count { it }); var k = 0
        for (i in m.indices) if (m[i]) out[k++] = px[i]
        return out
    }

    /** Bhattacharyya distance of two pixel sets' 16-bin histograms: 0 same, 1 unrelated. */
    fun histDistance(a: FloatArray, b: FloatArray): Float {
        if (a.isEmpty() || b.isEmpty()) return 0f
        val ha = DoubleArray(16); val hb = DoubleArray(16)
        for (v in a) ha[(v.toInt() / 16).coerceIn(0, 15)]++
        for (v in b) hb[(v.toInt() / 16).coerceIn(0, 15)]++
        var bc = 0.0
        for (i in 0 until 16) bc += sqrt(ha[i] / a.size * hb[i] / b.size)
        return sqrt(maxOf(0.0, 1.0 - bc)).toFloat()
    }
}
