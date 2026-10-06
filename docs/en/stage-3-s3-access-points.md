# Stage 3: partial serverless via S3 Access Points

> Evidence tier: `hypothesis` (not measured). The steps are a plan and the predictions are
> pre-measurement hypotheses, replaced by records.

Attach FSx for ONTAP S3 Access Points to the target volume and run part of the Worker from AWS
Lambda. The volume is not cloned and the data is not moved.

## Planned steps

1. Confirm the Lambda subnet's route tables carry the S3 prefix list
2. Present an estimate and obtain approval (including the `NetworkOrigin` and file-system identity)
3. Create the add-on stack (including the S3 Access Point attachment)
4. Confirm connectivity with a data operation such as `GetObject` (`HeadBucket` is not evidence)
5. Take the inventory and the boundary record (`b3`), the Probe (S3 variant), and the comparison

## What to confirm at the boundary

- The target volume UUID and the `seed/` inventory are identical to stage 0
- The security style is still `ntfs`
- The S3 Access Point target is `appdata`

## Sources for the claims

S3 Access Point constraints are cited per claim from Hub notes (contents are not copied).

- S3 Access Point constraints (unsupported APIs, single-identity authorization, ACLs not inherited, no event notification): [s3-access-point-constraints](https://github.com/Yoshiki0705/fsxn-adoption-playbook/blob/main/docs/ja/domains/data-utilization/notes/s3-access-point-constraints.md)
- The two-layer authorization: [access-point-authorization-layers](https://github.com/Yoshiki0705/fsxn-adoption-playbook/blob/main/docs/ja/domains/security-governance/notes/access-point-authorization-layers.md)

## Predictions (unverified)

- S3 Access Points are expected to attach on this configuration (`SINGLE_AZ_1`, AD-joined SVM, NTFS volume) (unconfirmed).
- Because S3 Access Points emit no event notification, the trigger is a scheduled poll.
