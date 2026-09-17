package com.boombiz.guard

import com.boombiz.guard.cloud.SyncQueue
import com.boombiz.guard.data.Incident
import com.boombiz.guard.media.ClipBuffer
import com.boombiz.guard.media.Yuv
import com.boombiz.guard.watch.IncidentRules
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The alert video: which frames go in it, what the cloud is told, and the colour conversion. */
class ClipTest {

    /** Frames at 5 a second, as the watcher feeds them (without Android's JPEG encoder). */
    private fun ring(from: Double, to: Double, into: ClipBuffer) {
        var t = from
        while (t <= to + 1e-9) { into.addShot(ClipBuffer.Shot(ByteArray(8), t)); t += 0.2 }
    }

    @Test fun theClipStartsBeforeTheAlertAndKeepsRecordingAfterIt() {
        val b = ClipBuffer()
        ring(90.0, 100.0, b)                       // 10 s already in memory when it happens
        assertNull("nothing to film yet", ClipBuffer().startCapture("inc", 100.0))
        val cap = b.startCapture("inc", 100.0)!!
        assertEquals("the 5 s before, at 5 a second", 26, cap.frames.size)
        ring(100.2, 112.0, b)                      // the alert's 10 s, then past the end
        assertTrue("15 s of frames, nothing after the end: ${cap.frames.size}", cap.frames.size in 75..76)
        assertEquals(110.0, cap.frames.last().ts, 0.25)
        assertTrue(b.ready(111.5).contains(cap))
        assertTrue("a finished clip is handed over once", b.ready(112.0).isEmpty())
    }

    @Test fun theBufferNeverGrowsPastItsFewSeconds() {
        val b = ClipBuffer()
        ring(0.0, 60.0, b)
        val cap = b.startCapture("inc", 60.0)!!
        assertTrue("only the last ~10 s are kept", cap.frames.size <= 55)
        assertTrue(cap.frames.first().ts >= 50.0)
    }

    @Test fun captureTakesTheSecondsBeforeAndAfterTheAlert() {
        val cap = ClipBuffer.Capture("inc", triggerTs = 100.0, preS = 5.0, postS = 10.0)
        assertTrue(cap.wants(95.0)); assertTrue(cap.wants(100.0)); assertTrue(cap.wants(110.0))
        assertFalse("older than the 5 s before", cap.wants(94.9))
        assertFalse("after the 10 s that follow", cap.wants(110.1))
        assertEquals(110.0, cap.endTs, 1e-9)
        assertFalse(cap.done(110.5))
        assertTrue(cap.done(111.5))
    }

    @Test fun clipLengthIsCapped() {
        val cap = ClipBuffer.Capture("inc", 100.0, ClipBuffer.PRE_S, minOf(ClipBuffer.POST_S, ClipBuffer.MAX_CLIP_S - ClipBuffer.PRE_S))
        assertEquals(ClipBuffer.MAX_CLIP_S, cap.endTs - (cap.triggerTs - ClipBuffer.PRE_S), 1e-9)
    }

    @Test fun onlySeriousAlertsCarryAVideo() {
        assertTrue("HIGH" in SyncQueue.CLIP_SEVERITIES && "CRITICAL" in SyncQueue.CLIP_SEVERITIES)
        assertFalse("LOW" in SyncQueue.CLIP_SEVERITIES)
        assertTrue("the picture is sent before the video", IncidentRules.SNAPSHOT_PRIORITY < IncidentRules.CLIP_PRIORITY)
    }

    @Test fun theCloudIsToldWhetherAVideoIsComing() {
        fun inc(sev: String, cam: Long?) = Incident("i", "BG-1", "AFTER_HOURS_INTRUSION", sev, cam, "Front",
            "Person inside after hours", null, "2026-09-17T21:00:00Z", "UNREVIEWED", null, null, "i.jpg")
        assertTrue(SyncQueue.payload(inc("CRITICAL", 1)).getBoolean("clip_expected"))
        assertFalse(SyncQueue.payload(inc("LOW", 1)).getBoolean("clip_expected"))
        assertFalse("the device itself has no camera to film with", SyncQueue.payload(inc("CRITICAL", null)).getBoolean("clip_expected"))
        assertNull(SyncQueue.payload(inc("CRITICAL", 1)).opt("confidence").takeIf { it != org.json.JSONObject.NULL })
    }

    @Test fun colourConversionForTheVideoEncoder() {
        val white = IntArray(4) { 0xFFFFFFFF.toInt() }
        val nv12 = Yuv.nv12(white, 2, 2, 2)
        assertEquals(6, nv12.size)
        assertEquals(255.toByte(), nv12[0])                       // Y: white
        assertEquals(128.toByte(), nv12[4]); assertEquals(128.toByte(), nv12[5]) // U, V: no colour
        val black = Yuv.nv12(IntArray(4) { 0xFF000000.toInt() }, 2, 2, 2)
        assertEquals(0.toByte(), black[0])
        // Red: NV21 swaps U and V, which is what the JPEG encoder wants.
        val red = IntArray(4) { 0xFFFF0000.toInt() }
        assertEquals(Yuv.nv12(red, 2, 2, 2)[4], Yuv.nv21(red, 2, 2, 2)[5])
        assertEquals(Yuv.nv12(red, 2, 2, 2)[5], Yuv.nv21(red, 2, 2, 2)[4])
        assertTrue("red is mostly V", Yuv.nv12(red, 2, 2, 2)[5].toInt() and 0xFF > 200)
    }
}
