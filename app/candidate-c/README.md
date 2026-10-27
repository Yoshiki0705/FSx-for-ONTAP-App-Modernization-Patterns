# app/candidate-c: Python Lambda worker/probe over FSx for ONTAP S3 Access Points

Candidate C (`candidate_id: candidate-c-python-lambda-fsx-ontap-s3-access-points`, GitHub Issue 47)
is a managed Python Lambda that reads and writes file data through
Amazon FSx for NetApp ONTAP S3 Access Points; thereafter FSx for ONTAP S3 Access Points.
It is independent of the .NET track and of the Python ONTAP REST candidate (Issue 48): no evidence
is reused across candidates.

First slice: the closed S3 object-operation set `GetObject` / `PutObject` / `ListObjectsV2`, a
policy-condition denial (negative), and a local invalid-parser-input classification (negative). No
image transform, no EventBridge, no NFS path.

## Pinned versions (source + capture date 2026-10-26)

| Input | Pin | Source |
|---|---|---|
| Lambda runtime | Python 3.14 | AWS Lambda managed runtime (current GA; 3.15 is public preview, not selected). |
| boto3 (dev/test) | 1.43.119 | PyPI; provided by the Lambda runtime at deploy time, pinned here for local tests. |
| moto (dev/test) | 5.2.4 | PyPI; fakes S3 for Tier 1 local tests. |
| pytest (dev/test) | 9.1.1 | PyPI. |

The exact ONTAP version, the S3 Access Point identity, and the artifacts-bucket identity are
recorded per verification run in the private Candidate C ledger.

## Execution modes (frozen)

| Mode | Meaning |
|---|---|
| `managed_lambda_decoded_event` | The managed path: a decoded event hands one S3 operation to the handler, which calls the FSx for ONTAP S3 Access Point and writes a witness record to a separate ordinary S3 artifacts bucket. The only mode that produces managed-Lambda evidence. |
| `parser_unit_byte_ingress` | Local parser/unit: classifies raw bytes with no managed identity and no S3 Access Point call. Never managed evidence. |

## Verified architecture choice (from Hub / sibling-Spoke knowledge)

- **Internet-origin access point** (no `VpcConfiguration`), a **VPC-external Lambda** (no `VpcConfig`),
  and **no access-point resource policy** (same-account IAM identity policy is sufficient). This is
  the pattern verified in the sibling S3-Access-Points serverless work.
- **AD-non-joined SVM** (UNIX security style): avoids the AD-DC-reachability requirement that applies
  to every S3 Access Point data operation on an AD-joined SVM (where `HeadBucket` is a false-positive
  health check). FSx for ONTAP auto-manages the `s3_unix` name-mapping; no manual mapping is needed.
- Supported object operations are documented: `GetObject`, `PutObject`, `ListObjectsV2`. S3 event
  notifications are not supported, so the managed path starts from a decoded event, not an S3 event.

## Layout

| Path | Contents |
|---|---|
| `src/docintake_c/handler.py` | Lambda entry; managed and parser modes; closed operation set |
| `tests/test_handler.py` | Tier 1 local tests (moto fakes S3) |
| `requirements-dev.txt` | pinned test dependencies |

## Build and test

```bash
cd app/candidate-c
python3 -m venv .venv-c && ./.venv-c/bin/pip install -r requirements-dev.txt
PYTHONPATH=src ./.venv-c/bin/pytest tests/ -q
```

Package for Lambda (framework-dependent; boto3 is in the runtime):

```bash
mkdir -p build && cp -r src/docintake_c build/
(cd build && zip -qr ../candidate-c.zip docintake_c)
```

Handler: `docintake_c.handler.handle`. Event (managed): `{"mode":"managed_lambda_decoded_event",
"op":"get|put|list","access_point":"<ap-arn-or-alias>","key":"...","content":"...","prefix":"..."}`.
Environment: `ARTIFACTS_BUCKET` (witness), optional `S3_ACCESS_POINT` default.

Evidence tier: Tier 1 (local/unit with a fake S3) establishes the handler's dispatch and output
shape only. Real FSx for ONTAP S3 Access Point behavior, policy-denial error classes, witness
separation under real identities, and any zero-call claim are `open`/`not-measured` until a
candidate-specific real-environment run (Tier 2/3) is recorded in the private Candidate C ledger.
The `c-zero-call-claim` case stays `open/not-measured` by design unless both CloudTrail scopes and
the correlation controls are present.
