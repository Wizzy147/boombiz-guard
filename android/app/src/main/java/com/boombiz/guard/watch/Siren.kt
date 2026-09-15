package com.boombiz.guard.watch

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.RingtoneManager
import android.media.ToneGenerator
import android.os.Handler
import android.os.Looper

/**
 * The phone's own alarm — the watcher's local siren (the PC's "PC beep").
 * Plays on the ALARM stream at full volume so a phone left on silent still
 * sounds. An Android TV box plays through the TV's speakers only when the TV
 * is on — the setup screen says so.
 */
class Siren(private val ctx: Context) {
    private val main = Handler(Looper.getMainLooper())
    private var ringtone: android.media.Ringtone? = null
    private var tone: ToneGenerator? = null
    private val lastFired = HashMap<String, Long>()

    /** → true when it actually sounded (false = inside its cooldown). */
    fun sound(key: String, seconds: Int, cooldownS: Int): Boolean {
        val now = System.currentTimeMillis()
        if (cooldownS > 0 && now - (lastFired[key] ?: 0L) < cooldownS * 1000L) return false
        lastFired[key] = now
        main.post { start(seconds) }
        return true
    }

    fun test() = main.post { start(2) }

    fun stop() = main.post {
        ringtone?.stop(); ringtone = null
        tone?.release(); tone = null
    }

    private fun start(seconds: Int) {
        val am = ctx.getSystemService(AudioManager::class.java)
        runCatching { am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0) }
        stop()
        main.post {
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            val r = uri?.let { RingtoneManager.getRingtone(ctx, it) }
            if (r != null) {
                r.audioAttributes = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
                r.play(); ringtone = r
            } else {
                // Some TV boxes ship no alarm sound: a plain loud tone instead.
                tone = ToneGenerator(AudioManager.STREAM_ALARM, 100).also { it.startTone(ToneGenerator.TONE_CDMA_EMERGENCY_RINGBACK) }
            }
            main.postDelayed({ stop() }, seconds * 1000L)
        }
    }
}
