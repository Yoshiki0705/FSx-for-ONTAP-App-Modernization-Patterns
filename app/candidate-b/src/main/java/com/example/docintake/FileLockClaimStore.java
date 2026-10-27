package com.example.docintake;

import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;

/**
 * Exclusive claim coordination using {@link java.nio.channels.FileLock}. Each item maps to one lock
 * file under {@code <appDataRoot>/.claims}. A claim holds the channel and lock open; releasing the
 * claim releases the lock and closes the channel.
 *
 * <p>Outcomes are classified exactly per {@link LockClassification}: an acquired-then-released lock
 * is {@code ACQUIRED_RELEASED}; a {@code tryLock} returning {@code null} is {@code LOCK_CONTENTION};
 * an {@link OverlappingFileLockException} is {@code SAME_JVM_CONTENTION}; an {@link IOException} from
 * the platform is {@code PLATFORM_IO_ERROR} with the operation, OS name, and message retained.
 */
public final class FileLockClaimStore {

    private final Path claimsRoot;

    public FileLockClaimStore(Path appDataRoot) throws IOException {
        this.claimsRoot = appDataRoot.resolve(StorePaths.CLAIMS_DIR);
        Files.createDirectories(claimsRoot);
    }

    /** An active claim: the open channel and the held lock. Call {@link #release()} to free it. */
    public static final class Claim implements AutoCloseable {
        private final FileChannel channel;
        private final FileLock lock;

        private Claim(FileChannel channel, FileLock lock) {
            this.channel = channel;
            this.lock = lock;
        }

        public void release() throws IOException {
            try {
                if (lock != null && lock.isValid()) {
                    lock.release();
                }
            } finally {
                channel.close();
            }
        }

        @Override
        public void close() throws IOException {
            release();
        }
    }

    /**
     * Attempts to acquire an exclusive lock for the item. Returns the {@link Claim} on success, or
     * {@code null} when the lock is already held by another process (the caller records
     * {@code LOCK_CONTENTION}). Throws {@link OverlappingFileLockException} for a same-JVM overlap
     * and {@link IOException} for a platform I/O error; the caller classifies those.
     */
    public Claim tryClaim(String item) throws IOException {
        Path lockPath = claimsRoot.resolve(lockFileName(item));
        FileChannel channel = FileChannel.open(
            lockPath, StandardOpenOption.CREATE, StandardOpenOption.WRITE);
        try {
            FileLock lock = channel.tryLock();
            if (lock == null) {
                channel.close();
                return null;
            }
            return new Claim(channel, lock);
        } catch (OverlappingFileLockException | IOException ex) {
            channel.close();
            throw ex;
        }
    }

    /** Classifies a platform I/O failure with operation, platform, and message context retained. */
    public static LockOutcome classifyPlatformIo(String operation, IOException ex) {
        String context = "operation=" + operation
            + ";platform=" + System.getProperty("os.name")
            + ";exception=" + ex.getClass().getName()
            + ";message=" + String.valueOf(ex.getMessage());
        return LockOutcome.platformIoError(ex.getClass().getName() + ": " + ex.getMessage(), context);
    }

    private static String lockFileName(String item) {
        // A stable, filesystem-safe name derived from the item; the item names in this slice are
        // already safe, so a direct suffix is sufficient and keeps the mapping human-readable.
        return item + ".lock";
    }
}
