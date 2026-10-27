package com.example.docintake;

/**
 * An immutable lock outcome: the mapped {@link LockClassification}, the raw outcome string (the
 * native result or the raw exception class and message), and the platform-I/O context when the
 * outcome is {@link LockClassification#PLATFORM_IO_ERROR}. Both the raw outcome and the mapped class
 * are retained unchanged after capture, per the spec.
 */
public final class LockOutcome {

    private final LockClassification classification;
    private final String rawOutcome;
    private final String platformIoContext;

    private LockOutcome(LockClassification classification, String rawOutcome, String platformIoContext) {
        this.classification = classification;
        this.rawOutcome = rawOutcome;
        this.platformIoContext = platformIoContext;
    }

    public static LockOutcome of(LockClassification classification, String rawOutcome) {
        return new LockOutcome(classification, rawOutcome, null);
    }

    public static LockOutcome platformIoError(String rawOutcome, String platformIoContext) {
        return new LockOutcome(LockClassification.PLATFORM_IO_ERROR, rawOutcome, platformIoContext);
    }

    public LockClassification classification() {
        return classification;
    }

    public String rawOutcome() {
        return rawOutcome;
    }

    /** Operation / platform / native-or-errno context; non-null only for a platform I/O error. */
    public String platformIoContext() {
        return platformIoContext;
    }
}
