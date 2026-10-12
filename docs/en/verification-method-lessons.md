# A measurement without a positive control is not a result

>
> Evidence tier: the facts in this document are `verified` where they rest on stage 0 to 3 runs and
> `documented` where they rest on AWS pricing information. They come from one sample application in this
> Spoke; no generalization to other environments or projects is made.

Four cases during the stage 0 to 3 measurements and write-up changed how a result was read.
Each section gives the facts (run id and date), then what a reader should do.

## A measurement without a positive control is invalid

The `case-sensitivity` records of stages 0 to 2 (run ids in the `s0`, `s1`, `s2`, `s2f`, and `s2g`
series, 2026-10-07 to 2026-10-11) cannot be used as conclusions. The Probe resolved the reference
`Docs\Index.json` against the volume root, but the real file was at `seed/docs/index.json`. A positive
control (`real`) that resolves the real file with the correct case was missing from every record.
As a result, Windows SMB, which does not distinguish case, was also recorded as `resolved: false`, and the
record could not tell "the path is case-sensitive" apart from "the file is simply not there".
In stage 1 (2026-10-07), `compare-probe.py` compared these control-less records with each other and
reported `ok`.
On 2026-10-12 the following changes were made.

- The reference was pinned under `seed/`
- `real` and `real_resolved` were added to the record
- `outcome` becomes `measured` only when the positive control resolves

The re-measurements are `s4c-20261012T034403Z` for SMB and NFS (Windows SMB, Linux cifs, Linux NFS) and
`s3b-20261012T022900Z` for S3 Access Points, on ONTAP 9.19.1P3, Single-AZ, one ntfs volume, once each.
The results are in [stage 1](stage-1-multiprotocol.md) and [stage 3](stage-3-s3-access-points.md).

## A passing gate is not evidence that it ran

A check that reports "0 findings" can mean either "nothing is wrong" or "nothing was detected".
This Spoke confirms that a check ran in three ways.

- Every Makefile target is declared `.PHONY`. A target named after an existing directory (`docs`,
  `templates`, `scripts`) is otherwise treated as up to date, prints "up to date", and runs nothing
- A missing tool fails the gate instead of skipping it. A skip reported as success cannot be told apart from a check that passed
- Each detector runs its own selftest before scanning. A detector whose rule stopped matching fails
  instead of returning an empty result as a pass

Details are in [quality gates](../agent/quality-gates.md).
In the AIMF session, the detectors for the migration checks (AP-1 to AP-13) were also fed temporary violating
files, and all were detected (pass 13, fail 0, 2026-10-12), before their "0 findings" was trusted.
The exit status of a pipeline (`make all | tail`) is the status of `tail`, so a gate's result is read with
`make all > /tmp/all.log 2>&1; echo $?`.

## A static page can miss a dynamic tab

The request pricing for S3 Access Points is on the English Amazon FSx for NetApp ONTAP pricing page,
in a script-rendered tab ("S3 Access Points for FSx"). <!-- allow:naming -->
A static HTML fetch does not contain the tab's content. The Japanese pricing page did not show this tab on 2026-10-12.
Reading only the Japanese page and concluding that S3 Access Points requests are not billed would be wrong.
The same unit prices were confirmed on 2026-10-12 in the AWS Price List API under service code `AmazonS3`
(`APN1-Requests-FSXONTAP-Tier1` and `Tier2`, Asia Pacific (Tokyo)). The background and the unit prices are in the
estimate section of [stage 3](stage-3-s3-access-points.md).

## A label the tool prints is not an observation

The Probe's standalone output and the JSON from a single Lambda invocation for S3 print `"topology": "cross-host"`
even when no two machines were coordinated. The label is a fixed value and does not describe an observation.
A pair counts as two coordinated machines only when `probe_merge.py` confirmed a matching sync ID and
overlapping times from both records, with an empty `reasons`, and classified it `cross-host`.
For the same reason, the `cross-host` claim in the 2026-10-07 stage 0 records was withdrawn (see the withdrawn
claims section of [stage 0](stage-0-smb-ntfs.md)). The Probe output format is in the [sample app](sample-app.md).

## What to do

- Build a positive control into the measurement, so that "the target is absent" and "the target exists but does not resolve" are distinguishable in the same record
- Use a comparison tool's verdict only when both records are valid. An `ok` between invalid records is not evidence of agreement
- Trust a detector or gate only after confirming that it fails on an input it must reject
- Check a dynamically rendered page through more than one route, such as a page in another language or a pricing API
- Decide a label or classification in the same place as the evidence that justifies it, and leave the label off when there is no evidence
