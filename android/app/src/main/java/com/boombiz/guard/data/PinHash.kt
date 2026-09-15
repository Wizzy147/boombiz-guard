package com.boombiz.guard.data

import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec

/**
 * The settings PIN, stored only as a salted PBKDF2 hash. Pure JVM so it's
 * unit-tested. After [MAX_TRIES] wrong PINs the lock refuses for [LOCKOUT_MS],
 * so a 4-digit PIN can't be guessed by trying them all on the counter.
 */
object PinHash {
    const val MAX_TRIES = 5
    const val LOCKOUT_MS = 5 * 60_000L
    private const val ITERATIONS = 60_000

    fun valid(pin: String) = pin.length in 4..8 && pin.all { it.isDigit() }

    fun newSalt(): String = ByteArray(16).also { SecureRandom().nextBytes(it) }.let { Base64.getEncoder().encodeToString(it) }

    fun hash(pin: String, salt: String): String {
        val spec = PBEKeySpec(pin.toCharArray(), Base64.getDecoder().decode(salt), ITERATIONS, 256)
        val key = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).encoded
        return Base64.getEncoder().encodeToString(key)
    }

    fun matches(pin: String, salt: String, expected: String): Boolean =
        MessageDigest.isEqual(hash(pin, salt).toByteArray(), expected.toByteArray())
}
