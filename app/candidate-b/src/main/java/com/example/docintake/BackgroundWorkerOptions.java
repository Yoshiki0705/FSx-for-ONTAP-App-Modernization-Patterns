package com.example.docintake;

import java.nio.file.Path;
import java.nio.file.Paths;

/**
 * Environment-driven configuration. Every value is required; a missing or malformed value makes
 * {@link #fromEnvironment()} throw before any work begins. Mirrors the Candidate A env contract so
 * the two candidates are configured the same way.
 */
public final class BackgroundWorkerOptions {

    private final Path appDataRoot;
    private final Path controlPlanePath;
    private final String stage;
    private final String ownerId;
    private final int maxItems;

    public BackgroundWorkerOptions(Path appDataRoot, Path controlPlanePath, String stage,
                                   String ownerId, int maxItems) {
        this.appDataRoot = appDataRoot;
        this.controlPlanePath = controlPlanePath;
        this.stage = stage;
        this.ownerId = ownerId;
        this.maxItems = maxItems;
    }

    public Path appDataRoot() {
        return appDataRoot;
    }

    public Path controlPlanePath() {
        return controlPlanePath;
    }

    public String stage() {
        return stage;
    }

    public String ownerId() {
        return ownerId;
    }

    public int maxItems() {
        return maxItems;
    }

    /**
     * Builds options from DOCINTAKE_* environment variables and validates them. The control-plane
     * path must be outside the appdata root, so a cleanup of the NFS side cannot erase the
     * postcondition records.
     */
    public static BackgroundWorkerOptions fromEnvironment() {
        Path root = Paths.get(required("DOCINTAKE_ROOT")).toAbsolutePath().normalize();
        Path controlPlane = Paths.get(required("DOCINTAKE_CONTROL_PLANE_PATH")).toAbsolutePath().normalize();
        String stage = required("DOCINTAKE_STAGE");
        String ownerId = required("DOCINTAKE_OWNER_ID");
        int maxItems = requiredInt("DOCINTAKE_MAX_ITEMS");

        if (controlPlane.equals(root) || controlPlane.startsWith(root)) {
            throw new IllegalStateException(
                "DOCINTAKE_CONTROL_PLANE_PATH must be outside DOCINTAKE_ROOT: " + controlPlane);
        }
        if (maxItems < 1) {
            throw new IllegalStateException("DOCINTAKE_MAX_ITEMS must be >= 1");
        }
        return new BackgroundWorkerOptions(root, controlPlane, stage, ownerId, maxItems);
    }

    private static String required(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException(name + " is required");
        }
        return value;
    }

    private static int requiredInt(String name) {
        try {
            return Integer.parseInt(required(name));
        } catch (NumberFormatException ex) {
            throw new IllegalStateException(name + " must be an integer", ex);
        }
    }
}
