package com.example.docintake;

import java.io.IOException;
import java.nio.channels.OverlappingFileLockException;

/**
 * Bounded, explicit wait for a claim. The frozen classification is unchanged: when the deadline
 * passes with {@code tryLock} still returning {@code null}, the outcome is {@code LOCK_CONTENTION}.
 * The wait is never silent: the result carries the number of attempts and the time waited, and the
 * caller records both.
 *
 * <p>Only a {@code null} result (another process holds the lock) is retried. An
 * {@link OverlappingFileLockException} (same JVM) and an {@link IOException} (platform error) are
 * not retried and propagate unchanged, so they keep their own classifications.
 */
public final class ClaimWaiter {

    /** One attempt to take the claim; returns {@code null} when another process holds the lock. */
    @FunctionalInterface
    public interface Attempt {
        FileLockClaimStore.Claim tryOnce() throws IOException;
    }

    /** Time source and sleeper, replaceable in tests. */
    public interface Clock {
        long nanoTime();

        void sleepMillis(long millis) throws InterruptedException;
    }

    public static final Clock SYSTEM = new Clock() {
        @Override
        public long nanoTime() {
            return System.nanoTime();
        }

        @Override
        public void sleepMillis(long millis) throws InterruptedException {
            Thread.sleep(millis);
        }
    };

    /** The claim (or {@code null} on contention), how many attempts were made, and the time waited. */
    public record Result(FileLockClaimStore.Claim claim, int attempts, long waitedMillis) {}

    private final long maxWaitMillis;
    private final long intervalMillis;
    private final Clock clock;

    public ClaimWaiter(long maxWaitMillis, long intervalMillis, Clock clock) {
        if (maxWaitMillis < 0) {
            throw new IllegalArgumentException("maxWaitMillis must be >= 0");
        }
        if (intervalMillis < 1) {
            throw new IllegalArgumentException("intervalMillis must be >= 1");
        }
        this.maxWaitMillis = maxWaitMillis;
        this.intervalMillis = intervalMillis;
        this.clock = clock;
    }

    /** A waiter that makes exactly one attempt: the behavior before wait/retry existed. */
    public static ClaimWaiter noWait() {
        return new ClaimWaiter(0, 1, SYSTEM);
    }

    public Result claim(Attempt attempt) throws IOException {
        long start = clock.nanoTime();
        int attempts = 0;
        while (true) {
            attempts++;
            FileLockClaimStore.Claim claim = attempt.tryOnce();
            long waited = (clock.nanoTime() - start) / 1_000_000L;
            if (claim != null) {
                return new Result(claim, attempts, waited);
            }
            long remaining = maxWaitMillis - waited;
            if (remaining <= 0) {
                return new Result(null, attempts, waited);
            }
            try {
                clock.sleepMillis(Math.min(intervalMillis, remaining));
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                return new Result(null, attempts, (clock.nanoTime() - start) / 1_000_000L);
            }
        }
    }
}
