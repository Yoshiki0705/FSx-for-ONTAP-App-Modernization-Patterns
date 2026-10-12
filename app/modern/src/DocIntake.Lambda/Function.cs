using System;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Amazon.Lambda.Core;
using Amazon.S3;
using Amazon.S3.Model;
using DocIntake.Core;
using DocIntake.Probe;

[assembly: LambdaSerializer(typeof(Amazon.Lambda.Serialization.SystemTextJson.DefaultLambdaJsonSerializer))]

namespace DocIntake.Lambda
{
    // The invocation payload: {"mode": "worker" | "probe", "run_id": "...", "stage": "s3"}.
    public class Invocation
    {
        [JsonPropertyName("mode")]
        public string Mode { get; set; }

        [JsonPropertyName("run_id")]
        public string RunId { get; set; }

        [JsonPropertyName("stage")]
        public string Stage { get; set; }
    }

    // Stage-3 handler. Handler string: DocIntake.Lambda::DocIntake.Lambda.Function::Handler.
    //
    // Both modes reuse the console code paths over an S3FileStore on the FSx for ONTAP S3 Access
    // Point (env S3_ACCESS_POINT_ARN):
    //   worker  DocIntake.Worker.Program.Run(store, "s3"): reports and index under out/s3/.
    //   probe   DocIntake.Probe.Program.RunFiveBehaviors with store kind s3 (stage 3): the
    //           appmod-probe/1 JSON is written to the artifacts bucket (env ARTIFACTS_BUCKET) at
    //           probe/<run-id>/lambda-s3.json and returned as the invocation result.
    //
    // An invalid payload throws ArgumentException before any S3 call, so a bad invocation is an
    // error result rather than a partial run. The template also sets STORE and FS_IDENTITY; they are
    // informational and not read here.
    public class Function
    {
        public const string Stage = "s3";
        public const int ProbeStage = 3;
        public const string AccessPointVariable = "S3_ACCESS_POINT_ARN";
        public const string ArtifactsBucketVariable = "ARTIFACTS_BUCKET";

        // The run id becomes part of an object key, so only a plain token is accepted.
        private static readonly Regex RunIdPattern = new Regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$");

        private readonly IAmazonS3 _s3;
        private readonly string _accessPoint;
        private readonly string _artifactsBucket;

        // Used by the Lambda runtime. Missing configuration fails the init (before an SDK client is
        // built), not a later invocation.
        public Function()
            : this(RequiredEnv(AccessPointVariable), RequiredEnv(ArtifactsBucketVariable))
        {
        }

        private Function(string accessPoint, string artifactsBucket)
            : this(new AmazonS3Client(), accessPoint, artifactsBucket)
        {
        }

        public Function(IAmazonS3 s3, string accessPoint, string artifactsBucket)
        {
            _s3 = s3 ?? throw new ArgumentNullException("s3");
            if (string.IsNullOrEmpty(accessPoint))
            {
                throw new ArgumentException("access point is required", "accessPoint");
            }
            if (string.IsNullOrEmpty(artifactsBucket))
            {
                throw new ArgumentException("artifacts bucket is required", "artifactsBucket");
            }
            _accessPoint = accessPoint;
            _artifactsBucket = artifactsBucket;
        }

        public static string ProbeResultKey(string runId)
        {
            return "probe/" + runId + "/lambda-s3.json";
        }

        public JsonElement Handler(Invocation input, ILambdaContext context)
        {
            Validate(input);
            var store = new S3FileStore(_s3, _accessPoint, "");
            if (input.Mode == "worker")
            {
                return RunWorker(store, input, context);
            }
            return RunProbe(store, input, context);
        }

        // Order: payload, mode, stage, run_id. run_id is required for both modes because the worker
        // response carries it too.
        private static void Validate(Invocation input)
        {
            if (input == null)
            {
                throw new ArgumentException("payload is required");
            }
            if (input.Mode != "worker" && input.Mode != "probe")
            {
                throw new ArgumentException("mode must be worker or probe (got '" + input.Mode + "')");
            }
            if (input.Stage != Stage)
            {
                throw new ArgumentException("stage must be " + Stage + " (got '" + input.Stage + "')");
            }
            if (string.IsNullOrEmpty(input.RunId))
            {
                throw new ArgumentException("run_id is required");
            }
            if (!RunIdPattern.IsMatch(input.RunId))
            {
                throw new ArgumentException("run_id must match " + RunIdPattern);
            }
        }

        private static JsonElement RunWorker(IFileStore store, Invocation input, ILambdaContext context)
        {
            var exitCode = DocIntake.Worker.Program.Run(store, Stage);
            if (exitCode != 0)
            {
                // Run printed the exception to stderr (CloudWatch Logs). Fail the invocation so the
                // caller does not read a failed pass as a completed one.
                throw new InvalidOperationException("worker exited " + exitCode + "; see the function log");
            }
            if (context != null)
            {
                context.Logger.LogInformation("worker: pass complete under out/" + Stage + "/");
            }
            var status = new
            {
                mode = "worker",
                run_id = input.RunId,
                stage = Stage,
                exit_code = exitCode,
                output_prefix = "out/" + Stage + "/"
            };
            return JsonSerializer.SerializeToElement(status);
        }

        private JsonElement RunProbe(IFileStore store, Invocation input, ILambdaContext context)
        {
            var config = new ProbeConfig
            {
                StoreKind = "s3",
                Root = _accessPoint,
                Stage = ProbeStage,
                Role = "writer",
                RunId = input.RunId
            };
            var json = DocIntake.Probe.Program.RunFiveBehaviors(config, store);

            // Parse before uploading, so a malformed record is never stored as a result.
            JsonElement result;
            using (var document = JsonDocument.Parse(json))
            {
                result = document.RootElement.Clone();
            }

            var key = ProbeResultKey(input.RunId);
            _s3.PutObjectAsync(new PutObjectRequest
            {
                BucketName = _artifactsBucket,
                Key = key,
                ContentBody = json,
                ContentType = "application/json"
            }).GetAwaiter().GetResult();
            if (context != null)
            {
                context.Logger.LogInformation("probe: wrote s3://" + _artifactsBucket + "/" + key);
            }
            return result;
        }

        private static string RequiredEnv(string name)
        {
            var value = Environment.GetEnvironmentVariable(name);
            if (string.IsNullOrEmpty(value))
            {
                throw new InvalidOperationException("environment variable " + name + " is required");
            }
            return value;
        }
    }
}
