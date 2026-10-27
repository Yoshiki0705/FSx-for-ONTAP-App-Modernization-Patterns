package com.example.docintake;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.channels.OverlappingFileLockException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.stereotype.Component;

/**
 * The Candidate B background worker. On startup it scans the NFS inbox, claims each item with a Java
 * file lock, writes a report, releases the claim, and records each outcome to the control plane
 * (outside appdata). The run is bounded by {@code DOCINTAKE_MAX_ITEMS} so it terminates, which suits
 * a one-shot verification and a systemd oneshot or simple unit.
 *
 * <p>Lock outcomes are classified per the frozen matrix in {@link LockClassification}. This runner
 * is a Spring bean so the Spring lifecycle (startup, run, shutdown) is exercised.
 */
@Component
public class DocIntakeBackgroundRunner implements ApplicationRunner {

    private final BackgroundWorkerOptions options;
    private final FileLockClaimStore claims;
    private final ControlPlaneRecorder controlPlane;

    public DocIntakeBackgroundRunner() {
        this.options = BackgroundWorkerOptions.fromEnvironment();
        try {
            this.claims = new FileLockClaimStore(options.appDataRoot());
        } catch (IOException ex) {
            throw new UncheckedIOException("failed to initialize claim store", ex);
        }
        this.controlPlane = new ControlPlaneRecorder(options.appDataRoot(), options.controlPlanePath());
    }

    @Override
    public void run(ApplicationArguments args) {
        controlPlane.record("startup", null, null, "worker_start_requested", "spring_application_ready");
        try {
            List<String> items = listInbox();
            int processed = 0;
            for (String item : items) {
                if (processed >= options.maxItems()) {
                    break;
                }
                processItem(item);
                processed++;
            }
        } finally {
            controlPlane.record("normal-stop", null, null, "worker_loop_stopped", "run_complete");
        }
    }

    private void processItem(String item) {
        FileLockClaimStore.Claim claim;
        try {
            claim = claims.tryClaim(item);
        } catch (OverlappingFileLockException ex) {
            // Same-JVM overlap: an exact SAME_JVM_CONTENTION, not cross-process contention.
            LockOutcome outcome = LockOutcome.of(
                LockClassification.SAME_JVM_CONTENTION, ex.getClass().getName());
            controlPlane.record("claim-contention", item, outcome.classification().wireName(),
                "claim_not_acquired_same_jvm", outcome.rawOutcome());
            return;
        } catch (IOException ex) {
            // Platform I/O error: retain operation/platform/message with the raw exception.
            LockOutcome outcome = FileLockClaimStore.classifyPlatformIo("tryClaim", ex);
            controlPlane.record("claim-io-error", item, outcome.classification().wireName(),
                outcome.platformIoContext(), outcome.rawOutcome());
            return;
        }

        if (claim == null) {
            // tryLock returned null: LOCK_CONTENTION, never a silent retry.
            LockOutcome outcome = LockOutcome.of(LockClassification.LOCK_CONTENTION, "tryLock=null");
            controlPlane.record("claim-contention", item, outcome.classification().wireName(),
                "claim_retained_by_other_process", outcome.rawOutcome());
            return;
        }

        boolean reportWritten = false;
        try {
            writeReport(item);
            reportWritten = true;
        } catch (IOException ex) {
            LockOutcome outcome = FileLockClaimStore.classifyPlatformIo("writeReport", ex);
            controlPlane.record("process-io-error", item, outcome.classification().wireName(),
                outcome.platformIoContext(), outcome.rawOutcome());
        } finally {
            try {
                claim.release();
                LockOutcome outcome = LockOutcome.of(LockClassification.ACQUIRED_RELEASED, "FileLock acquired+released");
                controlPlane.record(reportWritten ? "item-succeeded" : "item-failed", item,
                    outcome.classification().wireName(),
                    reportWritten ? "report_written;claim_released" : "no_report;claim_released",
                    outcome.rawOutcome());
            } catch (IOException ex) {
                LockOutcome outcome = FileLockClaimStore.classifyPlatformIo("release", ex);
                controlPlane.record("claim-release-error", item, outcome.classification().wireName(),
                    outcome.platformIoContext(), outcome.rawOutcome());
            }
        }
    }

    private List<String> listInbox() {
        Path inbox = options.appDataRoot().resolve(StorePaths.INBOX_DIR);
        if (!Files.isDirectory(inbox)) {
            return List.of();
        }
        List<String> items = new ArrayList<>();
        try (Stream<Path> stream = Files.list(inbox)) {
            stream.filter(Files::isRegularFile)
                  .map(p -> p.getFileName().toString())
                  .sorted()
                  .forEach(items::add);
        } catch (IOException ex) {
            throw new UncheckedIOException("failed to list inbox", ex);
        }
        return items;
    }

    private void writeReport(String item) throws IOException {
        Path inboxItem = options.appDataRoot().resolve(StorePaths.INBOX_DIR).resolve(item);
        byte[] content = Files.readAllBytes(inboxItem);
        String text = new String(content, StandardCharsets.UTF_8);
        String firstLine = text.isEmpty() ? "" : text.split("\n", -1)[0];
        String summary = "length=" + text.length() + "\nfirst-line=" + firstLine + "\n";

        Path report = options.appDataRoot().resolve(StorePaths.reportPath(options.stage(), item + ".txt"));
        Path parent = report.getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        Files.writeString(report, summary, StandardCharsets.UTF_8);
    }
}
