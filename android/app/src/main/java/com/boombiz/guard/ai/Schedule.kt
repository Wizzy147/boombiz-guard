package com.boombiz.guard.ai

import java.time.DayOfWeek
import java.time.LocalDateTime
import java.time.LocalTime

/**
 * Business hours, same rules as the PC agent's zones/schedule.py:
 * no hours set → always open (after-hours alerts stay silent until someone
 * sets hours); a day marked closed → closed all day; past-midnight hours
 * (18:00–02:00) wrap.
 */
data class DayHours(val day: DayOfWeek, val opens: String?, val closes: String?, val closed: Boolean = false)

object Schedule {
    fun parse(s: String): LocalTime {
        val (h, m) = s.trim().split(":")
        return LocalTime.of(h.toInt(), m.toInt())
    }

    fun valid(s: String?): Boolean = s != null && runCatching { parse(s) }.isSuccess

    fun isOpen(days: List<DayHours>, now: LocalDateTime): Boolean {
        if (days.isEmpty()) return true
        val today = days.firstOrNull { it.day == now.dayOfWeek } ?: return true
        if (today.closed || today.opens == null || today.closes == null) return false
        val o = parse(today.opens); val c = parse(today.closes); val t = now.toLocalTime()
        return if (!o.isAfter(c)) !t.isBefore(o) && t.isBefore(c) else !t.isBefore(o) || t.isBefore(c)
    }
}
