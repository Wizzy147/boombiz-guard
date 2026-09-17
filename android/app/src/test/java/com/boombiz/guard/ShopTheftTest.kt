package com.boombiz.guard

import com.boombiz.guard.ai.BBox
import com.boombiz.guard.ai.Frame
import com.boombiz.guard.ai.Grey
import com.boombiz.guard.ai.Pt
import com.boombiz.guard.ai.ShelfEngine
import com.boombiz.guard.ai.ShelfEvent
import com.boombiz.guard.ai.Zone
import com.boombiz.guard.ai.ZoneType
import com.boombiz.guard.camera.Auth
import com.boombiz.guard.camera.G711
import com.boombiz.guard.camera.Rtp
import com.boombiz.guard.camera.Sdp
import com.boombiz.guard.camera.StreamUrls
import com.boombiz.guard.watch.IncidentRules
import com.boombiz.guard.watch.TheftCorrelator
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ShopTheftTest {

    // ── Wi-Fi camera presets ─────────────────────────────────────────
    @Test fun wifiCameraPaths() {
        assertEquals("rtsp://192.168.1.20:554/12", StreamUrls.safe("192.168.1.20", 554, "CAMHI", 1, null))
        assertEquals("rtsp://192.168.1.20:554/22", StreamUrls.safe("192.168.1.20", 554, "CAMHI", 2, null))
        assertEquals("rtsp://192.168.1.21:554/onvif2", StreamUrls.safe("192.168.1.21", 554, "YOOSEE", 1, null))
        assertEquals("rtsp://192.168.1.22:554/stream2", StreamUrls.safe("192.168.1.22", 554, "TAPO", 1, null))
        assertEquals("rtsp://192.168.1.23:554/h264Preview_01_sub", StreamUrls.safe("192.168.1.23", 554, "REOLINK", 1, null))
        assertEquals("rtsp://192.168.1.24:554/h264/ch1/sub/av_stream", StreamUrls.safe("192.168.1.24", 554, "EZVIZ", 1, null))
        assertEquals("rtsp://192.168.1.25:554/cam/realmonitor?channel=1&subtype=1", StreamUrls.safe("192.168.1.25", 554, "IMOU", 1, null))
        val icsee = StreamUrls.url("192.168.1.26", 554, "ICSEE", 1, null, "admin", "pa ss")
        assertEquals("rtsp://admin:pa%20ss@192.168.1.26:554/user=admin&password=pa%20ss&channel=1&stream=1.sdp?real_stream", icsee)
        assertTrue("the shown form never carries the password", "pa%20ss" !in StreamUrls.safe("192.168.1.26", 554, "ICSEE", 1, null))
        assertEquals(StreamUrls.BRANDS.size, StreamUrls.BRAND_LIST.map { it.label }.toSet().size)
    }

    // ── shelf → exit (daytime theft) ─────────────────────────────────
    private val shelf = Zone(1, 1, "Drinks shelf", ZoneType.SHELF, Zone.rect(Pt(0.1f, 0.1f), Pt(0.4f, 0.5f))!!)
    private val exit = Zone(2, 1, "Front door", ZoneType.EXIT, Zone.rect(Pt(0.7f, 0.5f), Pt(1f, 1f))!!)
    private val person = BBox(0.3f, 0.1f, 0.5f, 0.9f)

    /** 320×180 textured shop wall; [product]: grey value of the item on the shelf (null = gone); [hand]: a hand in the shelf. */
    private fun frame(ts: Double, product: Int?, hand: Boolean, cameraShift: Int = 0): Frame {
        val w = 320; val h = 180
        val px = IntArray(w * h) { i ->
            val x = i % w; val y = i / w
            val sx = x + cameraShift
            var v = 100 + (sx * 7 + y * 13) % 40 + if ((sx / 16 + y / 16) % 2 == 0) 40 else 0
            if (product != null && x in 60..89 && y in 40..69) v = product
            if (hand && x in 85..109 && y in 40..59) v = 250
            (0xFF shl 24) or (v shl 16) or (v shl 8) or v
        }
        return Frame(w, h, px, ts)
    }

    /** Empty shelf 1 s, a person reaching in 1.4 s, then gone for 2 s with the shelf as [after]. → all shelf events. */
    private fun visit(engine: ShelfEngine, after: Int?, cameraShiftAfter: Int = 0): List<ShelfEvent> {
        val events = ArrayList<ShelfEvent>()
        var t = 0.0
        repeat(6) { events += engine.update(Grey.of(frame(t, 30, false)), emptyMap(), t); t += 0.2 }
        repeat(7) { i -> events += engine.update(Grey.of(frame(t, 30, i % 2 == 0)), mapOf(1L to person), t); t += 0.2 }
        repeat(10) { events += engine.update(Grey.of(frame(t, after, false, cameraShiftAfter)), emptyMap(), t); t += 0.2 }
        return events
    }

    @Test fun cameraKnockedDuringTheVisitNeverAlerts() {
        val engine = ShelfEngine().apply { setZones(listOf(shelf)) }
        val events = visit(engine, after = 30, cameraShiftAfter = 10)
        assertEquals(false, events.last().interaction.verifiable)
        assertEquals("RESOLVED", events.last().kind)
    }

    @Test fun productTakenThenExitIsAHighConfidenceUnpaidExit() {
        val engine = ShelfEngine().apply { setZones(listOf(shelf)) }
        val events = visit(engine, after = null)
        assertEquals(listOf("SHELF_INTERACTION", "UNRESOLVED_SHELF_INTERACTION"), events.map { it.kind })
        val it = events.last().interaction
        assertEquals("STRONG", it.strength)
        assertEquals("TAKEN", it.verdict)

        val theft = TheftCorrelator(1)
        assertTrue(theft.process(5.0, events, emptyList(), emptyList()).isEmpty())
        val out = theft.process(6.0, emptyList(), listOf(TheftCorrelator.Entry(1, exit)), emptyList())
        assertEquals(1, out.size)
        assertEquals("POSSIBLE_UNPAID_EXIT", out[0].kind)
        assertEquals("HIGH", out[0].severity); assertEquals("HIGH", out[0].confidence)
        assertTrue(out[0].detail!!.contains("not proof of theft"))
        assertTrue("one alert per person", theft.process(7.0, emptyList(), listOf(TheftCorrelator.Entry(1, exit)), emptyList()).isEmpty())
    }

    @Test fun productPutBackIsQuiet() {
        val engine = ShelfEngine().apply { setZones(listOf(shelf)) }
        val events = visit(engine, after = 30)
        assertEquals(listOf("SHELF_INTERACTION", "RESOLVED"), events.map { it.kind })
        val theft = TheftCorrelator(1)
        theft.process(5.0, events, emptyList(), emptyList())
        assertTrue(theft.process(6.0, emptyList(), listOf(TheftCorrelator.Entry(1, exit)), emptyList()).isEmpty())
    }

    @Test fun somethingElseLeftIsAProductSwap() {
        val engine = ShelfEngine().apply { setZones(listOf(shelf)) }
        val events = visit(engine, after = 220)
        val ia = events.last().interaction
        assertEquals("${events.map { it.kind }} frac=${ia.changeFrac} verifiable=${ia.verifiable} strength=${ia.strength}", "REPLACED", ia.verdict)
        val out = TheftCorrelator(1).process(5.0, events, emptyList(), emptyList())
        assertEquals("POSSIBLE_PRODUCT_REPLACEMENT", out.single().kind)
    }

    @Test fun confidenceTable() {
        assertEquals("HIGH", TheftCorrelator.confidenceFor("STRONG", false))
        assertEquals("MEDIUM", TheftCorrelator.confidenceFor("STRONG", true))
        assertEquals("MEDIUM", TheftCorrelator.confidenceFor("WEAK", false))
        assertEquals("LOW", TheftCorrelator.confidenceFor("WEAK", true))
        assertEquals("LOW", TheftCorrelator.confidenceFor("UNVERIFIED", false))
        assertEquals("POSSIBLE_UNPAID_EXIT", IncidentRules.RULES["POSSIBLE_UNPAID_EXIT"]!!.type)
        assertEquals(0, IncidentRules.ALARMS["POSSIBLE_UNPAID_EXIT"]!!.cooldownS)
        assertEquals(30, IncidentRules.priority("POSSIBLE_UNPAID_EXIT"))
    }

    // ── camera speaker (ONVIF back-channel) ──────────────────────────
    @Test fun g711() {
        assertEquals(0xFF.toByte(), G711.ulaw(0)); assertEquals(0x80.toByte(), G711.ulaw(32767)); assertEquals(0x00.toByte(), G711.ulaw(-32768))
        assertEquals(0xD5.toByte(), G711.alaw(0)); assertEquals(0xAA.toByte(), G711.alaw(32767)); assertEquals(0x2A.toByte(), G711.alaw(-32768))
    }

    @Test fun backchannelTrackFromSdp() {
        val sdp = """v=0
            |o=- 1 1 IN IP4 192.168.1.64
            |s=Media Presentation
            |m=video 0 RTP/AVP 96
            |a=control:rtsp://192.168.1.64:554/Streaming/Channels/102/trackID=1
            |a=rtpmap:96 H264/90000
            |m=audio 0 RTP/AVP 0
            |a=control:rtsp://192.168.1.64:554/Streaming/Channels/102/trackID=2
            |a=recvonly
            |m=audio 0 RTP/AVP 8
            |a=rtpmap:8 PCMA/8000
            |a=control:trackID=4
            |a=sendonly""".trimMargin().replace("\n", "\r\n")
        val t = Sdp.backchannel(sdp)!!
        assertEquals("trackID=4", t.control); assertEquals(8, t.payloadType); assertTrue(t.alaw)
        assertEquals("rtsp://192.168.1.64:554/Streaming/Channels/102/trackID=4", Sdp.resolve("rtsp://192.168.1.64:554/Streaming/Channels/102/", t.control))
        assertNull("no sendonly audio = no speaker", Sdp.backchannel(sdp.substringBefore("m=audio 0 RTP/AVP 8")))
    }

    @Test fun digestLoginMatchesRfc2617() {
        val h = Auth.digest("Mufasa", "Circle Of Life", "testrealm@host.com", "dcd98b7102dd2f0e8b11d0f600bfb0c093",
            "GET", "/dir/index.html", "auth", 1, cnonce = "0a4f113b")
        assertTrue(h, h.contains("response=\"6629fae49393a05397450978507c4ef1\""))
        assertEquals("abc", Auth.params("Digest realm=\"abc\", nonce=\"n\"")["realm"])
    }

    @Test fun rtpPacketOverRtsp() {
        val f = Rtp.interleaved(2, Rtp.packet(0, 7, 320, 1, ByteArray(160)))
        assertEquals('$'.code.toByte(), f[0]); assertEquals(2.toByte(), f[1]); assertEquals(172, (f[2].toInt() shl 8) or (f[3].toInt() and 0xFF))
        assertEquals(0x80.toByte(), f[4]); assertEquals(7.toByte(), f[7])
    }
}
