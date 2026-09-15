package com.boombiz.guard.ai

/**
 * Compact ByteTrack, a straight port of the PC agent's ai/tracker.py:
 * high-confidence matches first, then a second chance against low-confidence
 * boxes (carries a person through partial occlusion). Constant-velocity
 * prediction, greedy IoU. A track id only means "probably the same person in
 * this camera session" — no appearance model, no face.
 */
data class Detection(val box: BBox, val confidence: Float)

enum class TrackState { NEW, ACTIVE, LOST }

class Track(val id: Long, var box: BBox, var confidence: Float, val firstSeen: Double, var lastSeen: Double) {
    var state = TrackState.NEW
    var hits = 1
    private var vx = 0f
    private var vy = 0f

    fun predict(now: Double): BBox {
        val dt = minOf(now - lastSeen, 1.0).toFloat()
        return BBox(box.x1 + vx * dt, box.y1 + vy * dt, box.x2 + vx * dt, box.y2 + vy * dt)
    }

    fun update(d: Detection, now: Double) {
        val dt = (now - lastSeen).toFloat()
        if (dt > 0f) {
            val ocx = (box.x1 + box.x2) / 2; val ocy = (box.y1 + box.y2) / 2
            val ncx = (d.box.x1 + d.box.x2) / 2; val ncy = (d.box.y1 + d.box.y2) / 2
            vx = 0.6f * vx + 0.4f * (ncx - ocx) / dt
            vy = 0.6f * vy + 0.4f * (ncy - ocy) / dt
        }
        box = d.box; confidence = d.confidence; lastSeen = now; hits++
    }
}

class Tracker(
    private val highThresh: Float = 0.5f,
    private val lowThresh: Float = 0.25f,
    private val newTrackThresh: Float = 0.65f,
    private val matchIou: Float = 0.15f,
    private val confirmHits: Int = 2,
    private val lostAfterS: Double = 2.0,
    private val endAfterS: Double = 5.0,
) {
    val tracks = mutableListOf<Track>()
    private var nextId = 1L

    private fun match(ts: List<Track>, ds: List<Detection>, now: Double): Triple<List<Pair<Int, Int>>, List<Int>, List<Int>> {
        val pairs = ArrayList<Triple<Float, Int, Int>>()
        for ((ti, t) in ts.withIndex()) {
            val p = t.predict(now)
            for ((di, d) in ds.withIndex()) pairs.add(Triple(iou(p, d.box), ti, di))
        }
        pairs.sortByDescending { it.first }
        val usedT = HashSet<Int>(); val usedD = HashSet<Int>(); val m = ArrayList<Pair<Int, Int>>()
        for ((s, ti, di) in pairs) {
            if (s < matchIou) break
            if (ti in usedT || di in usedD) continue
            usedT += ti; usedD += di; m += ti to di
        }
        return Triple(m, ts.indices.filter { it !in usedT }, ds.indices.filter { it !in usedD })
    }

    /** → ended track ids. */
    fun update(dets: List<Detection>, now: Double): List<Long> {
        val high = dets.filter { it.confidence >= highThresh }
        val low = dets.filter { it.confidence >= lowThresh && it.confidence < highThresh }
        val (m1, unT, unD) = match(tracks, high, now)
        for ((ti, di) in m1) tracks[ti].update(high[di], now)
        val rest = unT.map { tracks[it] }
        val (m2, _, _) = match(rest, low, now)
        for ((ti, di) in m2) rest[ti].update(low[di], now)
        for (di in unD) {
            val d = high[di]
            if (d.confidence >= newTrackThresh) tracks += Track(nextId++, d.box, d.confidence, now, now)
        }
        val ended = ArrayList<Long>()
        val it = tracks.iterator()
        while (it.hasNext()) {
            val t = it.next()
            val gap = now - t.lastSeen
            when {
                gap > endAfterS -> { ended += t.id; it.remove() }
                gap >= lostAfterS -> t.state = TrackState.LOST
                t.lastSeen == now -> t.state = if (t.hits >= confirmHits) TrackState.ACTIVE else TrackState.NEW
            }
        }
        return ended
    }
}
