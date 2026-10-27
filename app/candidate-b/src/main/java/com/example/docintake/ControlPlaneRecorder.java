package com.example.docintake;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Instant;

/**
 * Appends one JSON line per event to a control-plane file kept OUTSIDE the appdata NFS root, so the
 * postcondition record survives an NFS-side cleanup. The constructor re-checks the outside-appdata
 * invariant; a path inside appdata is rejected.
 */
public final class ControlPlaneRecorder {

    private final Path recordPath;

    public ControlPlaneRecorder(Path appDataRoot, Path recordPath) {
        Path root = appDataRoot.toAbsolutePath().normalize();
        Path record = recordPath.toAbsolutePath().normalize();
        if (record.equals(root) || record.startsWith(root)) {
            throw new IllegalArgumentException("control-plane path must be outside appdata: " + record);
        }
        this.recordPath = record;
    }

    public Path recordPath() {
        return recordPath;
    }

    /** Records one event as a JSON object line. Fields are escaped minimally for JSON safety. */
    public synchronized void record(String event, String item, String classification,
                                    String postcondition, String detail) {
        String line = "{"
            + "\"event\":" + json(event)
            + ",\"occurredAt\":" + json(Instant.now().toString())
            + ",\"item\":" + json(item)
            + ",\"classification\":" + json(classification)
            + ",\"postcondition\":" + json(postcondition)
            + ",\"detail\":" + json(detail)
            + "}" + System.lineSeparator();
        try {
            Path parent = recordPath.getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            Files.writeString(recordPath, line, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException ex) {
            throw new UncheckedIOException("failed to append control-plane record", ex);
        }
    }

    private static String json(String value) {
        if (value == null) {
            return "null";
        }
        StringBuilder sb = new StringBuilder(value.length() + 2);
        sb.append('"');
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
        return sb.toString();
    }
}
