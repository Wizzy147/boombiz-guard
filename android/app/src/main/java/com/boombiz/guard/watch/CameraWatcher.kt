package com.boombiz.guard.watch

import com.boombiz.guard.ai.Detection
import com.boombiz.guard.ai.Frame
import com.boombiz.guard.ai.FrameOps
import com.boombiz.guard.ai.Grey
import com.boombiz.guard.ai.Pt
import com.boombiz.guard.ai.ShelfEngine
import com.boombiz.guard.ai.TamperDetector
import com.boombiz.guard.ai.Track
import com.boombiz.guard.ai.TrackState
import com.boombiz.guard.ai.Tracker
import com.boombiz.guard.ai.Zone
import com.boombiz.guard.ai.ZoneType
import com.boombiz.guard.ai.contains
import kotlin.math.hypot

/**
 * One camera's analysis, frame by frame (the core of the PC's ai/worker.py):
 *
 *   frame ─► person detector (shared) ─► drop people standing in IGNORE zones
 *         ─► tracker ─► RESTRICTED / EXIT / CASHIER zone entry (foot point)
 *         ─► SHELF zones: reach-in + shelf changed ─► unpaid exit / product swap
 *         ─► after hours: a confirmed person visible ≥ 2 s anywhere in view
 *         ─► tamper check (~1/s on a 160×90 grey copy)
 *
 * The shelf check only runs on a camera that has shelf zones, so a camera that
 * only guards a door costs nothing extra.
 *
 * Pure logic: no Android types, so it runs in JVM unit tests with a fake detector.
 */
class CameraWatcher(
    val cameraId: Long,
    zones: List<Zone>,
    private val detect: (Frame) -> List<Detection>,
    private val afterHoursPersistS: Double = 2.0,
) {
    /** [severity]/[confidence]: set when the event decides them itself (theft signals); else the rule's. */
    data class Event(val kind: String, val cameraId: Long, val trackId: Long?, val detail: String?,
                     val severity: String? = null, val confidence: String? = null)

    private val tracker = Tracker()
    private val tamper = TamperDetector()
    private val shelves = ShelfEngine()
    private val theft = TheftCorrelator(cameraId)
    private var entryZones = emptyList<Zone>()
    private var ignore = emptyList<Zone>()
    private val inZones = HashMap<Long, Set<Long>>() // track → entry zone ids its feet are in
    private val feet = HashMap<Long, Pair<Pt, Double>>()
    private val speeds = HashMap<Long, Float>()
    var aiFps = 0.0
        private set
    private val times = ArrayDeque<Double>()
    var lastPeople = 0
        private set

    init { setZones(zones) }

    fun setZones(zones: List<Zone>) {
        entryZones = zones.filter { it.type == ZoneType.RESTRICTED || it.type == ZoneType.EXIT || it.type == ZoneType.CASHIER }
        ignore = zones.filter { it.type == ZoneType.IGNORE }
        shelves.setZones(zones.filter { it.type == ZoneType.SHELF })
    }

    fun process(f: Frame, storeOpen: Boolean): List<Event> {
        val now = f.ts
        val out = ArrayList<Event>()
        val dets = detect(f).filter { d -> ignore.none { contains(it.polygon, d.box.foot()) } }
        val ended = tracker.update(dets, now)
        ended.forEach { inZones.remove(it); feet.remove(it); speeds.remove(it) }
        val visible: List<Track> = tracker.tracks.filter { it.lastSeen == now && it.state == TrackState.ACTIVE }
        lastPeople = visible.size

        val entries = ArrayList<TheftCorrelator.Entry>()
        for (t in visible) {
            val foot = t.box.foot()
            feet[t.id]?.let { (p, ts) ->
                val dt = now - ts
                if (dt > 0) speeds[t.id] = 0.5f * (speeds[t.id] ?: 0f) + 0.5f * (hypot(foot.x - p.x, foot.y - p.y) / dt.toFloat())
            }
            feet[t.id] = foot to now

            val zonesNow = entryZones.filter { contains(it.polygon, foot) }
            val before = inZones[t.id] ?: emptySet()
            for (z in zonesNow) {
                if (z.id in before) continue
                if (z.type == ZoneType.RESTRICTED) out += Event("RESTRICTED_ZONE_ENTRY", cameraId, t.id, z.name)
                else entries += TheftCorrelator.Entry(t.id, z)
            }
            inZones[t.id] = zonesNow.map { it.id }.toSet()
        }

        if (shelves.hasShelves || entries.isNotEmpty() || ended.isNotEmpty()) {
            val shelfEvents = if (shelves.hasShelves) {
                shelves.update(Grey.of(f), visible.associate { it.id to it.box }, now, speeds)
            } else emptyList()
            out += theft.process(now, shelfEvents, entries, ended)
        }

        if (!storeOpen) {
            visible.firstOrNull { now - it.firstSeen >= afterHoursPersistS }?.let {
                out += Event("AFTER_HOURS_PERSON", cameraId, it.id, "${visible.size} person(s) in view")
            }
        }

        tamper.observe(FrameOps.grey(f, TamperDetector.W, TamperDetector.H), now)?.let { kind ->
            out += Event("CAMERA_TAMPERED", cameraId, null,
                if (kind == "COVERED") "The camera's picture looks covered or blacked out. Check the camera now."
                else "The camera's picture looks turned away from its usual view. Check the camera now.")
        }

        times.addLast(now)
        while (times.size > 20) times.removeFirst()
        if (times.size > 1) aiFps = (times.size - 1) / maxOf(1e-6, times.last() - times.first())
        return out
    }
}
