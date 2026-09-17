package com.boombiz.guard.data

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.boombiz.guard.ai.DayHours
import com.boombiz.guard.ai.Zone
import com.boombiz.guard.ai.ZoneType
import java.time.DayOfWeek

data class Camera(
    val id: Long, val name: String, val brand: String, val host: String, val rtspPort: Int,
    val channel: Int, val customPath: String?, val username: String, val passwordSealed: String?, val enabled: Boolean,
    /** Also play the siren through this camera's own speaker (ONVIF audio back-channel). */
    val speaker: Boolean = false,
)

data class Incident(
    val id: String, val ref: String, val type: String, val severity: String, val cameraId: Long?,
    val cameraName: String?, val title: String, val description: String?, val occurredAt: String,
    val status: String, val acknowledgedBy: String?, val acknowledgedAt: String?, val snapshotPath: String?,
    /** LOW | MEDIUM | HIGH for theft signals; null for everything else. */
    val confidence: String? = null,
)

data class SyncJob(val id: Long, val op: String, val incidentId: String, val attempts: Int)

/** The phone's local database. Plain SQLite: small schema, no annotation processors to build. */
class Store(ctx: Context) : SQLiteOpenHelper(ctx, "guard.db", null, 2) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        db.execSQL("""CREATE TABLE cameras (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, brand TEXT NOT NULL,
            host TEXT NOT NULL, rtsp_port INTEGER NOT NULL, channel INTEGER NOT NULL, custom_path TEXT,
            username TEXT NOT NULL, password_sealed TEXT, enabled INTEGER NOT NULL DEFAULT 1)""")
        db.execSQL("""CREATE TABLE zones (id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id INTEGER NOT NULL, name TEXT NOT NULL,
            type TEXT NOT NULL, polygon TEXT NOT NULL)""")
        db.execSQL("CREATE TABLE hours (day INTEGER PRIMARY KEY, opens TEXT, closes TEXT, closed INTEGER NOT NULL DEFAULT 0)")
        db.execSQL("""CREATE TABLE incidents (id TEXT PRIMARY KEY, ref TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL,
            camera_id INTEGER, camera_name TEXT, title TEXT NOT NULL, description TEXT, occurred_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'UNREVIEWED', acknowledged_by TEXT, acknowledged_at TEXT, snapshot_path TEXT,
            created_ms INTEGER NOT NULL)""")
        db.execSQL("""CREATE TABLE sync_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL, incident_id TEXT NOT NULL,
            priority INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
            next_at_ms INTEGER NOT NULL DEFAULT 0, last_error TEXT, cloud_id TEXT, UNIQUE(op, incident_id))""")
        onUpgrade(db, 1, 2)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) {
            db.execSQL("ALTER TABLE incidents ADD COLUMN confidence TEXT")
            db.execSQL("ALTER TABLE cameras ADD COLUMN speaker INTEGER NOT NULL DEFAULT 0")
        }
    }

    // ── settings ─────────────────────────────────────────────────────
    fun get(key: String): String? = readableDatabase.rawQuery("SELECT value FROM settings WHERE key=?", arrayOf(key))
        .use { if (it.moveToFirst()) it.getString(0) else null }

    fun put(key: String, value: String?) {
        if (value == null) writableDatabase.delete("settings", "key=?", arrayOf(key))
        else writableDatabase.insertWithOnConflict("settings", null,
            ContentValues().apply { put("key", key); put("value", value) }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    // ── cameras ──────────────────────────────────────────────────────
    private fun Cursor.camera() = Camera(getLong(0), getString(1), getString(2), getString(3), getInt(4), getInt(5),
        getString(6), getString(7), getString(8), getInt(9) == 1, getInt(10) == 1)

    fun cameras(): List<Camera> = readableDatabase.rawQuery(
        "SELECT id,name,brand,host,rtsp_port,channel,custom_path,username,password_sealed,enabled,speaker FROM cameras ORDER BY id", null
    ).use { c -> buildList { while (c.moveToNext()) add(c.camera()) } }

    fun camera(id: Long): Camera? = cameras().firstOrNull { it.id == id }

    fun saveCamera(c: Camera): Long {
        val v = ContentValues().apply {
            put("name", c.name); put("brand", c.brand); put("host", c.host); put("rtsp_port", c.rtspPort)
            put("channel", c.channel); put("custom_path", c.customPath); put("username", c.username)
            put("password_sealed", c.passwordSealed); put("enabled", if (c.enabled) 1 else 0); put("speaker", if (c.speaker) 1 else 0)
        }
        return if (c.id == 0L) writableDatabase.insert("cameras", null, v)
        else { writableDatabase.update("cameras", v, "id=?", arrayOf(c.id.toString())); c.id }
    }

    fun deleteCamera(id: Long) {
        writableDatabase.delete("zones", "camera_id=?", arrayOf(id.toString()))
        writableDatabase.delete("cameras", "id=?", arrayOf(id.toString()))
    }

    // ── zones ────────────────────────────────────────────────────────
    fun zones(cameraId: Long): List<Zone> = readableDatabase.rawQuery(
        "SELECT id,camera_id,name,type,polygon FROM zones WHERE camera_id=? ORDER BY id", arrayOf(cameraId.toString())
    ).use { c -> buildList { while (c.moveToNext()) add(Zone(c.getLong(0), c.getLong(1), c.getString(2),
        ZoneType.valueOf(c.getString(3)), Zone.decode(c.getString(4)))) } }

    fun addZone(z: Zone): Long = writableDatabase.insert("zones", null, ContentValues().apply {
        put("camera_id", z.cameraId); put("name", z.name); put("type", z.type.name); put("polygon", z.encode())
    })

    fun deleteZone(id: Long) = writableDatabase.delete("zones", "id=?", arrayOf(id.toString()))

    // ── business hours ───────────────────────────────────────────────
    fun hours(): List<DayHours> = readableDatabase.rawQuery("SELECT day,opens,closes,closed FROM hours ORDER BY day", null)
        .use { c -> buildList { while (c.moveToNext()) add(DayHours(DayOfWeek.of(c.getInt(0)), c.getString(1), c.getString(2), c.getInt(3) == 1)) } }

    fun saveHours(days: List<DayHours>) = writableDatabase.run {
        beginTransaction()
        try {
            delete("hours", null, null)
            for (d in days) insert("hours", null, ContentValues().apply {
                put("day", d.day.value); put("opens", d.opens); put("closes", d.closes); put("closed", if (d.closed) 1 else 0)
            })
            setTransactionSuccessful()
        } finally { endTransaction() }
    }

    // ── incidents ────────────────────────────────────────────────────
    private fun Cursor.incident() = Incident(getString(0), getString(1), getString(2), getString(3),
        if (isNull(4)) null else getLong(4), getString(5), getString(6), getString(7), getString(8), getString(9),
        getString(10), getString(11), getString(12), getString(13))

    private val incCols = "id,ref,type,severity,camera_id,camera_name,title,description,occurred_at,status,acknowledged_by,acknowledged_at,snapshot_path,confidence"

    fun incidents(limit: Int = 100): List<Incident> = readableDatabase.rawQuery(
        "SELECT $incCols FROM incidents ORDER BY created_ms DESC LIMIT $limit", null
    ).use { c -> buildList { while (c.moveToNext()) add(c.incident()) } }

    fun incident(id: String): Incident? = readableDatabase.rawQuery("SELECT $incCols FROM incidents WHERE id=?", arrayOf(id))
        .use { if (it.moveToFirst()) it.incident() else null }

    fun insertIncident(i: Incident) = writableDatabase.insert("incidents", null, ContentValues().apply {
        put("id", i.id); put("ref", i.ref); put("type", i.type); put("severity", i.severity); put("camera_id", i.cameraId)
        put("camera_name", i.cameraName); put("title", i.title); put("description", i.description)
        put("occurred_at", i.occurredAt); put("status", i.status); put("snapshot_path", i.snapshotPath); put("confidence", i.confidence)
        put("created_ms", System.currentTimeMillis())
    })

    /** UNREVIEWED → ACKNOWLEDGED. False when it was already reviewed (a local review always wins). */
    fun acknowledge(id: String, by: String, at: String): Boolean = writableDatabase.update("incidents",
        ContentValues().apply { put("status", "ACKNOWLEDGED"); put("acknowledged_by", by); put("acknowledged_at", at) },
        "id=? AND status='UNREVIEWED'", arrayOf(id)) == 1

    fun lastIncidentAt(): String? = readableDatabase.rawQuery("SELECT MAX(occurred_at) FROM incidents", null)
        .use { if (it.moveToFirst()) it.getString(0) else null }

    /** Per-day sequence for refs, like the PC's BG-<loc>-<yyyymmdd>-000001. */
    @Synchronized
    fun nextSeq(day: String): Int {
        val k = "incident_seq:$day"
        val n = (get(k)?.toIntOrNull() ?: 0) + 1
        put(k, n.toString())
        return n
    }

    /** Local retention: incidents older than [days] go, with their snapshots. → snapshot paths to delete. */
    fun pruneIncidents(days: Int): List<String> {
        val cutoff = System.currentTimeMillis() - days * 86_400_000L
        val paths = readableDatabase.rawQuery("SELECT snapshot_path FROM incidents WHERE created_ms < ? AND snapshot_path IS NOT NULL",
            arrayOf(cutoff.toString())).use { c -> buildList { while (c.moveToNext()) add(c.getString(0)) } }
        writableDatabase.execSQL("DELETE FROM sync_jobs WHERE incident_id IN (SELECT id FROM incidents WHERE created_ms < ?)", arrayOf(cutoff))
        writableDatabase.delete("incidents", "created_ms < ?", arrayOf(cutoff.toString()))
        return paths
    }

    // ── sync queue ───────────────────────────────────────────────────
    /** Queue (or re-queue) a job. */
    fun want(op: String, incidentId: String, priority: Int, reset: Boolean = false) {
        val v = ContentValues().apply { put("op", op); put("incident_id", incidentId); put("priority", priority) }
        val id = writableDatabase.insertWithOnConflict("sync_jobs", null, v, SQLiteDatabase.CONFLICT_IGNORE)
        if (id == -1L && reset) writableDatabase.execSQL(
            "UPDATE sync_jobs SET status='PENDING', attempts=0, next_at_ms=0 WHERE op=? AND incident_id=? AND status IN ('DONE','FAILED')",
            arrayOf(op, incidentId))
    }

    fun dueJobs(now: Long, limit: Int = 10): List<SyncJob> = readableDatabase.rawQuery(
        "SELECT id,op,incident_id,attempts FROM sync_jobs WHERE status IN ('PENDING','RETRY') AND next_at_ms <= ? ORDER BY priority, id LIMIT $limit",
        arrayOf(now.toString())).use { c -> buildList { while (c.moveToNext()) add(SyncJob(c.getLong(0), c.getString(1), c.getString(2), c.getInt(3))) } }

    fun cloudId(incidentId: String): String? = readableDatabase.rawQuery(
        "SELECT cloud_id FROM sync_jobs WHERE op='INCIDENT_UPSERT' AND incident_id=?", arrayOf(incidentId)
    ).use { if (it.moveToFirst()) it.getString(0) else null }

    fun jobDone(id: Long, cloudId: String? = null) = writableDatabase.update("sync_jobs", ContentValues().apply {
        put("status", "DONE"); putNull("last_error"); if (cloudId != null) put("cloud_id", cloudId)
    }, "id=?", arrayOf(id.toString()))

    fun jobRetry(id: Long, attempts: Int, nextAt: Long, err: String) = writableDatabase.update("sync_jobs", ContentValues().apply {
        put("status", "RETRY"); put("attempts", attempts); put("next_at_ms", nextAt); put("last_error", err)
    }, "id=?", arrayOf(id.toString()))

    fun jobFail(id: Long, err: String) = writableDatabase.update("sync_jobs", ContentValues().apply {
        put("status", "FAILED"); put("last_error", err)
    }, "id=?", arrayOf(id.toString()))

    fun pendingJobs(): Int = readableDatabase.rawQuery(
        "SELECT COUNT(*) FROM sync_jobs WHERE status IN ('PENDING','RETRY')", null).use { it.moveToFirst(); it.getInt(0) }
}
