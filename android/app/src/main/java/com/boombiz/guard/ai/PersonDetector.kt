package com.boombiz.guard.ai

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import java.nio.FloatBuffer
import java.security.MessageDigest

/**
 * YOLOX-nano person detector on ONNX Runtime (Apache-2.0, Megvii) — the same
 * model file the PC agent ships. Loaded ONLY if its SHA-256 matches, like the
 * PC's model_manager: a swapped or corrupted file is refused, never run.
 *
 * One shared instance for every camera (ORT sessions are thread-safe for
 * run(); callers serialise anyway to keep the phone cool).
 */
class PersonDetector private constructor(private val session: OrtSession, private val env: OrtEnvironment) : AutoCloseable {
    val inW = 416
    val inH = 416
    private val inputName = session.inputNames.first()
    var lastMs = 0.0
        private set

    @Synchronized
    fun detect(f: Frame): List<Detection> {
        val t0 = System.nanoTime()
        val (tensor, ratio) = FrameOps.letterbox(f, inW, inH)
        val dets = OnnxTensor.createTensor(env, FloatBuffer.wrap(tensor), longArrayOf(1, 3, inH.toLong(), inW.toLong())).use { input ->
            session.run(mapOf(inputName to input)).use { res ->
                val t = res[0] as OnnxTensor
                val buf = t.floatBuffer
                val out = FloatArray(buf.remaining()).also { buf.get(it) }
                Yolox.decode(out, inW, inH, ratio, f.width, f.height)
            }
        }
        lastMs = (System.nanoTime() - t0) / 1e6
        return dets
    }

    override fun close() = session.close()

    companion object {
        const val MODEL_ASSET = "yolox_nano.onnx"
        const val MODEL_VERSION = "guard-person-yolox-nano-0.1.1"
        private const val SHA256 = "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"

        class IntegrityError(msg: String) : Exception(msg)

        fun load(ctx: Context): PersonDetector {
            val bytes = ctx.assets.open(MODEL_ASSET).use { it.readBytes() }
            val digest = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
            if (digest != SHA256) throw IntegrityError("The Guard AI model failed its integrity check and was not loaded.")
            val env = OrtEnvironment.getEnvironment()
            val opts = OrtSession.SessionOptions().apply {
                setIntraOpNumThreads(2) // leave cores for decoding; phones throttle when hot
                runCatching { addXnnpack(mapOf("intra_op_num_threads" to "2")) }
            }
            return PersonDetector(env.createSession(bytes, opts), env)
        }
    }
}
