package com.boombiz.guard.watch

import android.app.Notification
import android.content.ComponentName
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.drawable.BitmapDrawable
import android.graphics.drawable.Icon
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import androidx.media3.common.util.UnstableApi
import com.boombiz.guard.GuardApp

/**
 * Reads the notifications of the camera apps the installer linked (see
 * [AppAlerts]) and hands alarms to the watcher. Every other app's
 * notifications are dropped on the spot: never read further, stored or sent.
 */
@UnstableApi
class AppAlertListener : NotificationListenerService() {

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        try {
            val app = application as GuardApp
            val source = AppAlerts.decode(app.store.get(AppAlerts.KEY)).firstOrNull { it.pkg == sbn.packageName } ?: return
            val n = sbn.notification
            // The app's own "running" banner and group headers aren't alarms.
            if (n.flags and (Notification.FLAG_ONGOING_EVENT or Notification.FLAG_GROUP_SUMMARY or Notification.FLAG_FOREGROUND_SERVICE) != 0) return
            val ex = n.extras
            val title = ex.getCharSequence(Notification.EXTRA_TITLE)?.toString()
            val text = (ex.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: ex.getCharSequence(Notification.EXTRA_TEXT))?.toString()
            if (AppAlerts.classify(title, text) != AppAlerts.Kind.ALARM) return
            WatchService.appAlert(this, WatchService.AppAlert(source, title, text, picture(ex)))
        } catch (t: Throwable) {
            Log.w(TAG, "camera-app alert failed", t)
        }
    }

    /** Vendor "phone managers" unbind listeners; ask Android to bind us again. */
    override fun onListenerDisconnected() {
        runCatching { requestRebind(ComponentName(this, AppAlertListener::class.java)) }
    }

    /** The picture some camera apps attach to the alert (big-picture style). Not the app's logo. */
    @Suppress("DEPRECATION")
    private fun picture(ex: Bundle): Bitmap? {
        (ex.get(Notification.EXTRA_PICTURE) as? Bitmap)?.let { return it }
        if (Build.VERSION.SDK_INT >= 31) {
            val icon = ex.get(Notification.EXTRA_PICTURE_ICON) as? Icon ?: return null
            val d = icon.loadDrawable(this) ?: return null
            if (d is BitmapDrawable) return d.bitmap
            if (d.intrinsicWidth <= 0 || d.intrinsicHeight <= 0) return null
            return Bitmap.createBitmap(d.intrinsicWidth, d.intrinsicHeight, Bitmap.Config.ARGB_8888).also {
                d.setBounds(0, 0, it.width, it.height); d.draw(Canvas(it))
            }
        }
        return null
    }

    companion object {
        private const val TAG = "GuardAppAlerts"

        /** Has the installer allowed Guard to read notifications (Settings → Notification access)? */
        fun granted(ctx: Context): Boolean {
            val flat = Settings.Secure.getString(ctx.contentResolver, "enabled_notification_listeners") ?: return false
            return flat.split(":").any { ComponentName.unflattenFromString(it)?.packageName == ctx.packageName }
        }
    }
}
