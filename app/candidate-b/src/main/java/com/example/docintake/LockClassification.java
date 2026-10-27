package com.example.docintake;

/**
 * The frozen lock-outcome classification from the Candidate B spec. The mapping is fixed before
 * execution and must not be changed to make a run pass:
 *
 * <ul>
 *   <li>{@link #ACQUIRED_RELEASED} — a {@code FileLock} acquired and released cleanly (positive).</li>
 *   <li>{@link #LOCK_CONTENTION} — {@code tryLock} returned {@code null} (negative).</li>
 *   <li>{@link #SAME_JVM_CONTENTION} — {@code OverlappingFileLockException} thrown (negative).</li>
 *   <li>{@link #PLATFORM_IO_ERROR} — a platform I/O error; the raw exception and the operation,
 *       platform, and native/errno context are retained alongside this class (negative).</li>
 * </ul>
 *
 * A {@code null} outcome is {@code LOCK_CONTENTION}, never a silent retry. An
 * {@code OverlappingFileLockException} is {@code SAME_JVM_CONTENTION} and must not be conflated with
 * cross-process contention.
 */
public enum LockClassification {
    ACQUIRED_RELEASED("acquired_released"),
    LOCK_CONTENTION("lock_contention"),
    SAME_JVM_CONTENTION("same_jvm_contention"),
    PLATFORM_IO_ERROR("platform_io_error");

    private final String wireName;

    LockClassification(String wireName) {
        this.wireName = wireName;
    }

    /** The exact string recorded in the evidence ledger for this classification. */
    public String wireName() {
        return wireName;
    }
}
