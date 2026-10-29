# app/candidate-b: Java/Spring background worker over NFS

Candidate B (`candidate_id: candidate-b-java-spring-background-nfs`, GitHub Issue #46) is a
Java/Spring background worker that processes an NFS-mounted inbox backed by
Amazon FSx for NetApp ONTAP; thereafter FSx for ONTAP. It is independent of the .NET track
(`app/modern`): no .NET evidence is reused as Candidate B evidence.

First slice: non-Web background processing only. No AWS SDK, no FSx for ONTAP S3 Access Points, no
HTTP/UI/ECS/EKS/container path.

## Pinned versions (source + capture date 2026-10-26)

| Input | Pin | Source |
|---|---|---|
| Spring Boot | 4.1.2 | Maven Central `maven-metadata.xml` for `spring-boot-starter` (4.2.0 is RC only). Spring Boot 3.x reached OSS end of life in June 2026, so a 3.x line is not selected for new work. |
| Java release | 21 | Amazon Corretto 21 (LTS). Spring Boot 4.x requires Java 17+, 21 recommended. |
| Build tool | Apache Maven | `pom.xml` with the Spring Boot parent; dependency versions resolved by the parent BOM. |

The exact ONTAP version and the NFS topology are recorded per verification run in the private
Candidate B ledger, not here.

## Layout

| Path | Contents |
|---|---|
| `pom.xml` | Maven build, Spring Boot 4.1.2 parent, Java 21 target |
| `src/main/java/com/example/docintake/DocIntakeBApplication.java` | Spring Boot entry (no web server) |
| `.../BackgroundWorkerOptions.java` | `DOCINTAKE_*` env configuration; control-plane must be outside appdata |
| `.../DocIntakeBackgroundRunner.java` | `ApplicationRunner`: scan inbox, claim, write report, release, record |
| `.../FileLockClaimStore.java` | `java.nio.channels.FileLock` exclusive claim and platform-I/O classification |
| `.../LockClassification.java`, `LockOutcome.java` | the frozen lock-outcome mapping |
| `.../ClaimWaiter.java` | bounded, explicit wait for a claim held by another process; reports attempts and time waited |
| `.../ControlPlaneRecorder.java` | JSONL control-plane records outside the appdata root |
| `src/test/java/...LockClassificationTest.java`, `ClaimWaiterTest.java` | Tier 1 (local/unit) lock-outcome and wait tests; `ClaimWaiterTest` also uses a second JVM (`LockHolder`) on a local directory |

## Lock-outcome mapping (frozen)

| Raw outcome | Classification |
|---|---|
| `FileLock` acquired and released | `acquired_released` (positive) |
| `tryLock` returns `null` | `lock_contention` (negative) |
| `OverlappingFileLockException` | `same_jvm_contention` (negative) |
| Platform I/O error | `platform_io_error` with operation/platform/message and the raw exception (negative) |

**Wait and retry.** The mapping above is unchanged. With `DOCINTAKE_CLAIM_WAIT_MS` greater than 0 the
worker retries only a `null` `tryLock`, up to that limit. If the limit passes, the outcome is still
`lock_contention`, and the control-plane record carries `attempts`, `waited_ms` and `wait_limit_ms`.
A claim taken after a retry is recorded as `claim-acquired-after-wait`. `same_jvm_contention` and
`platform_io_error` are never retried. Tier 1 covers the logic and a two-JVM case on a local
directory.

Measured once on two hosts over NFS v4.1 (ap-northeast-1, ONTAP 9.18.1P6, Amazon Linux 2023 arm64,
Corretto 21; lease 30 s, grace 60 s on that SVM): a waiter took the claim 198 ms after the holder
released it (retry interval 250 ms); a 10 s limit ended as `lock_contention` after 41 attempts; with no
wait the outcome was `lock_contention` after one attempt; after `kill -9` of the holder JVM the
waiter took the claim 162 ms later. After a forced stop of the holder's host the waiter took it about
75 s after the stop request, a single observation that does not separate stop latency from lease
expiry. This is not a statement about other ONTAP versions, Multi-AZ, NFS v3 or more than two hosts.
The record is in the private ledger.

**Java file-lock note (verified Tier 1):** a `null` result (`lock_contention`) is inherently a
cross-process outcome. Within a single JVM the platform raises `OverlappingFileLockException`
(`same_jvm_contention`) instead of returning `null`, so the cross-process `lock_contention` case is
a two-process NFS runtime case, not a single-JVM unit case.

## Environment variables

| Variable | Meaning |
|---|---|
| `DOCINTAKE_ROOT` | NFS mount root (inbox, reports, and `.claims` live under it) |
| `DOCINTAKE_CONTROL_PLANE_PATH` | JSONL control-plane file; must be OUTSIDE `DOCINTAKE_ROOT` |
| `DOCINTAKE_STAGE` | stage name for `out/<stage>/reports/` |
| `DOCINTAKE_OWNER_ID` | owner id recorded with claims |
| `DOCINTAKE_MAX_ITEMS` | bounded number of items to process in one run (>= 1) |
| `DOCINTAKE_CLAIM_WAIT_MS` | optional, default 0. How long to keep retrying a claim held by another process. 0 keeps the single attempt |
| `DOCINTAKE_CLAIM_RETRY_INTERVAL_MS` | optional, default 200 (>= 1). Pause between attempts |

## Build and test

Requires a JDK 21 (Amazon Corretto 21) and Maven.

```bash
cd app/candidate-b
mvn clean test       # Tier 1 local/unit tests
mvn -DskipTests package
java -jar target/candidate-b-java-spring-background-nfs-1.0.0.jar
```

Evidence tier: Tier 1 (local/unit) establishes local algorithmic behavior only. The
candidate-specific real-environment run (Tier 2/3) is complete and `verified` for this candidate on
its recorded environment only (ap-northeast-1, ONTAP 9.18.1, NFS v4.1, Amazon Linux 2023 arm64,
Amazon Corretto 21, Spring Boot 4.1.2): 5 cases PASS, 1 SKIPPED (wait/retry, out of the first
slice). The cross-process `lock_contention` (a `null` FileLock) was confirmed with two JVMs on the
NFS mount. Not a general compatibility statement and no .NET evidence reused. Per-run identifiers
and raw artifacts are in the private Candidate B ledger, not here.
