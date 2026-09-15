package com.boombiz.guard

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import com.boombiz.guard.cloud.CloudClient
import com.boombiz.guard.data.Store

class GuardApp : Application() {
    lateinit var store: Store
        private set
    lateinit var cloud: CloudClient
        private set

    override fun onCreate() {
        super.onCreate()
        store = Store(this)
        cloud = CloudClient(store)
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_WATCH, "Guard is watching", NotificationManager.IMPORTANCE_LOW)
            .apply { description = "Shown while Guard watches the cameras. Android needs it to keep Guard running." })
        nm.createNotificationChannel(NotificationChannel(CH_ALERT, "Guard alerts", NotificationManager.IMPORTANCE_HIGH)
            .apply { description = "Incidents seen by the cameras in this shop." })
    }

    companion object {
        const val CH_WATCH = "watch"
        const val CH_ALERT = "alerts"
        const val VERSION = "android-" + BuildConfig.VERSION_NAME
    }
}
