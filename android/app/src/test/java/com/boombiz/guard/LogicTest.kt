package com.boombiz.guard

import com.boombiz.guard.ai.BBox
import com.boombiz.guard.ai.DayHours
import com.boombiz.guard.ai.Detection
import com.boombiz.guard.ai.Frame
import com.boombiz.guard.ai.Pt
import com.boombiz.guard.ai.Schedule
import com.boombiz.guard.ai.TamperDetector
import com.boombiz.guard.ai.Tracker
import com.boombiz.guard.ai.Yolox
import com.boombiz.guard.ai.Zone
import com.boombiz.guard.ai.ZoneType
import com.boombiz.guard.ai.contains
import com.boombiz.guard.camera.StreamUrls
import com.boombiz.guard.cloud.CameraHealth
import com.boombiz.guard.cloud.HeartbeatReport
import com.boombiz.guard.watch.CameraWatcher
import com.boombiz.guard.watch.IncidentRules
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.DayOfWeek
import java.time.LocalDateTime
import kotlin.math.ln

class LogicTest {

    // ── geometry ─────────────────────────────────────────────────────
    @Test fun pointInPolygon() {
        val sq = listOf(Pt(0.2f, 0.2f), Pt(0.6f, 0.2f), Pt(0.6f, 0.6f), Pt(0.2f, 0.6f))
        assertTrue(contains(sq, Pt(0.4f, 0.4f)))
        assertFalse(contains(sq, Pt(0.7f, 0.4f)))
    }

    @Test fun misTapRectangleIsRefused() {
        assertNull(Zone.rect(Pt(0.5f, 0.5f), Pt(0.51f, 0.51f)))
        val poly = Zone.rect(Pt(0.8f, 0.9f), Pt(0.1f, 0.2f))!!
        assertEquals(Pt(0.1f, 0.2f), poly[0])
        assertEquals(poly, Zone.decode(Zone(0, 1, "x", ZoneType.RESTRICTED, poly).encode()))
    }

    // ── business hours (same rules as zones/schedule.py) ────────────
    @Test fun hours() {
        val mon = LocalDateTime.of(2026, 9, 14, 10, 0) // a Monday
        assertTrue("no hours = always open", Schedule.isOpen(emptyList(), mon))
        val day = listOf(DayHours(DayOfWeek.MONDAY, "08:00", "20:00"))
        assertTrue(Schedule.isOpen(day, mon))
        assertFalse(Schedule.isOpen(day, mon.withHour(21)))
        val night = listOf(DayHours(DayOfWeek.MONDAY, "18:00", "02:00"))
        assertTrue(Schedule.isOpen(night, mon.withHour(23)))
        assertTrue(Schedule.isOpen(night, mon.withHour(1)))
        assertFalse(Schedule.isOpen(night, mon.withHour(10)))
        assertFalse(Schedule.isOpen(listOf(DayHours(DayOfWeek.MONDAY, null, null, closed = true)), mon))
        assertFalse(Schedule.valid("8am"))
    }

    // ── YOLOX decode ─────────────────────────────────────────────────
    /** One strong person anchor in the stride-8 grid at cell (10, 20), plus a weak non-person. */
    @Test fun yoloxDecodesPersonAndMapsBackThroughLetterbox() {
        val inW = 416; val inH = 416
        val n = (52 * 52 + 26 * 26 + 13 * 13)
        val out = FloatArray(n * 85)
        val row = 20 * 52 + 10
        val o = row * 85
        out[o] = 0.5f; out[o + 1] = 0.5f              // centre = (10.5*8, 20.5*8) = (84, 164)
        out[o + 2] = ln(5f); out[o + 3] = ln(10f)      // w = 40, h = 80
        out[o + 4] = 0.9f; out[o + 5] = 0.9f           // person score 0.81
        val o2 = (row + 1) * 85
        out[o2 + 4] = 0.9f; out[o2 + 6] = 0.9f         // class 1 only → ignored
        val ratio = 416f / 640f                        // a 640×360 frame
        val dets = Yolox.decode(out, inW, inH, ratio, 640, 360)
        assertEquals(1, dets.size)
        val b = dets[0].box
        assertEquals((84 - 20) / ratio / 640, b.x1, 1e-3f)
        assertEquals((164 + 40) / ratio / 360, b.y2, 1e-3f)
        assertEquals(0.81f, dets[0].confidence, 1e-4f)
    }

    @Test fun nmsKeepsTheBestOfOverlappingBoxes() {
        val keep = Yolox.nms(listOf(floatArrayOf(0f, 0f, 10f, 10f), floatArrayOf(1f, 1f, 10f, 10f), floatArrayOf(50f, 50f, 10f, 10f)),
            listOf(0.6f, 0.9f, 0.7f), 0.45f)
        assertEquals(listOf(1, 2), keep)
    }

    // ── tracker ──────────────────────────────────────────────────────
    @Test fun trackerConfirmsThenEnds() {
        val t = Tracker()
        val d = Detection(BBox(0.4f, 0.2f, 0.5f, 0.8f), 0.9f)
        t.update(listOf(d), 0.0); t.update(listOf(d), 0.2)
        assertEquals(1, t.tracks.size)
        assertEquals(com.boombiz.guard.ai.TrackState.ACTIVE, t.tracks[0].state)
        val ended = t.update(emptyList(), 6.0)
        assertEquals(1, ended.size)
        assertTrue(t.tracks.isEmpty())
    }

    // ── watcher: after hours, restricted, ignore ────────────────────
    private fun frame(ts: Double) = Frame(64, 36, IntArray(64 * 36) { if ((it / 4) % 2 == 0) -1 else 0xFF000000.toInt() }, ts)

    @Test fun afterHoursNeedsTwoSecondsAndTheShopClosed() {
        val person = Detection(BBox(0.4f, 0.2f, 0.5f, 0.8f), 0.9f)
        val w = CameraWatcher(1, emptyList(), { listOf(person) })
        val kinds = (0..15).flatMap { w.process(frame(it * 0.2), storeOpen = false).map { e -> e.kind } }
        assertTrue("AFTER_HOURS_PERSON" in kinds)
        val w2 = CameraWatcher(1, emptyList(), { listOf(person) })
        assertTrue((0..15).flatMap { w2.process(frame(it * 0.2), storeOpen = true) }.none { it.kind == "AFTER_HOURS_PERSON" })
        val brief = CameraWatcher(1, emptyList(), { listOf(person) })
        assertTrue((0..5).flatMap { brief.process(frame(it * 0.2), storeOpen = false) }.none { it.kind == "AFTER_HOURS_PERSON" })
    }

    @Test fun restrictedEntryUsesTheFeetAndIgnoreZonesDropPeople() {
        val zone = Zone(7, 1, "Stockroom", ZoneType.RESTRICTED, Zone.rect(Pt(0.3f, 0.6f), Pt(0.7f, 1f))!!)
        val inside = Detection(BBox(0.4f, 0.2f, 0.5f, 0.8f), 0.9f) // feet at y=0.74
        val w = CameraWatcher(1, listOf(zone), { listOf(inside) })
        val ev = (0..3).flatMap { w.process(frame(it * 0.2), storeOpen = true) }.filter { it.kind == "RESTRICTED_ZONE_ENTRY" }
        assertEquals(1, ev.size)
        assertEquals("Stockroom", ev[0].detail)

        val tv = Zone(8, 1, "TV", ZoneType.IGNORE, Zone.rect(Pt(0f, 0f), Pt(1f, 1f))!!)
        val w2 = CameraWatcher(1, listOf(zone, tv), { listOf(inside) })
        assertTrue((0..15).flatMap { w2.process(frame(it * 0.2), storeOpen = false) }.isEmpty())
    }

    // ── tamper ───────────────────────────────────────────────────────
    @Test fun coveredCameraIsReportedOnceAfterThirtySeconds() {
        val t = TamperDetector()
        val flat = FloatArray(TamperDetector.W * TamperDetector.H) { 20f }
        var hits = 0
        for (s in 0..70) if (t.observe(flat, s.toDouble()) == "COVERED") hits++
        assertEquals(1, hits)
    }

    @Test fun detailedSceneIsNotCovered() {
        val t = TamperDetector()
        val stripes = FloatArray(TamperDetector.W * TamperDetector.H) { if ((it % TamperDetector.W) / 8 % 2 == 0) 220f else 30f }
        for (s in 0..70) assertNull(t.observe(stripes, s.toDouble()))
    }

    // ── incidents ────────────────────────────────────────────────────
    @Test fun sameTypesAndSeveritiesAsThePcAgent() {
        assertEquals("AFTER_HOURS_INTRUSION" to "CRITICAL", IncidentRules.RULES["AFTER_HOURS_PERSON"]!!.let { it.type to it.severity })
        assertEquals("RESTRICTED_AREA_INCIDENT" to "HIGH", IncidentRules.RULES["RESTRICTED_ZONE_ENTRY"]!!.let { it.type to it.severity })
        assertTrue(IncidentRules.ALARMS["CAMERA_OFFLINE"]!!.afterClosingOnly)
    }

    @Test fun dedupMergesWithinTheWindow() {
        val d = IncidentRules.Dedup()
        val r = IncidentRules.RULES["AFTER_HOURS_PERSON"]!!
        assertTrue(d.isNew(r, 1, 5, 0.0))
        assertFalse(d.isNew(r, 1, 9, 30.0))  // per camera, not per person
        assertTrue(d.isNew(r, 2, 5, 30.0))   // another camera
        assertTrue(d.isNew(r, 1, 5, 61.0))
        val off = IncidentRules.RULES["CAMERA_OFFLINE"]!!
        assertTrue(d.isNew(off, 1, null, 0.0)); assertFalse(d.isNew(off, 1, null, 9999.0))
        d.clear("CAMERA_OFFLINE", 1)
        assertTrue(d.isNew(off, 1, null, 10000.0))
    }

    // ── privacy: the heartbeat never carries addresses or passwords ─
    @Test fun heartbeatHasNoAddressesOrCredentials() {
        val json = HeartbeatReport.build("android-0.1.0", listOf(CameraHealth(1, "Entrance", true), CameraHealth(2, "Store", false)),
            41.0, 12.3, 4.26, true, 3, null, listOf("ack:1")).toString()
        assertFalse(Regex("""\d+\.\d+\.\d+\.\d+|rtsp|password|admin""").containsMatchIn(json))
        assertTrue("\"camera_online\":1" in json)
        assertTrue("\"ai_fps\":4.3" in json)
        assertTrue("applied_commands" in json)
    }

    // ── stream URLs ──────────────────────────────────────────────────
    @Test fun streamUrls() {
        assertEquals("rtsp://admin:p%40ss%20w@192.168.1.64:554/Streaming/Channels/302",
            StreamUrls.url("192.168.1.64", 554, "HIKVISION", 3, null, "admin", "p@ss w"))
        assertEquals("rtsp://10.0.0.9:554/cam/realmonitor?channel=2&subtype=1", StreamUrls.safe("10.0.0.9", 554, "DAHUA", 2, null))
        assertTrue(StreamUrls.isLan("172.20.1.5")); assertFalse(StreamUrls.isLan("8.8.8.8")); assertFalse(StreamUrls.isLan("cam.example.com"))
    }
}
