using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Amazon.S3;
using Amazon.S3.Model;
using Moq;

namespace DocIntake.Core.Tests
{
    // In-memory S3 that keeps objects per bucket (the access point and the artifacts bucket are
    // different namespaces) and records every call, so a test can assert "no S3 call happened" and
    // "no delete happened". Strict mock: a method the code under test is not expected to use fails.
    // No network, no credentials.
    internal sealed class BucketFakeS3
    {
        private readonly Dictionary<string, byte[]> _objects = new Dictionary<string, byte[]>(StringComparer.Ordinal);

        public BucketFakeS3()
        {
            Calls = new List<string>();
            Mock = new Mock<IAmazonS3>(MockBehavior.Strict);

            Mock.Setup(s => s.PutObjectAsync(It.IsAny<PutObjectRequest>(), It.IsAny<CancellationToken>()))
                .Returns((PutObjectRequest r, CancellationToken ct) =>
                {
                    Calls.Add("put " + r.BucketName + " " + r.Key);
                    if (FailPut != null && FailPut(r.Key))
                    {
                        throw new AmazonS3Exception("ServiceUnavailable") { StatusCode = HttpStatusCode.ServiceUnavailable };
                    }
                    byte[] bytes;
                    if (r.InputStream != null)
                    {
                        using (var ms = new MemoryStream())
                        {
                            r.InputStream.CopyTo(ms);
                            bytes = ms.ToArray();
                        }
                    }
                    else
                    {
                        bytes = Encoding.UTF8.GetBytes(r.ContentBody ?? "");
                    }
                    _objects[r.BucketName + "|" + r.Key] = bytes;
                    return Task.FromResult(new PutObjectResponse());
                });

            Mock.Setup(s => s.DeleteObjectAsync(It.IsAny<DeleteObjectRequest>(), It.IsAny<CancellationToken>()))
                .Returns((DeleteObjectRequest r, CancellationToken ct) =>
                {
                    Calls.Add("delete " + r.BucketName + " " + r.Key);
                    _objects.Remove(r.BucketName + "|" + r.Key);
                    return Task.FromResult(new DeleteObjectResponse());
                });

            Mock.Setup(s => s.GetObjectAsync(It.IsAny<GetObjectRequest>(), It.IsAny<CancellationToken>()))
                .Returns((GetObjectRequest r, CancellationToken ct) =>
                {
                    Calls.Add("get " + r.BucketName + " " + r.Key);
                    byte[] bytes;
                    if (!_objects.TryGetValue(r.BucketName + "|" + r.Key, out bytes))
                    {
                        throw new AmazonS3Exception("NoSuchKey") { StatusCode = HttpStatusCode.NotFound };
                    }
                    return Task.FromResult(new GetObjectResponse
                    {
                        BucketName = r.BucketName,
                        Key = r.Key,
                        ResponseStream = new MemoryStream(bytes)
                    });
                });

            Mock.Setup(s => s.ListObjectsV2Async(It.IsAny<ListObjectsV2Request>(), It.IsAny<CancellationToken>()))
                .Returns((ListObjectsV2Request r, CancellationToken ct) =>
                {
                    Calls.Add("list " + r.BucketName + " " + (r.Prefix ?? ""));
                    var prefix = r.Prefix ?? "";
                    var objs = new List<S3Object>();
                    foreach (var kv in _objects)
                    {
                        var bar = kv.Key.IndexOf('|');
                        if (kv.Key.Substring(0, bar) != r.BucketName)
                        {
                            continue;
                        }
                        var key = kv.Key.Substring(bar + 1);
                        if (!key.StartsWith(prefix, StringComparison.Ordinal))
                        {
                            continue;
                        }
                        if (r.Delimiter == "/" && key.Substring(prefix.Length).Contains("/"))
                        {
                            continue;
                        }
                        objs.Add(new S3Object { Key = key });
                    }
                    return Task.FromResult(new ListObjectsV2Response { S3Objects = objs, IsTruncated = false });
                });
        }

        public Mock<IAmazonS3> Mock { get; private set; }

        public IAmazonS3 Client { get { return Mock.Object; } }

        public List<string> Calls { get; private set; }

        // When set and it returns true for a key, PutObject fails with a 503.
        public Func<string, bool> FailPut { get; set; }

        public void Seed(string bucket, string key, string text)
        {
            _objects[bucket + "|" + key] = Encoding.UTF8.GetBytes(text);
        }

        public bool Has(string bucket, string key)
        {
            return _objects.ContainsKey(bucket + "|" + key);
        }

        public string Text(string bucket, string key)
        {
            return Encoding.UTF8.GetString(_objects[bucket + "|" + key]);
        }

        public List<string> Keys(string bucket)
        {
            return _objects.Keys.Where(k => k.StartsWith(bucket + "|", StringComparison.Ordinal))
                .Select(k => k.Substring(bucket.Length + 1)).OrderBy(k => k, StringComparer.Ordinal).ToList();
        }

        public int CallsStarting(string verb)
        {
            return Calls.Count(c => c.StartsWith(verb + " ", StringComparison.Ordinal));
        }
    }
}
