package com.boombiz.guard.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Bitmap
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.ArrayAdapter
import android.widget.TextView
import androidx.media3.common.util.UnstableApi
import com.boombiz.guard.GuardApp
import com.boombiz.guard.ai.Frame
import com.boombiz.guard.camera.CameraSpeaker
import com.boombiz.guard.camera.RtspFrameSource
import com.boombiz.guard.camera.StreamUrls
import com.boombiz.guard.data.Camera
import com.boombiz.guard.data.Vault
import com.boombiz.guard.watch.WatchService

/**
 * Add or edit one camera. The installer picks the recorder brand and channel
 * (as on the DVR's screen), and "Test" pulls one live picture so they can see
 * it's the right camera before saving. The password is Keystore-sealed.
 */
@UnstableApi
class CameraEditActivity : Activity() {
    private val app get() = application as GuardApp
    private val ui = Handler(Looper.getMainLooper())
    private var existing: Camera? = null
    private var tester: RtspFrameSource? = null
    private lateinit var name: EditText
    private lateinit var brand: Spinner
    private lateinit var host: EditText
    private lateinit var port: EditText
    private lateinit var channel: EditText
    private lateinit var path: EditText
    private lateinit var user: EditText
    private lateinit var pass: EditText
    private lateinit var enabled: CheckBox
    private lateinit var speaker: CheckBox
    private lateinit var speakerResult: TextView
    private var speakerTest: CameraSpeaker? = null
    private lateinit var result: TextView
    private lateinit var preview: ImageView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        existing = intent.getLongExtra("id", 0L).takeIf { it != 0L }?.let { app.store.camera(it) }
        val c = existing
        val p = page(if (c == null) "Add a camera" else "Edit camera")

        name = p.field("Name (what the shop calls it)", c?.name ?: "", hint = "Entrance")
        p.text("Recorder or camera brand", 14f, bold = true, color = C.MUTED)
        brand = Spinner(this).apply {
            adapter = ArrayAdapter(this@CameraEditActivity, android.R.layout.simple_spinner_dropdown_item,
                StreamUrls.BRAND_LIST.map { it.label })
            setSelection(StreamUrls.BRANDS.indexOf(c?.brand ?: "HIKVISION").coerceAtLeast(0))
        }
        p.addView(brand)
        val login = p.text("", 14f, color = C.MUTED)
        brand.onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: android.widget.AdapterView<*>?, v: android.view.View?, pos: Int, id: Long) {
                login.text = "Login: ${StreamUrls.BRAND_LIST[pos].login}"
            }
            override fun onNothingSelected(parent: android.widget.AdapterView<*>?) = Unit
        }
        host = p.field("Recorder or camera IP address", c?.host ?: "", InputType.TYPE_CLASS_PHONE, "192.168.1.64")
        port = p.field("RTSP port", (c?.rtspPort ?: 554).toString(), InputType.TYPE_CLASS_NUMBER)
        channel = p.field("Channel (camera number on the recorder, or lens number; usually 1)", (c?.channel ?: 1).toString(), InputType.TYPE_CLASS_NUMBER)
        path = p.field("RTSP path (Other only)", c?.customPath ?: "", hint = "/live/ch00_1")
        user = p.field("Username", c?.username ?: "admin")
        pass = p.field("Password", "", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD,
            if (c?.passwordSealed != null) "Saved — leave blank to keep it" else null)
        enabled = CheckBox(this).apply { text = "Guard watches this camera"; isChecked = c?.enabled ?: true; setTextColor(C.INK) }
        p.addView(enabled)
        speaker = CheckBox(this).apply {
            text = "Also sound the siren through this camera's speaker"; isChecked = c?.speaker ?: false; setTextColor(C.INK)
        }
        p.addView(speaker)

        p.button("Test — show me this camera", primary = false) { test() }
        result = p.text("", 15f)
        preview = ImageView(this).apply { adjustViewBounds = true }
        p.addView(preview, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        p.button("Test the camera's speaker (plays the siren for 3 seconds)", primary = false) { testSpeaker() }
        speakerResult = p.text("", 15f)

        p.button("Save") { save() }
        if (c != null) {
            p.button("Areas: shelves, exit, cashier, restricted, ignore", primary = false) {
                startActivity(Intent(this, ZoneEditorActivity::class.java).putExtra("id", c.id))
            }
            p.button("Remove this camera", primary = false) { remove(c) }
        }
    }

    override fun onDestroy() { tester?.stop(); speakerTest?.stop(); super.onDestroy() }

    private fun testSpeaker() {
        validate()?.let { speakerResult.text = it; speakerResult.setTextColor(C.RED); return }
        if (speakerTest != null) return
        speakerResult.text = "Sending the siren to the camera…"; speakerResult.setTextColor(C.INK)
        val s = CameraSpeaker(host.text.toString().trim(), port.text.toString().toInt(),
            StreamUrls.path(brandKey(), channel.text.toString().toInt(), path.text.toString(), user.text.toString(), password()),
            user.text.toString(), password())
        speakerTest = s
        Thread {
            val outcome = runCatching { s.play(3) }
            ui.post {
                speakerTest = null
                outcome.onSuccess {
                    speakerResult.text = "The camera accepted the siren. Did you hear it from the camera? If yes, tick “Also sound the siren through this camera's speaker” and Save."
                    speakerResult.setTextColor(C.GREEN)
                }.onFailure {
                    speakerResult.text = it.message ?: "Couldn't play sound on this camera."
                    speakerResult.setTextColor(C.RED)
                }
            }
        }.start()
    }

    private fun brandKey() = StreamUrls.BRANDS[brand.selectedItemPosition]

    private fun password(): String? = pass.text.toString().ifEmpty { existing?.passwordSealed?.let { Vault.open(it) } }

    private fun validate(): String? {
        val h = host.text.toString().trim()
        return when {
            name.text.isBlank() -> "Give the camera a name."
            !StreamUrls.isLan(h) -> "Type the recorder's local IP address, like 192.168.1.64. Guard only connects to recorders on the shop's own network."
            port.text.toString().toIntOrNull() !in 1..65535 -> "The RTSP port is a number, usually 554."
            channel.text.toString().toIntOrNull() !in 1..64 -> "The channel is the camera's number on the recorder, like 1."
            brandKey() == "OTHER" && path.text.isBlank() -> "Type the RTSP path from the camera's manual."
            else -> null
        }
    }

    private fun test() {
        validate()?.let { result.text = it; result.setTextColor(C.RED); return }
        tester?.stop()
        result.text = "Connecting to the camera…"; result.setTextColor(C.INK)
        val url = StreamUrls.url(host.text.toString(), port.text.toString().toInt(), brandKey(), channel.text.toString().toInt(),
            path.text.toString(), user.text.toString(), password())
        var got = false
        val src = RtspFrameSource(this, url, 1.0, onFrame = { f: Frame ->
            if (!got) {
                got = true
                val bmp = Bitmap.createBitmap(f.argb, f.width, f.height, Bitmap.Config.ARGB_8888)
                ui.post {
                    preview.setImageBitmap(bmp)
                    result.text = "Working. Is this the right camera? If yes, tap Save."
                    result.setTextColor(C.GREEN)
                    tester?.stop(); tester = null
                }
            }
        }, onState = { s, msg ->
            if (s == RtspFrameSource.State.AUTH_ERROR || (s == RtspFrameSource.State.OFFLINE && !got)) ui.post {
                result.text = when (s) {
                    RtspFrameSource.State.AUTH_ERROR -> "The recorder refused the username or password."
                    else -> "No picture. Check the IP address, the channel and that this device is on the shop Wi-Fi. ${msg ?: ""}"
                }
                result.setTextColor(C.RED)
                tester?.stop(); tester = null
            }
        })
        tester = src
        src.start()
    }

    private fun save() {
        validate()?.let { AlertDialog.Builder(this).setMessage(it).setPositiveButton("OK", null).show(); return }
        val sealed = pass.text.toString().ifEmpty { null }?.let { Vault.seal(it) } ?: existing?.passwordSealed
        app.store.saveCamera(Camera(existing?.id ?: 0L, name.text.toString().trim().take(60), brandKey(), host.text.toString().trim(),
            port.text.toString().toInt(), channel.text.toString().toInt(), path.text.toString().trim().ifEmpty { null },
            user.text.toString().trim(), sealed, enabled.isChecked, speaker.isChecked))
        WatchService.reload(this)
        finish()
    }

    private fun remove(c: Camera) {
        AlertDialog.Builder(this).setMessage("Stop watching ${c.name} and remove it from Guard?")
            .setPositiveButton("Remove") { _, _ -> app.store.deleteCamera(c.id); WatchService.reload(this); finish() }
            .setNegativeButton("Cancel", null).show()
    }
}
