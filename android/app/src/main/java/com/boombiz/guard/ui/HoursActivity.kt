package com.boombiz.guard.ui

import android.app.Activity
import android.app.AlertDialog
import android.os.Bundle
import android.text.InputType
import android.widget.CheckBox
import android.widget.EditText
import androidx.media3.common.util.UnstableApi
import com.boombiz.guard.GuardApp
import com.boombiz.guard.ai.DayHours
import com.boombiz.guard.ai.Schedule
import com.boombiz.guard.watch.WatchService
import java.time.DayOfWeek
import java.time.format.TextStyle
import java.util.Locale

/** Opening hours per day. Outside them, anyone seen inside is an after-hours intrusion. */
@UnstableApi
class HoursActivity : Activity() {
    private val app get() = application as GuardApp
    private val rows = mutableListOf<Triple<DayOfWeek, Pair<EditText, EditText>, CheckBox>>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val p = page("Opening hours")
        p.text("Times look like 08:00 and 21:00. Past midnight works too (18:00 to 02:00).", color = C.MUTED)
        val saved = app.store.hours().associateBy { it.day }
        for (d in DayOfWeek.values()) {
            val h = saved[d]
            p.heading(d.getDisplayName(TextStyle.FULL, Locale.getDefault()))
            val open = EditText(this).apply { setText(h?.opens ?: "08:00"); inputType = InputType.TYPE_CLASS_DATETIME or InputType.TYPE_DATETIME_VARIATION_TIME; setTextColor(C.INK) }
            val close = EditText(this).apply { setText(h?.closes ?: "20:00"); inputType = open.inputType; setTextColor(C.INK) }
            p.row(open, close)
            val closed = CheckBox(this).apply { text = "Closed all day"; isChecked = h?.closed ?: false; setTextColor(C.INK) }
            p.addView(closed)
            rows += Triple(d, open to close, closed)
        }
        p.button("Copy Monday to every day", primary = false) {
            val (_, m, mc) = rows.first()
            rows.drop(1).forEach { (_, oc, c) -> oc.first.setText(m.first.text); oc.second.setText(m.second.text); c.isChecked = mc.isChecked }
        }
        p.button("Save") { save() }
    }

    private fun save() {
        val days = rows.map { (d, oc, c) -> DayHours(d, oc.first.text.toString().trim(), oc.second.text.toString().trim(), c.isChecked) }
        val bad = days.firstOrNull { !it.closed && (!Schedule.valid(it.opens) || !Schedule.valid(it.closes)) }
        if (bad != null) {
            AlertDialog.Builder(this).setMessage("Check ${bad.day.getDisplayName(TextStyle.FULL, Locale.getDefault())}: times must look like 08:00.")
                .setPositiveButton("OK", null).show()
            return
        }
        app.store.saveHours(days)
        WatchService.reload(this)
        finish()
    }
}
