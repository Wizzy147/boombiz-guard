package com.boombiz.guard.watch

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.SystemClock

/** Power cut → phone restarts → Guard starts itself again, with nobody touching it. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        WatchService.startIfConfigured(ctx)
        Watchdog.schedule(ctx)
    }
}

/**
 * Tecno/Infinix/itel "phone managers" kill background apps even with a
 * foreground service. Every 15 min this alarm restarts the watcher if it was
 * killed (starting it is a no-op when it's already running).
 */
class WatchdogReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        WatchService.startIfConfigured(ctx)
        Watchdog.schedule(ctx)
    }
}

object Watchdog {
    private const val EVERY_MS = 15 * 60_000L

    fun schedule(ctx: Context) {
        val pi = PendingIntent.getBroadcast(ctx, 7, Intent(ctx, WatchdogReceiver::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        ctx.getSystemService(AlarmManager::class.java)
            .setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, SystemClock.elapsedRealtime() + EVERY_MS, pi)
    }
}
