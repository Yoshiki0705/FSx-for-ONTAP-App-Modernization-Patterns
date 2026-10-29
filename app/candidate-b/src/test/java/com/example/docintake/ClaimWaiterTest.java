package com.example.docintake;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.channels.OverlappingFileLockException;
import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * Tier 1 (local/unit) tests for the bounded claim wait. The cross-process tests use a second JVM on
 * a local temporary directory; they establish the wait logic against a real {@code null}
 * {@code tryLock}, not NFS behavior.
 */
class ClaimWaiterTest {

    /** Virtual time: sleeping advances the clock, so tests do not wait. */
    private static final class FakeClock implements ClaimWaiter.Clock {
        long nowNanos = 0;

        @Override
        public long nanoTime() {
            return nowNanos;
        }

        @Override
        public void sleepMillis(long millis) {
            nowNanos += millis * 1_000_000L;
        }
    }

    private static FileLockClaimStore.Claim realClaim(Path tmp) throws IOException {
        return new FileLockClaimStore(tmp).tryClaim("fake-source");
    }

    @Test
    void noWaitMakesExactlyOneAttempt() throws IOException {
        AtomicInteger calls = new AtomicInteger();
        ClaimWaiter.Result result = ClaimWaiter.noWait().claim(() -> {
            calls.incrementAndGet();
            return null;
        });
        assertThat(result.claim()).isNull();
        assertThat(result.attempts()).isEqualTo(1);
        assertThat(calls.get()).isEqualTo(1);
    }

    @Test
    void retriesUntilTheClaimIsTaken(@TempDir Path tmp) throws IOException {
        FakeClock clock = new FakeClock();
        AtomicInteger calls = new AtomicInteger();
        ClaimWaiter.Result result = new ClaimWaiter(1000, 100, clock).claim(() ->
            calls.incrementAndGet() < 4 ? null : realClaim(tmp));
        assertThat(result.claim()).isNotNull();
        assertThat(result.attempts()).isEqualTo(4);
        assertThat(result.waitedMillis()).isEqualTo(300);
        result.claim().release();
    }

    @Test
    void givesUpAtTheDeadlineAndReportsAttemptsAndTime() throws IOException {
        FakeClock clock = new FakeClock();
        ClaimWaiter.Result result = new ClaimWaiter(450, 100, clock).claim(() -> null);
        assertThat(result.claim()).isNull();
        // Attempts at 0, 100, 200, 300, 400 ms, then a 50 ms sleep and a last attempt at 450 ms.
        assertThat(result.attempts()).isEqualTo(6);
        assertThat(result.waitedMillis()).isEqualTo(450);
    }

    @Test
    void sameJvmOverlapAndIoErrorAreNotRetried() {
        AtomicInteger calls = new AtomicInteger();
        ClaimWaiter waiter = new ClaimWaiter(1000, 100, new FakeClock());
        assertThatThrownBy(() -> waiter.claim(() -> {
            calls.incrementAndGet();
            throw new OverlappingFileLockException();
        })).isInstanceOf(OverlappingFileLockException.class);
        assertThatThrownBy(() -> waiter.claim(() -> {
            calls.incrementAndGet();
            throw new IOException("disk gone");
        })).isInstanceOf(IOException.class);
        assertThat(calls.get()).isEqualTo(2);
    }

    @Test
    void rejectsInvalidSettings() {
        assertThatThrownBy(() -> new ClaimWaiter(-1, 10, ClaimWaiter.SYSTEM))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new ClaimWaiter(10, 0, ClaimWaiter.SYSTEM))
            .isInstanceOf(IllegalArgumentException.class);
    }

    private static Process startHolder(Path root, String item, long holdMillis) throws IOException {
        String java = Path.of(System.getProperty("java.home"), "bin", "java").toString();
        Process holder = new ProcessBuilder(
            java, "-cp", System.getProperty("java.class.path"),
            LockHolder.class.getName(), root.toString(), item, Long.toString(holdMillis))
            .redirectErrorStream(true).start();
        String first = new BufferedReader(new InputStreamReader(holder.getInputStream())).readLine();
        assertThat(first).isEqualTo("LOCKED");
        return holder;
    }

    @Test
    void crossProcessClaimIsTakenAfterTheHolderExits(@TempDir Path tmp) throws Exception {
        Process holder = startHolder(tmp, "item-wait", 800);
        try {
            FileLockClaimStore store = new FileLockClaimStore(tmp);
            ClaimWaiter.Result result = new ClaimWaiter(10_000, 50, ClaimWaiter.SYSTEM)
                .claim(() -> store.tryClaim("item-wait"));
            assertThat(result.claim()).isNotNull();
            assertThat(result.attempts()).isGreaterThan(1);
            result.claim().release();
        } finally {
            holder.destroyForcibly();
        }
    }

    @Test
    void crossProcessContentionPastTheDeadlineStaysLockContention(@TempDir Path tmp) throws Exception {
        Process holder = startHolder(tmp, "item-held", 30_000);
        try {
            FileLockClaimStore store = new FileLockClaimStore(tmp);
            ClaimWaiter.Result result = new ClaimWaiter(400, 50, ClaimWaiter.SYSTEM)
                .claim(() -> store.tryClaim("item-held"));
            assertThat(result.claim()).isNull();
            assertThat(result.attempts()).isGreaterThan(1);
            assertThat(result.waitedMillis()).isGreaterThanOrEqualTo(400);
        } finally {
            holder.destroyForcibly();
        }
    }
}
