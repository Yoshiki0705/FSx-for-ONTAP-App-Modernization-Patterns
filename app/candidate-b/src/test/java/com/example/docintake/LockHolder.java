package com.example.docintake;

import java.nio.file.Path;

/** Test helper run in a separate JVM: holds the claim for an item, then exits. */
public final class LockHolder {
    private LockHolder() {}

    public static void main(String[] args) throws Exception {
        Path root = Path.of(args[0]);
        long holdMillis = Long.parseLong(args[2]);
        FileLockClaimStore.Claim claim = new FileLockClaimStore(root).tryClaim(args[1]);
        if (claim == null) {
            System.out.println("NOT_LOCKED");
            System.out.flush();
            return;
        }
        System.out.println("LOCKED");
        System.out.flush();
        Thread.sleep(holdMillis);
        claim.release();
    }
}
