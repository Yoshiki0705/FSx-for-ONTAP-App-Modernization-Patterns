using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using Amazon.S3;
using Amazon.S3.Model;

namespace DocIntake.Core
{
    // S3 implementation of IFileStore for FSx for ONTAP S3 Access Points (ADR-1). The Lambda handler
    // and its deployment are out of AIMF scope; this is the IFileStore.S3 implementation only.
    //
    // S3 Access Points are NOT an S3 bucket equivalent. This store uses S3 OBJECT operations only:
    //   - List   → ListObjectsV2
    //   - Read   → GetObject
    //   - Write  → PutObject
    //   - CanWrite → a minimal PutObject + DeleteObject trial (no cheap pre-check exists)
    //   - OpenExclusive → NotSupportedException (S3 has no exclusive-open; file-locking is not an
    //     S3 concept — the Probe skips file-locking on S3)
    //
    // Deliberately avoids (adaptation-ledger AP-10..13):
    //   MP-27: no bucket-layer APIs (PutBucket*, GetBucketLocation, ListBuckets, lifecycle,
    //          versioning). Only object operations against the access-point/bucket name given.
    //   MP-28: authorization is the single access-point identity (FileSystemIdentity); no per-caller
    //          object ACLs and no HeadBucket as a health signal (HeadBucket succeeds even when the
    //          AD-joined SVM cannot serve data, a false positive). Health is a real data op.
    //   MP-29: does not rely on source NTFS ACLs carrying over to S3 authorization.
    //   MP-30: does not depend on S3 event notifications (no ObjectCreated trigger assumption).
    //
    // The relative paths used by the Worker/Probe ("seed/inbox/x", "out/s0/index.json") are S3 keys
    // as-is (forward slash, case-sensitive — matches the s3b measurement). An optional key prefix is
    // prepended.
    public sealed class S3FileStore : IFileStore
    {
        private readonly IAmazonS3 _s3;
        private readonly string _bucket;   // access point alias / ARN or bucket name
        private readonly string _prefix;   // optional key prefix (no leading slash)

        public S3FileStore(IAmazonS3 s3, string bucket, string prefix)
        {
            _s3 = s3 ?? throw new ArgumentNullException("s3");
            _bucket = bucket ?? throw new ArgumentNullException("bucket");
            _prefix = string.IsNullOrEmpty(prefix) ? "" : (prefix.EndsWith("/") ? prefix : prefix + "/");
        }

        private string Key(string relativePath)
        {
            // S3 keys are '/'-separated and a '\' is an ordinary character inside a key. It is sent
            // verbatim: the Probe's path-separator behavior depends on it, and rewriting it would
            // record a key that was never written. Keys are case-sensitive (s3b).
            return _prefix + relativePath;
        }

        public IEnumerable<string> List(string relativeDir)
        {
            // ListObjectsV2 under the directory prefix; return immediate file names (not full keys).
            var dirKey = Key(relativeDir);
            if (!dirKey.EndsWith("/"))
            {
                dirKey += "/";
            }
            var results = new List<string>();
            var request = new ListObjectsV2Request
            {
                BucketName = _bucket,
                Prefix = dirKey,
                Delimiter = "/"
            };
            while (true)
            {
                var response = _s3.ListObjectsV2Async(request).GetAwaiter().GetResult();
                if (response.S3Objects != null)
                {
                    foreach (var obj in response.S3Objects)
                    {
                        var name = obj.Key.Substring(dirKey.Length);
                        if (name.Length > 0)
                        {
                            results.Add(name);
                        }
                    }
                }
                if (response.IsTruncated == true)
                {
                    request.ContinuationToken = response.NextContinuationToken;
                }
                else
                {
                    break;
                }
            }
            return results;
        }

        public byte[] Read(string relativePath)
        {
            var request = new GetObjectRequest { BucketName = _bucket, Key = Key(relativePath) };
            try
            {
                using (var response = _s3.GetObjectAsync(request).GetAwaiter().GetResult())
                using (var ms = new MemoryStream())
                {
                    response.ResponseStream.CopyTo(ms);
                    return ms.ToArray();
                }
            }
            catch (AmazonS3Exception ex) when (ex.StatusCode == HttpStatusCode.NotFound)
            {
                throw new FileNotFoundException("S3 key not found: " + request.Key, request.Key, ex);
            }
            catch (AmazonS3Exception ex) when (ex.StatusCode == HttpStatusCode.Forbidden)
            {
                // Same contract as the filesystem stores: a denied access is UnauthorizedAccessException.
                throw new UnauthorizedAccessException("S3 access denied: " + request.Key, ex);
            }
        }

        public void Write(string relativePath, byte[] content)
        {
            var request = new PutObjectRequest
            {
                BucketName = _bucket,
                Key = Key(relativePath),
                InputStream = new MemoryStream(content ?? new byte[0])
            };
            try
            {
                _s3.PutObjectAsync(request).GetAwaiter().GetResult();
            }
            catch (AmazonS3Exception ex) when (ex.StatusCode == HttpStatusCode.Forbidden)
            {
                // Same contract as the filesystem stores; the Probe reads it as "write not allowed".
                throw new UnauthorizedAccessException("S3 access denied: " + request.Key, ex);
            }
        }

        // S3 has no exclusive-open. file-locking is not an S3 concept; the Probe records it skipped.
        public Stream OpenExclusive(string relativePath)
        {
            throw new NotSupportedException("OpenExclusive is not supported on S3 Access Points "
                + "(no exclusive-open semantics; file-locking is not an S3 concept)");
        }

        // No cheap pre-check for S3 write authorization (MP-28: HeadBucket is a false positive on an
        // AD-joined SVM that cannot serve data). Do a real minimal object trial, mirroring the Linux
        // real-I/O approach of ADR-3. AccessDenied → false; success → true (probe object deleted).
        public bool CanWrite(string relativePath)
        {
            var probeKey = Key(relativePath) + ".docintake-canwrite-" + Guid.NewGuid().ToString("N");
            try
            {
                _s3.PutObjectAsync(new PutObjectRequest
                {
                    BucketName = _bucket,
                    Key = probeKey,
                    InputStream = new MemoryStream(new byte[0])
                }).GetAwaiter().GetResult();
                try
                {
                    _s3.DeleteObjectAsync(new DeleteObjectRequest { BucketName = _bucket, Key = probeKey })
                        .GetAwaiter().GetResult();
                }
                catch (AmazonS3Exception)
                {
                }
                return true;
            }
            catch (AmazonS3Exception ex) when (ex.StatusCode == HttpStatusCode.Forbidden)
            {
                // AccessDenied: on an AD-joined SVM this can mean the SVM cannot reach its AD DCs,
                // not an IAM/policy problem (MP-28). Report not-writable.
                return false;
            }
        }
    }
}
