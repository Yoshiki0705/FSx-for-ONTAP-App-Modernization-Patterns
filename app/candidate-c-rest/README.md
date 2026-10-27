# app/candidate-c-rest: Python Lambda over the ONTAP REST API

Candidate C-REST (`candidate_id: candidate-c-rest-python-ontap-rest-lambda`, GitHub Issue 48) is a
managed Python Lambda that reads and serves file data from
Amazon FSx for NetApp ONTAP by calling the ONTAP REST API over HTTPS; thereafter FSx for ONTAP.

**Distinct from Candidate C (Issue 47) — do not merge.** Candidate C reaches data through
FSx for ONTAP S3 Access Points (S3 object API, AWS SigV4/IAM authorization). This candidate reaches
data through the ONTAP REST API directly (ONTAP credential; ONTAP, not IAM, authorizes the
operation). Different endpoint, authentication, network boundary, IAM/secrets boundary, and API
contract. Separate ledger namespace `evidence-ledger-v1/c-rest/...` and run-ID prefix
`run-c-rest-python-ontap-rest-lambda-`.

First slice: a read on the documented files resource `GET /storage/volumes/{volume.uuid}/files/
{path}` (with `Accept: multipart/form-data` and a `length` query property). No write/mutation, no
oversize multi-call strategy, no S3 Access Points, no NFS mount.

## Pinned versions (source + capture date 2026-10-26)

| Input | Pin | Source |
|---|---|---|
| Lambda runtime | Python 3.14 | AWS Lambda managed runtime (current GA; 3.15 is public preview, not selected). |
| boto3 (dev) | 1.43.119 | PyPI; provided by the runtime; pinned here for local imports. Used only for Secrets Manager and the S3 witness. |
| pytest (dev) | 9.1.1 | PyPI. |

The handler otherwise uses only the standard library (`urllib`, `ssl`, `base64`, `json`,
`hashlib`). The exact ONTAP version, endpoint boundary, and authentication path are recorded per
verification run in the private ledger.

## Network and authorization boundary (verified architecture choice)

The ONTAP management/data LIFs are VPC-private, so the Lambda runs **inside the VPC** (`VpcConfig`)
and its ENI reaches the ONTAP LIF. The maruo VPC already has an S3 gateway endpoint and a Secrets
Manager interface endpoint, so the in-VPC Lambda reaches the witness bucket and the ONTAP credential
secret without a new endpoint. Authentication to ONTAP is a basic credential held in Secrets
Manager; **ONTAP authorizes the operation, not AWS IAM** — a `403` is an ONTAP authorization denial,
not an IAM denial.

TLS: the handler does not disable certificate validation by default. `ONTAP_CA_BUNDLE` supplies a
trusted CA; without it, verification uses the default trust store (an FSx for ONTAP management
endpoint's self-signed certificate then fails verification, recorded as `ontap_tls_untrusted`, not
silently bypassed). `ONTAP_TLS_INSECURE=1` is an explicit, recorded operator choice for the
diagnostic path only.

## Negative classifications (frozen)

| Case | Trigger | Class | Mapping |
|---|---|---|---|
| auth failure | rejected credential | `ontap_rest_auth_failed` | HTTP 401 |
| not authorized | authenticated, unpermitted | `ontap_rest_authorization_denied` | HTTP 403, ONTAP code 6 |
| unreachable | ENI cannot reach the LIF | `ontap_endpoint_unreachable` | timeout / no route (no HTTP status) |
| invalid identifier | bad `volume.uuid` / `path` | `ontap_rest_invalid_identifier` | HTTP 404/405 (code 4) or 400 (code 2) |

The 1 MB single-call read limit is a documented ONTAP REST API-contract limit, not a sample
property and not a general service limit; serving above it (multi-call) is a skipped case.

## Layout

| Path | Contents |
|---|---|
| `src/docintake_crest/handler.py` | Lambda entry; managed ONTAP REST mode and local parser mode |
| `tests/test_handler.py` | Tier 1 local tests (classification + parser; no real ONTAP) |
| `requirements-dev.txt` | pinned test dependencies |

## Build and test

```bash
cd app/candidate-c-rest
python3 -m venv .venv-crest && ./.venv-crest/bin/pip install -r requirements-dev.txt
PYTHONPATH=src ./.venv-crest/bin/pytest tests/ -q
```

Handler: `docintake_crest.handler.handle`. Environment: `ONTAP_HOST` (e.g. `https://<mgmt-ip>`),
`ONTAP_SECRET_ID`, `ARTIFACTS_BUCKET`, optional `ONTAP_CA_BUNDLE` / `ONTAP_TLS_INSECURE`.
Managed event: `{"mode":"managed_lambda_ontap_rest","op":"read","volume_uuid":"...","path":"...",
"length":1024,"scenario_id":"..."}`.

Evidence tier: Tier 1 (local/unit) establishes the classification and parser logic only. The
candidate-specific real-environment run (Tier 2/3) is complete for this candidate on its recorded
environment only (ap-northeast-1, ONTAP 9.18.1, the cluster management LIF REST endpoint,
python3.14 Lambda in-VPC): the read and the four negatives (401 auth failure, 403 not authorized,
timeout unreachable, 404 invalid identifier) are `verified`; two cases are SKIPPED (write mutation,
oversize single call); one is `open/not-measured`. The `serve` case is `attested` — one tier below
`verified`: the read leg is verified and the witness write is attested from source, but the
independent witness read-back was not exported before teardown, a provenance gap (see the private
ledger's run envelope). The run also found and fixed two handler defects (TLS-trust classification
via unwrapping the SSLError from URLError.reason; a listing uses `Accept: application/json` because
ONTAP REST answers 406 to `multipart/form-data` for a listing). Not a general compatibility
statement and no .NET evidence reused. `crest-zero-call-claim` stays `open/not-measured` by design
unless both AWS-side and ONTAP-side audit scopes and the correlation controls are present. Per-run
identifiers and raw artifacts are in the private Candidate C-REST ledger, not here.
