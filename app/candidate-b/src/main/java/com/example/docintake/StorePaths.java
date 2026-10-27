package com.example.docintake;

/**
 * Path constants shared by the worker. The appdata root is the NFS mount; the inbox, the output
 * reports, and the claim files live under it. The control-plane record is kept OUTSIDE the appdata
 * root by {@link BackgroundWorkerOptions}.
 */
public final class StorePaths {

    /** Inbox directory under the appdata root. */
    public static final String INBOX_DIR = "seed/inbox";

    /** Claim files directory under the appdata root. */
    public static final String CLAIMS_DIR = ".claims";

    private StorePaths() {
    }

    /** Output report relative path for an item at a given stage: {@code out/<stage>/reports/<item>}. */
    public static String reportPath(String stage, String item) {
        return "out/" + stage + "/reports/" + item;
    }
}
