package com.boombiz.guard.watch

import com.boombiz.guard.ai.Interaction
import com.boombiz.guard.ai.ShelfEvent
import com.boombiz.guard.ai.Zone

/**
 * Daytime theft signals — the PC agent's events/correlator.py for shelves, the
 * cashier and the exit. At most ONE "Possible unpaid exit" per person visit.
 *
 * POSSIBLE_UNPAID_EXIT when a person enters an EXIT zone while they have an
 * unresolved shelf interaction younger than 5 minutes. Confidence, rule-based:
 *     strong change + no cashier visit  → HIGH
 *     strong change + cashier visit     → MEDIUM   (may have paid)
 *     weak change   + no cashier visit  → MEDIUM
 *     weak change   + cashier visit     → LOW
 *     shelf blocked, never checked      → LOW
 * LOW confidence goes out as a LOW incident; MEDIUM and HIGH as HIGH.
 *
 * POSSIBLE_PRODUCT_REPLACEMENT at once when the shelf check says something
 * different was left where a product was.
 *
 * Never "theft": always "possible", with a snapshot to check. The PC's
 * concealment signal (hand to pocket, from a pose model) isn't on the phone,
 * so it never raises a confidence here.
 */
class TheftCorrelator(
    private val cameraId: Long,
    private val windowS: Double = 300.0,
    private val lateResolutionS: Double = 15.0,
) {
    private class Ctx {
        val unresolved = mutableListOf<Interaction>()
        val cashierVisits = mutableListOf<Double>()
        var exitAt: Double? = null
        var exitZone: Zone? = null
        var exitAlerted = false
    }

    private val ctx = HashMap<Long, Ctx>()

    /** A person's feet entered a zone this frame. */
    data class Entry(val trackId: Long, val zone: Zone)

    fun process(now: Double, shelfEvents: List<ShelfEvent>, entries: List<Entry>, ended: List<Long>): List<CameraWatcher.Event> {
        val out = ArrayList<CameraWatcher.Event>()

        for (ev in shelfEvents) {
            if (ev.kind != "UNRESOLVED_SHELF_INTERACTION") continue
            val it = ev.interaction
            val c = ctx.getOrPut(it.trackId) { Ctx() }
            c.unresolved += it
            if (it.verdict == "REPLACED") {
                val conf = if (it.strength == "STRONG") "MEDIUM" else "LOW"
                out += CameraWatcher.Event("POSSIBLE_PRODUCT_REPLACEMENT", cameraId, it.trackId,
                    "Something different was left on “${it.shelf.name}” where a product was. Check the shelf and the snapshot. This is not proof of theft.",
                    severity = if (conf == "LOW") "LOW" else "HIGH", confidence = conf)
            }
            // A quick grab-and-go reaches the door before the shelf check settles.
            val exitAt = c.exitAt
            if (exitAt != null && now - exitAt <= lateResolutionS) unpaidExit(c, listOf(it), it.trackId)?.let { e -> out += e }
        }

        for (e in entries) {
            val c = ctx.getOrPut(e.trackId) { Ctx() }
            when (e.zone.type) {
                com.boombiz.guard.ai.ZoneType.CASHIER -> c.cashierVisits += now
                com.boombiz.guard.ai.ZoneType.EXIT -> {
                    c.exitAt = now; c.exitZone = e.zone
                    c.unresolved.retainAll { now - (it.endedAt ?: it.startedAt) <= windowS }
                    if (c.unresolved.isNotEmpty()) unpaidExit(c, c.unresolved, e.trackId)?.let { ev -> out += ev }
                }
                else -> Unit
            }
        }

        for (t in ended) ctx.remove(t)
        return out
    }

    private fun unpaidExit(c: Ctx, live: List<Interaction>, trackId: Long): CameraWatcher.Event? {
        if (c.exitAlerted) return null
        c.exitAlerted = true
        val best = live.firstOrNull { it.strength == "STRONG" } ?: live.first()
        val since = best.endedAt ?: best.startedAt
        val paid = c.cashierVisits.any { it >= since }
        val conf = confidenceFor(best.strength, paid)
        val door = c.exitZone?.name ?: "the exit"
        val detail = buildString {
            append("Someone reached into “${best.shelf.name}”")
            append(if (best.strength == "UNVERIFIED") ", Guard couldn't see the shelf afterwards (people in the way)," else ", the shelf looks different,")
            append(" and they went to “$door”")
            append(if (paid) ". They passed the cashier, so they may have paid." else " without passing the cashier.")
            append(" Check the snapshot. This is not proof of theft.")
        }
        return CameraWatcher.Event("POSSIBLE_UNPAID_EXIT", cameraId, trackId, detail,
            severity = if (conf == "LOW") "LOW" else "HIGH", confidence = conf)
    }

    companion object {
        fun confidenceFor(strength: String?, visitedCashier: Boolean): String {
            if (strength == "UNVERIFIED") return "LOW" // "we couldn't check" must never read like evidence
            val strong = strength == "STRONG"
            return when {
                strong && !visitedCashier -> "HIGH"
                strong || !visitedCashier -> "MEDIUM"
                else -> "LOW"
            }
        }
    }
}
