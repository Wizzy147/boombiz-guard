package com.boombiz.guard.camera

import android.content.Context
import android.graphics.ImageFormat
import android.media.Image
import android.media.ImageReader
import android.net.Uri
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.rtsp.RtspMediaSource
import com.boombiz.guard.ai.Frame

/**
 * One camera's RTSP sub-stream → decoded frames, via Media3 ExoPlayer and the
 * phone's HARDWARE decoder (MediaCodec) into an ImageReader. Nothing is shown
 * on screen and nothing is recorded; frames are sampled at [fps] and handed
 * over as ARGB. RTP over TCP: shop Wi-Fi drops UDP packets and smears frames.
 *
 * Reconnects with backoff 2/5/10/30 s (as the PC's stream worker). A wrong
 * password stops retrying — hammering a recorder with a rejected login can
 * lock the account on Hikvision/Dahua.
 */
@UnstableApi
class RtspFrameSource(
    private val ctx: Context,
    private val url: String,
    private val fps: Double,
    private val onFrame: (Frame) -> Unit,
    private val onState: (State, String?) -> Unit,
) {
    enum class State { CONNECTING, ONLINE, OFFLINE, AUTH_ERROR }

    private val thread = HandlerThread("rtsp").apply { start() }
    private val handler = Handler(thread.looper)
    private var player: ExoPlayer? = null
    private var reader: ImageReader? = null
    private var lastEmit = 0L
    private var attempts = 0
    @Volatile private var stopped = false
    @Volatile var lastFrameAt = 0L
        private set

    fun start() = handler.post { open() }

    fun stop() {
        stopped = true
        handler.post { release(); thread.quitSafely() }
    }

    private fun open() {
        if (stopped) return
        onState(State.CONNECTING, null)
        val r = ImageReader.newInstance(WIDTH, HEIGHT, ImageFormat.YUV_420_888, 3)
        r.setOnImageAvailableListener({ ir ->
            val img = ir.acquireLatestImage() ?: return@setOnImageAvailableListener
            try {
                lastFrameAt = SystemClock.elapsedRealtime()
                if (attempts != 0) { attempts = 0; onState(State.ONLINE, null) }
                val now = SystemClock.elapsedRealtime()
                if (now - lastEmit >= (1000 / fps).toLong()) {
                    lastEmit = now
                    onFrame(Frame(img.width, img.height, yuvToArgb(img), now / 1000.0))
                }
            } finally { img.close() }
        }, handler)
        reader = r

        val source = RtspMediaSource.Factory().setForceUseRtpTcp(true).setTimeoutMs(10_000)
            .createMediaSource(MediaItem.fromUri(Uri.parse(url)))
        player = ExoPlayer.Builder(ctx).setLooper(thread.looper).build().apply {
            setVideoSurface(r.surface)
            volume = 0f
            addListener(object : Player.Listener {
                override fun onPlayerError(error: PlaybackException) {
                    val msg = error.cause?.message ?: error.message ?: ""
                    val auth = "401" in msg || "nauthori" in msg
                    release()
                    if (auth) { onState(State.AUTH_ERROR, "The recorder refused the username or password."); return }
                    onState(State.OFFLINE, "No video from the recorder.")
                    val delay = BACKOFF[minOf(attempts, BACKOFF.size - 1)]
                    attempts++
                    handler.postDelayed({ open() }, delay)
                }
                override fun onPlaybackStateChanged(state: Int) {
                    if (state == Player.STATE_ENDED) onPlayerError(PlaybackException("ended", null, PlaybackException.ERROR_CODE_UNSPECIFIED))
                }
            })
            setMediaSource(source)
            prepare()
            playWhenReady = true
        }
        attempts = maxOf(attempts, 1) // counts as "not yet online" until the first frame
    }

    /** No frames for [seconds] although the player thinks it's playing: tear down and reconnect. */
    fun kickIfStalled(seconds: Int) = handler.post {
        if (!stopped && player != null && lastFrameAt > 0 && SystemClock.elapsedRealtime() - lastFrameAt > seconds * 1000L) {
            release(); onState(State.OFFLINE, "No video received for ${seconds}s."); attempts = 1
            handler.postDelayed({ open() }, 2000)
        }
    }

    private fun release() {
        player?.release(); player = null
        reader?.close(); reader = null
    }

    companion object {
        // The decoder scales into this surface; sub-streams are usually 640×360/352×288/704×576.
        const val WIDTH = 640
        const val HEIGHT = 360
        private val BACKOFF = longArrayOf(2_000, 5_000, 10_000, 30_000)

        fun yuvToArgb(img: Image): IntArray {
            val w = img.width; val h = img.height
            val yP = img.planes[0]; val uP = img.planes[1]; val vP = img.planes[2]
            val yB = yP.buffer; val uB = uP.buffer; val vB = vP.buffer
            val yRow = yP.rowStride; val uvRow = uP.rowStride; val uvPix = uP.pixelStride
            val out = IntArray(w * h)
            for (y in 0 until h) {
                val yo = y * yRow; val uvo = (y shr 1) * uvRow
                for (x in 0 until w) {
                    val yy = (yB.get(yo + x).toInt() and 0xFF) - 16
                    val ui = uvo + (x shr 1) * uvPix
                    val u = (uB.get(ui).toInt() and 0xFF) - 128
                    val v = (vB.get(ui).toInt() and 0xFF) - 128
                    val c = 1192 * maxOf(0, yy)
                    val r = ((c + 1634 * v) shr 10).coerceIn(0, 255)
                    val g = ((c - 833 * v - 400 * u) shr 10).coerceIn(0, 255)
                    val b = ((c + 2066 * u) shr 10).coerceIn(0, 255)
                    out[y * w + x] = (0xFF shl 24) or (r shl 16) or (g shl 8) or b
                }
            }
            return out
        }
    }
}
