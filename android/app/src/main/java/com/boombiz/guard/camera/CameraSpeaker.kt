package com.boombiz.guard.camera

import java.io.BufferedInputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URI
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin

/**
 * Plays the siren through a camera's own speaker, using the ONVIF audio
 * back-channel: RTSP DESCRIBE with `Require: www.onvif.org/ver20/backchannel`,
 * SETUP the `sendonly` audio track over the same TCP connection, PLAY, then
 * send G.711 (μ-law or A-law, 8 kHz) RTP packets every 20 ms.
 *
 * Hikvision, Dahua, EZVIZ, Imou and many ONVIF cameras with 2-way audio accept
 * it. Many cheap app cameras (V380, some CamHi/Yoosee) only take sound from
 * their own app: then DESCRIBE has no back-channel track and [play] says so in
 * plain words. Nothing here has been checked on real hardware yet — the
 * camera screen's "Test the camera's speaker" is the proof.
 *
 * LAN only (the caller passes a camera that passed StreamUrls.isLan), and the
 * password never leaves this class except inside the RTSP auth header.
 */
class CameraSpeaker(
    private val host: String,
    private val port: Int,
    private val path: String,
    private val user: String,
    private val password: String?,
) {
    class Unsupported(msg: String) : IOException(msg)

    private val stopped = AtomicBoolean(false)

    fun stop() = stopped.set(true)

    /** Blocks for [seconds] (or until [stop]). Throws [Unsupported] / IOException with a plain-language message. */
    fun play(seconds: Int) {
        if (!StreamUrls.isLan(host)) throw Unsupported("Guard only talks to cameras on the shop's own network.")
        Socket().use { s ->
            try { s.connect(InetSocketAddress(host, port), 4000) } catch (e: IOException) {
                throw IOException("Couldn't reach the camera at $host:$port. Check it's on the shop Wi-Fi.")
            }
            s.soTimeout = 5000
            val rtsp = Rtsp(BufferedInputStream(s.getInputStream()), s.getOutputStream(), user, password)
            val base = "rtsp://$host:$port$path"

            val describe = rtsp.request("DESCRIBE", base, mapOf("Accept" to "application/sdp", "Require" to BACKCHANNEL))
            if (describe.code == 551) throw Unsupported(NO_SPEAKER)
            describe.check()
            val track = Sdp.backchannel(describe.body) ?: throw Unsupported(NO_SPEAKER)
            val contentBase = describe.headers["content-base"] ?: base
            val control = Sdp.resolve(contentBase, track.control)

            val setup = rtsp.request("SETUP", control, mapOf("Transport" to "RTP/AVP/TCP;unicast;interleaved=0-1", "Require" to BACKCHANNEL))
            if (setup.code == 551 || setup.code == 461) throw Unsupported(NO_SPEAKER)
            setup.check()
            val session = setup.headers["session"]?.substringBefore(";")?.trim() ?: throw IOException("The camera didn't start an audio session.")
            val channel = Regex("interleaved=(\\d+)").find(setup.headers["transport"] ?: "")?.groupValues?.get(1)?.toInt() ?: 0

            rtsp.request("PLAY", base, mapOf("Session" to session, "Range" to "npt=0.000-", "Require" to BACKCHANNEL)).check()

            val out = s.getOutputStream()
            val ssrc = (System.nanoTime() and 0x7FFFFFFF).toInt()
            var seq = 0; var ts = 0L
            val start = System.nanoTime()
            val packets = seconds * 50
            for (n in 0 until packets) {
                if (stopped.get()) break
                val pcm = SirenTone.samples(n * 160, 160)
                val payload = if (track.alaw) G711.alaw(pcm) else G711.ulaw(pcm)
                out.write(Rtp.interleaved(channel, Rtp.packet(track.payloadType, seq, ts, ssrc, payload)))
                seq = (seq + 1) and 0xFFFF; ts += 160
                val due = start + (n + 1) * 20_000_000L
                val wait = (due - System.nanoTime()) / 1_000_000L
                if (wait > 0) Thread.sleep(wait)
            }
            out.flush()
            runCatching { rtsp.request("TEARDOWN", base, mapOf("Session" to session)) }
        }
    }

    companion object {
        const val BACKCHANNEL = "www.onvif.org/ver20/backchannel"
        const val NO_SPEAKER = "This camera doesn't let other apps play sound through its speaker. The siren will sound on the Guard phone only."
    }
}

/** Minimal RTSP over one TCP connection, with Basic and Digest login. */
internal class Rtsp(private val input: InputStream, private val output: OutputStream,
                    private val user: String, private val password: String?) {
    class Response(val code: Int, val headers: Map<String, String>, val body: String) {
        fun check() {
            when {
                code == 401 -> throw IOException("The camera refused the username or password.")
                code !in 200..299 -> throw IOException("The camera answered with an error ($code).")
            }
        }
    }

    private var cseq = 0
    private var auth: ((String, String) -> String)? = null

    fun request(method: String, url: String, headers: Map<String, String>): Response {
        var r = send(method, url, headers)
        if (r.code == 401 && user.isNotEmpty()) {
            val challenge = r.headers["www-authenticate"] ?: return r
            auth = Auth.from(challenge, user, password ?: "") ?: return r
            r = send(method, url, headers)
        }
        return r
    }

    private fun send(method: String, url: String, headers: Map<String, String>): Response {
        val sb = StringBuilder("$method $url RTSP/1.0\r\nCSeq: ${++cseq}\r\nUser-Agent: BoombizGuard\r\n")
        auth?.let { sb.append("Authorization: ").append(it(method, url)).append("\r\n") }
        headers.forEach { (k, v) -> sb.append(k).append(": ").append(v).append("\r\n") }
        sb.append("\r\n")
        output.write(sb.toString().toByteArray(Charsets.ISO_8859_1)); output.flush()
        return read()
    }

    private fun byte(): Int = input.read().also { if (it < 0) throw IOException("The camera closed the connection.") }

    private fun line(first: Int? = null): String {
        val b = StringBuilder()
        var c = first ?: byte()
        while (c != '\n'.code) { b.append(c.toChar()); c = byte() }
        return b.toString().trimEnd('\r')
    }

    private fun read(): Response {
        var status: String
        while (true) {
            val c = byte()
            if (c == '$'.code) { // an interleaved RTCP frame from the camera: skip it
                byte(); val len = (byte() shl 8) or byte()
                repeat(len) { byte() }
                continue
            }
            status = line(c)
            if (status.isNotEmpty()) break
        }
        val code = status.split(" ").getOrNull(1)?.toIntOrNull() ?: throw IOException("The camera doesn't speak RTSP on this port.")
        val headers = HashMap<String, String>()
        while (true) {
            val l = line()
            if (l.isEmpty()) break
            val i = l.indexOf(':')
            if (i > 0) {
                val k = l.substring(0, i).trim().lowercase()
                // Keep the first Digest challenge when a camera offers both.
                if (k == "www-authenticate" && headers[k]?.startsWith("Digest", true) == true) continue
                headers[k] = l.substring(i + 1).trim()
            }
        }
        val len = headers["content-length"]?.toIntOrNull() ?: 0
        val body = ByteArray(len)
        var got = 0
        while (got < len) { val n = input.read(body, got, len - got); if (n < 0) break; got += n }
        return Response(code, headers, String(body, 0, got, Charsets.UTF_8))
    }
}

internal object Auth {
    /** → a function (method, uri) → Authorization header value, or null for an unknown scheme. */
    fun from(challenge: String, user: String, password: String): ((String, String) -> String)? {
        if (challenge.startsWith("Basic", true)) {
            val token = java.util.Base64.getEncoder().encodeToString("$user:$password".toByteArray())
            return { _, _ -> "Basic $token" }
        }
        if (!challenge.startsWith("Digest", true)) return null
        val p = params(challenge.substringAfter(' '))
        val realm = p["realm"] ?: ""; val nonce = p["nonce"] ?: return null
        val qop = p["qop"]?.split(",")?.map { it.trim() }?.firstOrNull { it == "auth" }
        var nc = 0
        return { method, uri -> digest(user, password, realm, nonce, method, uri, qop, ++nc, p["opaque"]) }
    }

    fun digest(user: String, password: String, realm: String, nonce: String, method: String, uri: String,
               qop: String?, nc: Int = 1, opaque: String? = null, cnonce: String = "0a4f113b"): String {
        val ha1 = md5("$user:$realm:$password")
        val ha2 = md5("$method:$uri")
        val ncs = "%08x".format(nc)
        val response = if (qop != null) md5("$ha1:$nonce:$ncs:$cnonce:$qop:$ha2") else md5("$ha1:$nonce:$ha2")
        val sb = StringBuilder("Digest username=\"$user\", realm=\"$realm\", nonce=\"$nonce\", uri=\"$uri\", response=\"$response\"")
        if (qop != null) sb.append(", qop=$qop, nc=$ncs, cnonce=\"$cnonce\"")
        if (opaque != null) sb.append(", opaque=\"$opaque\"")
        return sb.toString()
    }

    fun params(s: String): Map<String, String> =
        Regex("(\\w+)=(\"([^\"]*)\"|[^,\\s]+)").findAll(s).associate { m ->
            m.groupValues[1].lowercase() to (m.groups[3]?.value ?: m.groupValues[2])
        }

    fun md5(s: String) = MessageDigest.getInstance("MD5").digest(s.toByteArray()).joinToString("") { "%02x".format(it) }
}

internal object Sdp {
    data class Track(val control: String, val payloadType: Int, val alaw: Boolean)

    /** The back-channel = an audio media section marked a=sendonly, carrying PCMU or PCMA. */
    fun backchannel(sdp: String): Track? {
        val sections = sdp.split(Regex("\r?\n(?=m=)"))
        for (sec in sections) {
            val lines = sec.lines().map { it.trim() }
            val m = lines.firstOrNull { it.startsWith("m=audio") } ?: continue
            if (lines.none { it == "a=sendonly" }) continue
            val control = lines.firstOrNull { it.startsWith("a=control:") }?.removePrefix("a=control:") ?: continue
            val pts = m.split(" ").drop(3).mapNotNull { it.toIntOrNull() }
            val maps = lines.filter { it.startsWith("a=rtpmap:") }.associate {
                val (pt, enc) = it.removePrefix("a=rtpmap:").split(" ", limit = 2)
                pt.toInt() to enc.uppercase()
            }
            for (pt in pts) {
                val enc = maps[pt] ?: when (pt) { 0 -> "PCMU/8000"; 8 -> "PCMA/8000"; else -> "" }
                if (enc.startsWith("PCMU/8000")) return Track(control, pt, alaw = false)
                if (enc.startsWith("PCMA/8000")) return Track(control, pt, alaw = true)
            }
        }
        return null
    }

    fun resolve(base: String, control: String): String = when {
        control.startsWith("rtsp://", true) -> control
        control == "*" -> base
        else -> base.trimEnd('/') + "/" + control.trimStart('/')
    }.let { runCatching { URI(it).toString() }.getOrDefault(it) }
}

internal object Rtp {
    fun packet(pt: Int, seq: Int, ts: Long, ssrc: Int, payload: ByteArray): ByteArray {
        val p = ByteArray(12 + payload.size)
        p[0] = 0x80.toByte(); p[1] = (pt and 0x7F).toByte()
        p[2] = (seq shr 8).toByte(); p[3] = seq.toByte()
        p[4] = (ts shr 24).toByte(); p[5] = (ts shr 16).toByte(); p[6] = (ts shr 8).toByte(); p[7] = ts.toByte()
        p[8] = (ssrc shr 24).toByte(); p[9] = (ssrc shr 16).toByte(); p[10] = (ssrc shr 8).toByte(); p[11] = ssrc.toByte()
        System.arraycopy(payload, 0, p, 12, payload.size)
        return p
    }

    /** RTSP interleaved frame: '$', channel, 16-bit length, data. */
    fun interleaved(channel: Int, data: ByteArray): ByteArray =
        byteArrayOf('$'.code.toByte(), channel.toByte(), (data.size shr 8).toByte(), data.size.toByte()) + data
}

internal object G711 {
    fun ulaw(pcm: ShortArray): ByteArray = ByteArray(pcm.size) { ulaw(pcm[it].toInt()) }

    fun ulaw(sample: Int): Byte {
        val bias = 0x84; val clip = 32635
        val sign = if (sample < 0) 0x80 else 0
        val s = minOf(abs(sample), clip) + bias
        var exp = 7
        var mask = 0x4000
        while (exp > 0 && (s and mask) == 0) { exp--; mask = mask shr 1 }
        val mant = (s shr (exp + 3)) and 0x0F
        return (sign or (exp shl 4) or mant).inv().toByte()
    }

    fun alaw(pcm: ShortArray): ByteArray = ByteArray(pcm.size) { alaw(pcm[it].toInt()) }

    fun alaw(sample: Int): Byte {
        val sign = if (sample >= 0) 0x80 else 0
        val s = minOf(abs(sample), 32767) shr 3 // 13-bit
        val v = if (s < 32) s shr 1 else {
            var exp = 1; var t = s shr 5
            while (t > 1 && exp < 7) { t = t shr 1; exp++ }
            (exp shl 4) or ((s shr exp) and 0x0F)
        }
        return ((sign or v) xor 0x55).toByte()
    }
}

/** A loud two-tone wail, 8 kHz 16-bit: 700 ↔ 1300 Hz, switching every 400 ms. */
internal object SirenTone {
    fun samples(offset: Int, count: Int): ShortArray = ShortArray(count) { i ->
        val n = offset + i
        val hz = if ((n / 3200) % 2 == 0) 700.0 else 1300.0
        (sin(2 * PI * hz * n / 8000.0) * 26000).toInt().toShort()
    }
}
