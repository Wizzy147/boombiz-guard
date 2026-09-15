package com.boombiz.guard.ui

import android.app.Activity
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * Tiny view builders. Screens are built in code: few of them, and plain
 * framework views are D-pad friendly out of the box, which an Android TV box
 * with only a remote needs. Black text on white; buttons yellow with black text.
 */
object C {
    const val INK = 0xFF0F172A.toInt()
    const val MUTED = 0xFF334155.toInt()
    const val LINE = 0xFFE2E8F0.toInt()
    const val YELLOW = 0xFFFACC15.toInt()
    const val RED = 0xFFDC2626.toInt()
    const val GREEN = 0xFF16A34A.toInt()
}

fun Activity.dp(v: Int) = (v * resources.displayMetrics.density).toInt()

/** A scrolling page with a title; returns the column to add to. */
fun Activity.page(title: String): LinearLayout {
    val col = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(20), dp(20), dp(20), dp(32))
    }
    col.addView(TextView(this).apply {
        text = title; textSize = 24f; setTypeface(typeface, Typeface.BOLD); setTextColor(C.INK)
        setPadding(0, 0, 0, dp(12))
    })
    setContentView(ScrollView(this).apply { setBackgroundColor(Color.WHITE); addView(col) })
    return col
}

fun LinearLayout.text(s: String, size: Float = 16f, bold: Boolean = false, color: Int = C.INK): TextView =
    TextView(context).apply {
        text = s; textSize = size; setTextColor(color)
        if (bold) setTypeface(typeface, Typeface.BOLD)
        setPadding(0, (context.resources.displayMetrics.density * 6).toInt(), 0, 0)
    }.also { addView(it) }

fun LinearLayout.heading(s: String) = text(s, 18f, bold = true).also { it.setPadding(0, it.paddingTop * 4, 0, 0) }

fun LinearLayout.button(label: String, primary: Boolean = true, onClick: () -> Unit): Button =
    Button(context).apply {
        text = label; isAllCaps = false; textSize = 16f
        setTextColor(if (primary) C.INK else C.INK)
        background = GradientDrawable().apply {
            cornerRadius = 12f * context.resources.displayMetrics.density
            setColor(if (primary) C.YELLOW else Color.WHITE)
            setStroke((context.resources.displayMetrics.density * 1.5f).toInt(), if (primary) C.YELLOW else C.INK)
        }
        backgroundTintList = null
        stateListAnimator = null
        setOnClickListener { onClick() }
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { topMargin = (context.resources.displayMetrics.density * 10).toInt() }
        // A visible focus ring for TV remotes.
        setOnFocusChangeListener { v, f -> v.alpha = if (f) 0.8f else 1f }
    }.also { addView(it) }

fun LinearLayout.field(label: String, value: String = "", type: Int = InputType.TYPE_CLASS_TEXT, hint: String? = null): EditText {
    text(label, 14f, bold = true, color = C.MUTED)
    return EditText(context).apply {
        setText(value); inputType = type; setTextColor(C.INK); textSize = 17f; setSingleLine()
        hint?.let { this.hint = it }
        backgroundTintList = ColorStateList.valueOf(C.INK)
    }.also { addView(it) }
}

fun LinearLayout.row(vararg views: View): LinearLayout = LinearLayout(context).apply {
    orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
    views.forEach { v -> addView(v, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)) }
}.also { addView(it) }

fun LinearLayout.divider() = View(context).apply { setBackgroundColor(C.LINE) }
    .also { addView(it, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 2).apply { topMargin = 24; bottomMargin = 8 }) }
