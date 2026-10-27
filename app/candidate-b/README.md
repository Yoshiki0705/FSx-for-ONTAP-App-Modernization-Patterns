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
| `.../ControlPlaneRecorder.java` | JSONL control-plane records outside the appdata root |
| `src/test/java/...LockClassificationTest.java` | Tier 1 (local/unit) lock-outcome tests |

## Lock-outcome mapping (frozen)

| Raw outcome | Classification |
|---|---|
| `FileLock` acquired and released | `acquired_released` (positive) |
| `tryLock` returns `null` | `lock_contention` (negative) |
| `OverlappingFileLockException` | `same_jvm_contention` (negative) |
| Platform I/O error | `platform_io_error` with operation/platform/message and the raw exception (negative) |

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

## Build and test

Requires a JDK 21 (Amazon Corretto 21) and Maven.

```bash
cd app/candidate-b
mvn clean test       # Tier 1 local/unit tests
mvn -DskipTests package
java -jar target/candidate-b-java-spring-background-nfs-1.0.0.jar
```

Evidence tier: Tier 1 (local/unit) establishes local algorithmic behavior only. NFS, cross-host,
Spring-lifecycle-on-host, and ONTAP behavior are `open`/`not-measured` until a candidate-specific
real-environment run (Tier 2/3) is recorded in the private Candidate B ledger.
