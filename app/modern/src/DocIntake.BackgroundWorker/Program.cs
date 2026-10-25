using System;
using System.Threading.Tasks;
using DocIntake.Core;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

namespace DocIntake.BackgroundWorker
{
    public static class Program
    {
        public static async Task Main(string[] args)
        {
            var options = BackgroundWorkerOptions.FromEnvironment();
            var builder = Host.CreateApplicationBuilder(args);
            builder.Services.AddSingleton(options);
            builder.Services.AddSingleton<IClock, SystemClock>();
            builder.Services.AddSingleton<IFileStore>(_ => new NfsFileStore(options.AppDataRoot));
            builder.Services.AddSingleton<IClaimStore>(_ => new FileClaimStore(options.AppDataRoot));
            builder.Services.AddSingleton<IControlPlaneRecorder>(_ =>
                new FileControlPlaneRecorder(options.AppDataRoot, options.ControlPlanePath));
            builder.Services.AddSingleton<IWorkItemProcessor>(services =>
                new DocIntakeWorkItemProcessor(services.GetRequiredService<IFileStore>(), options.Stage));
            builder.Services.AddHostedService<DocIntakeBackgroundService>();
            await builder.Build().RunAsync().ConfigureAwait(false);
        }
    }
}
