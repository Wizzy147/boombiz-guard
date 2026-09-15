package com.boombiz.guard

import com.boombiz.guard.data.PinHash
import com.boombiz.guard.watch.IncidentRules
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TheftTest {

    @Test fun pinIsHashedWithSaltAndChecked() {
        val salt = PinHash.newSalt()
        val h = PinHash.hash("4821", salt)
        assertFalse("4821" in h)
        assertTrue(PinHash.matches("4821", salt, h))
        assertFalse(PinHash.matches("4822", salt, h))
        assertNotEquals("same PIN, new salt, new hash", h, PinHash.hash("4821", PinHash.newSalt()))
    }

    @Test fun pinRules() {
        assertTrue(PinHash.valid("1234")); assertTrue(PinHash.valid("12345678"))
        assertFalse(PinHash.valid("123")); assertFalse(PinHash.valid("12ab")); assertFalse(PinHash.valid("123456789"))
    }

    @Test fun unpluggedAfterHoursIsCriticalWithTheSiren() {
        val closed = IncidentRules.RULES["DEVICE_UNPLUGGED_CLOSED"]!!
        val open = IncidentRules.RULES["DEVICE_UNPLUGGED_OPEN"]!!
        assertEquals(IncidentRules.UNPLUGGED, closed.type)
        assertEquals("CRITICAL", closed.severity)
        assertEquals("LOW", open.severity)
        val alarm = IncidentRules.ALARMS[IncidentRules.UNPLUGGED]!!
        assertTrue("siren only when the shop is closed", alarm.afterClosingOnly)
        assertEquals(0, alarm.cooldownS)
        assertEquals("syncs with after-hours, first after fire", 20, IncidentRules.priority(IncidentRules.UNPLUGGED))
    }

    @Test fun replugEndsTheEpisode() {
        val d = IncidentRules.Dedup()
        val r = IncidentRules.RULES["DEVICE_UNPLUGGED_CLOSED"]!!
        assertTrue(d.isNew(r, 0, null, 0.0))
        assertFalse("a loose cable doesn't spam", d.isNew(r, 0, null, 20.0))
        d.clear(IncidentRules.UNPLUGGED, 0)
        assertTrue(d.isNew(r, 0, null, 25.0))
    }
}
