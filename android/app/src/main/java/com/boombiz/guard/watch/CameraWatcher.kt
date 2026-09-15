package com.boombiz.guard.watch

import com.boombiz.guard.ai.Detection
import com.boombiz.guard.ai.Frame
import com.boombiz.guard.ai.FrameOps
import com.boombiz.guard.ai.TamperDetector
import com.boombiz.guard.ai.Track
import com.boombiz.guard.ai.TrackState
import com.boombiz.guard.ai.Tracker
import com.boombiz.guard.ai.Zone
import com.boombiz.guard.ai.ZoneType
import com.boombiz.guard.ai.contains

/**
 * One camera's analysis, frame by frame (the core of the PC's ai/worker.py):
 *
 *   frame ─► person detector (shared) ─► drop people standing in IGNORE zones
 *         ─► tracker ─► RESTRICTED zone entry (foot point)
 *         ─► after hours: a confirmed person visible ≥ 2 s anywhere in view
 *         ─► tamper check (~1/s on a 160×90 grey copy)
 *
 * Pure logic: no Android types, so it runs in JVM unit tests with a fake detector.
 */
class CameraWatcher(
    val cameraId: Long,
    zones: List<Zone>,
    private val detect: (Frame) -> List<Detection>,
    private val afterHoursPersistS: Double = 2.0,
) {
    data class Event(val kind: String, val cameraId: Long, val trackId: Long?, val detail: String?)

    private val tracker = Tracker()
    private val tamper = TamperDetector()
    private var restricted = zones.filter { it.type == ZoneType.RESTRICTED }
    private var ignore = zones.filter { it.type == ZoneType.IGNORE }
    private val inRestricted = HashMap<Long, Set<Long>>() // track → restricted zone ids
    var aiFps = 0.0
        private set
    private val times = ArrayDeque<Double>()
    var lastPeople = 0
        private set

    fun setZones(zones: List<Zone>) {
        restricted = zones.filter { it.type == ZoneType.RESTRICTED }
        ignore = zones.filter { it.type == ZoneType.IGNORE }
    }

    fun process(f: Frame, storeOpen: Boolean): List<Event> {
        val now = f.ts
        val out = ArrayList<Event>()
        val dets = detect(f).filter { d -> ignore.none { contains(it.polygon, d.box.foot()) } }
        val ended = tracker.update(dets, now)
        ended.forEach { inRestricted.remove(it) }
        val visible: List<Track> = tracker.tracks.filter { it.lastSeen == now && it.state == TrackState.ACTIVE }
        lastPeople = visible.size

        for (t in visible) {
            val zonesNow = restricted.filter { contains(it.polygon, t.box.foot()) }.map { it.id }.toSet()
            val before = inRestricted[t.id] ?: emptySet()
            for (zid in zonesNow - before) {
                out += Event("RESTRICTED_ZONE_ENTRY", cameraId, t.id, restricted.first { it.id == zid }.name)
            }
            inRestricted[t.id] = zonesNow
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
