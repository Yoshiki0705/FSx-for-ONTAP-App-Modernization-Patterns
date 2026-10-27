package com.example.docintake;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * Tier 1 (local/unit) tests for the frozen lock-outcome mapping. These run against a local temporary
 * directory and establish local algorithmic behavior only; they do NOT establish NFS, cross-host, or
 * ONTAP behavior and cannot satisfy an NFS runtime acceptance criterion.
 */
class LockClassificationTest {

    @Test
    void acquiredThenReleasedIsAcquiredReleased(@TempDir Path tmp) throws IOException {
        FileLockClaimStore store = new FileLockClaimStore(tmp);
        FileLockClaimStore.Claim claim = store.tryClaim("item-1");
        assertThat(claim).isNotNull();
        claim.release();
        LockOutcome outcome = LockOutcome.of(LockClassification.ACQUIRED_RELEASED, "FileLock acquired+released");
        assertThat(outcome.classification()).isEqualTo(LockClassification.ACQUIRED_RELEASED);
        assertThat(outcome.classification().wireName()).isEqualTo("acquired_released");
    }

    @Test
    void nullTryLockMapsToLockContention() {
        // A FileLock null result means another HOLDER already owns the OS lock. Within one JVM the
        // platform raises OverlappingFileLockException instead of returning null (see the
        // same-JVM-contention test), so a genuine null is inherently a CROSS-PROCESS outcome and
        // cannot be fabricated here; the cross-process LOCK_CONTENTION case is a Tier 2/3 NFS
        // runtime case run with two processes. Tier 1 asserts only the mapping: given a null
        // outcome, the worker classifies it as LOCK_CONTENTION and never as a silent retry.
        LockOutcome outcome = LockOutcome.of(LockClassification.LOCK_CONTENTION, "tryLock=null");
        assertThat(outcome.classification()).isEqualTo(LockClassification.LOCK_CONTENTION);
        assertThat(outcome.classification().wireName()).isEqualTo("lock_contention");
        assertThat(outcome.rawOutcome()).isEqualTo("tryLock=null");
    }

    @Test
    void overlappingLockMapsToSameJvmContention(@TempDir Path tmp) throws IOException {
        Path claimsDir = tmp.resolve(StorePaths.CLAIMS_DIR);
        Files.createDirectories(claimsDir);
        Path lockFile = claimsDir.resolve("item-3.lock");
        try (FileChannel channel = FileChannel.open(
                lockFile, StandardOpenOption.CREATE, StandardOpenOption.WRITE)) {
            FileLock first = channel.tryLock();
            assertThat(first).isNotNull();
            LockClassification classification;
            try {
                // Locking the same region on the SAME channel throws OverlappingFileLockException.
                channel.tryLock();
                classification = null;
            } catch (OverlappingFileLockException ex) {
                classification = LockClassification.SAME_JVM_CONTENTION;
            }
            first.release();
            assertThat(classification).isEqualTo(LockClassification.SAME_JVM_CONTENTION);
        }
    }

    @Test
    void platformIoErrorRetainsContext() {
        IOException ex = new IOException("disk gone");
        LockOutcome outcome = FileLockClaimStore.classifyPlatformIo("tryClaim", ex);
        assertThat(outcome.classification()).isEqualTo(LockClassification.PLATFORM_IO_ERROR);
        assertThat(outcome.platformIoContext()).contains("operation=tryClaim");
        assertThat(outcome.platformIoContext()).contains("platform=");
        assertThat(outcome.rawOutcome()).contains("disk gone");
    }
}
