package com.boombiz.guard.media

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import java.io.File
import java.io.IOException

/**
 * A capture's JPEG frames → one small H.264 MP4, with the phone's own hardware
 * encoder (the PC uses ffmpeg; Android has none). 640×360 at the rate the
 * frames actually arrived, ~200 kbps: a 15-second clip is a few hundred KB, so
 * it uploads on a slow shop line and stays far under the cloud's 30 MB limit.
 *
 * Frames are fed as NV12 byte buffers, not through a Surface, so no OpenGL is
 * needed and it behaves the same on a TV box as on a phone.
 */
object ClipEncoder {

    class EncodeError(msg: String, cause: Throwable? = null) : IOException(msg, cause)

    const val BITRATE = 200_000
    private const val TIMEOUT_US = 10_000L

    /** → the written file. Throws [EncodeError]; the caller keeps the snapshot either way. */
    fun encode(frames: List<ClipBuffer.Shot>, out: File): File {
        if (frames.size < 2) throw EncodeError("Too few pictures for a clip.")
        val first = ClipBuffer.bitmap(frames.first().jpeg) ?: throw EncodeError("The clip's pictures couldn't be read.")
        val w = first.width and 1.inv(); val h = first.height and 1.inv()
        val span = frames.last().ts - frames.first().ts
        val fps = ((frames.size - 1) / maxOf(0.2, span)).coerceIn(1.0, 30.0)

        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420SemiPlanar)
            setInteger(MediaFormat.KEY_BIT_RATE, BITRATE)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps.toInt().coerceAtLeast(1))
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
        }
        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        var muxer: MediaMuxer? = null
        var track = -1
        var started = false
        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            codec.start()
            muxer = MediaMuxer(out.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
            val info = MediaCodec.BufferInfo()
            val t0 = frames.first().ts
            var pixels: IntArray? = null

            for ((i, shot) in frames.withIndex()) {
                val bmp = (if (i == 0) first else ClipBuffer.bitmap(shot.jpeg)) ?: continue
                val buf = pixels?.takeIf { it.size >= bmp.width * bmp.height } ?: IntArray(bmp.width * bmp.height).also { pixels = it }
                bmp.getPixels(buf, 0, bmp.width, 0, 0, bmp.width, bmp.height)
                if (bmp !== first) bmp.recycle()
                val yuv = Yuv.nv12(buf, bmp.width, w, h)
                val index = codec.dequeueInputBuffer(TIMEOUT_US * 10)
                if (index >= 0) {
                    codec.getInputBuffer(index)!!.apply { clear(); put(yuv) }
                    codec.queueInputBuffer(index, 0, yuv.size, ((shot.ts - t0) * 1_000_000).toLong().coerceAtLeast(0), 0)
                }
                val r = drain(codec, muxer, info, track, started, false)
                track = r.first; started = r.second
            }
            val end = codec.dequeueInputBuffer(TIMEOUT_US * 10)
            if (end >= 0) codec.queueInputBuffer(end, 0, 0, ((frames.last().ts - t0) * 1_000_000).toLong() + 1, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
            val r = drain(codec, muxer, info, track, started, true)
            if (!r.second) throw EncodeError("The phone's video encoder produced nothing.")
        } catch (e: EncodeError) {
            throw e
        } catch (e: Exception) {
            throw EncodeError("This device couldn't make a video clip: ${e.message}", e)
        } finally {
            runCatching { codec.stop() }; runCatching { codec.release() }
            if (started) runCatching { muxer?.stop() }
            runCatching { muxer?.release() }
            first.recycle()
        }
        if (!out.exists() || out.length() == 0L) throw EncodeError("The clip file came out empty.")
        return out
    }

    /** → (track index, muxer started). */
    private fun drain(codec: MediaCodec, muxer: MediaMuxer, info: MediaCodec.BufferInfo,
                      trackIn: Int, startedIn: Boolean, toEnd: Boolean): Pair<Int, Boolean> {
        var track = trackIn; var started = startedIn
        while (true) {
            val out = codec.dequeueOutputBuffer(info, if (toEnd) TIMEOUT_US * 10 else TIMEOUT_US)
            when {
                out == MediaCodec.INFO_TRY_AGAIN_LATER -> if (!toEnd) return track to started
                out == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    track = muxer.addTrack(codec.outputFormat); muxer.start(); started = true
                }
                out >= 0 -> {
                    val buf = codec.getOutputBuffer(out)
                    if (buf != null && started && info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                        buf.position(info.offset); buf.limit(info.offset + info.size)
                        muxer.writeSampleData(track, buf, info)
                    }
                    codec.releaseOutputBuffer(out, false)
                    if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return track to started
                }
            }
        }
    }
}
