package com.boombiz.guard.ui

import android.app.Activity
import android.app.AlertDialog
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.widget.EditText
import android.widget.LinearLayout
import com.boombiz.guard.GuardApp
import com.boombiz.guard.data.PinHash
import kotlin.concurrent.thread

/**
 * Settings PIN. The status screen stays open to anyone in the shop; cameras,
 * areas, hours, incidents, the alarm test and disconnecting need the PIN.
 * Unlocked for 5 minutes after a correct PIN, and locked again whenever the
 * app goes to the background.
 *
 * Forgot the PIN: a fresh activation code from the Guard console clears it —
 * only someone who can sign in to Boombiz for this shop can get one.
 */
object PinLock {
    private const val UNLOCK_MS = 5 * 60_000L
    @Volatile private var unlockedUntil = 0L

    fun isSet(a: Activity) = (a.application as GuardApp).store.get("pin_hash") != null

    fun lockNow() { unlockedUntil = 0L }

    /** Runs [onOk] now if no PIN is set or it was entered recently; otherwise asks for it first. */
    fun guard(a: Activity, onOk: () -> Unit) {
        val store = (a.application as GuardApp).store
        val hash = store.get("pin_hash")
        if (hash == null || System.currentTimeMillis() < unlockedUntil) return onOk()
        val lockedUntil = store.get("pin_locked_until")?.toLongOrNull() ?: 0L
        if (System.currentTimeMillis() < lockedUntil) {
            val min = ((lockedUntil - System.currentTimeMillis()) / 60_000) + 1
            AlertDialog.Builder(a).setMessage("Too many wrong PINs. Try again in $min minute${if (min == 1L) "" else "s"}.")
                .setPositiveButton("OK", null).show()
            return
        }
        val input = pinField(a, "PIN")
        AlertDialog.Builder(a).setTitle("Enter the Guard PIN").setView(wrap(a, input))
            .setPositiveButton("Unlock") { _, _ ->
                if (PinHash.matches(input.text.toString(), store.get("pin_salt") ?: "", hash)) {
                    store.put("pin_fails", null)
                    unlockedUntil = System.currentTimeMillis() + UNLOCK_MS
                    onOk()
                } else {
                    val fails = (store.get("pin_fails")?.toIntOrNull() ?: 0) + 1
                    if (fails >= PinHash.MAX_TRIES) {
                        store.put("pin_fails", null)
                        store.put("pin_locked_until", (System.currentTimeMillis() + PinHash.LOCKOUT_MS).toString())
                        AlertDialog.Builder(a).setMessage("Wrong PIN. Settings are locked for 5 minutes.").setPositiveButton("OK", null).show()
                    } else {
                        store.put("pin_fails", fails.toString())
                        AlertDialog.Builder(a).setMessage("Wrong PIN. ${PinHash.MAX_TRIES - fails} tries left.").setPositiveButton("OK", null).show()
                    }
                }
            }
            .setNeutralButton("Forgot PIN") { _, _ -> forgot(a) }
            .setNegativeButton("Cancel", null).show()
    }

    /** Set or change the PIN (changing needs the current one — the caller guards it). */
    fun setPin(a: Activity, onDone: () -> Unit) {
        val store = (a.application as GuardApp).store
        val first = pinField(a, "New PIN (4 to 8 digits)")
        val again = pinField(a, "Type it again")
        AlertDialog.Builder(a).setTitle("Guard PIN").setView(wrap(a, first, again))
            .setPositiveButton("Save") { _, _ ->
                val p = first.text.toString()
                val msg = when {
                    !PinHash.valid(p) -> "The PIN must be 4 to 8 digits."
                    p != again.text.toString() -> "The two PINs didn't match."
                    else -> null
                }
                if (msg != null) { AlertDialog.Builder(a).setMessage(msg).setPositiveButton("OK", null).show(); return@setPositiveButton }
                val salt = PinHash.newSalt()
                store.put("pin_salt", salt)
                store.put("pin_hash", PinHash.hash(p, salt))
                store.put("pin_fails", null); store.put("pin_locked_until", null)
                unlockedUntil = System.currentTimeMillis() + UNLOCK_MS
                onDone()
            }
            .setNegativeButton("Cancel", null).show()
    }

    private fun forgot(a: Activity) {
        val app = a.application as GuardApp
        if (!app.cloud.isActivated) {
            AlertDialog.Builder(a).setMessage("This device isn't connected to Boombiz, so the PIN can't be reset here. Ask your Boombiz installer.")
                .setPositiveButton("OK", null).show()
            return
        }
        val code = EditText(a).apply { hint = "GARD-XXXX-XXXX"; setTextColor(C.INK); setSingleLine() }
        AlertDialog.Builder(a).setTitle("Reset the PIN")
            .setMessage("In Boombiz Guard, open Locations and create an activation code for this branch, then type it here.")
            .setView(wrap(a, code))
            .setPositiveButton("Reset") { _, _ ->
                val ui = Handler(Looper.getMainLooper())
                thread {
                    val r = runCatching {
                        app.cloud.activate(code.text.toString(), app.store.get("device_name") ?: "Guard phone", GuardApp.VERSION,
                            "Android ${Build.VERSION.RELEASE} (${Build.MANUFACTURER} ${Build.MODEL})")
                    }
                    ui.post {
                        r.onSuccess {
                            listOf("pin_hash", "pin_salt", "pin_fails", "pin_locked_until").forEach { app.store.put(it, null) }
                            setPin(a) {}
                        }.onFailure { e ->
                            AlertDialog.Builder(a).setMessage(e.message ?: "That code didn't work.").setPositiveButton("OK", null).show()
                        }
                    }
                }
            }
            .setNegativeButton("Cancel", null).show()
    }

    private fun pinField(a: Activity, hint: String) = EditText(a).apply {
        this.hint = hint; setTextColor(C.INK); setSingleLine()
        inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
    }

    private fun wrap(a: Activity, vararg fields: EditText) = LinearLayout(a).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(a.dp(20), a.dp(8), a.dp(20), 0)
        fields.forEach { addView(it) }
    }
}
