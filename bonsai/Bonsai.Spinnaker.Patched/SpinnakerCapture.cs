using OpenCV.Net;
using SpinnakerNET;
using SpinnakerNET.GenApi;
using System;
using System.ComponentModel;
using System.Reactive;
using System.Reactive.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Xml.Serialization;

namespace Bonsai.Spinnaker
{
    [XmlType(Namespace = Constants.XmlNamespace)]
    [Description("Acquires a sequence of images from a Spinnaker camera.")]
    public class SpinnakerCapture : Source<SpinnakerDataFrame>
    {
        static readonly object systemLock = new object();
        static readonly string[] RequiredChunkNames =
        {
            "Timestamp",
            "FrameID",
            "LineStatusAll",
            "ExposureEndLineStatusAll"
        };

        [Description("The optional index of the camera from which to acquire images.")]
        public int? Index { get; set; }

        [TypeConverter(typeof(SerialNumberConverter))]
        [Description("The optional serial number of the camera from which to acquire images.")]
        public string SerialNumber { get; set; }

        [Description("The method used to process bayer pattern encoded color images.")]
        public ColorProcessingAlgorithm ColorProcessing { get; set; }

        [Description("The number of transport-layer stream buffers to allocate on acquisition start when manual buffer mode is enabled.")]
        public int StreamBufferCount { get; set; } = 64;

        protected virtual void Configure(IManagedCamera camera)
        {
            var nodeMap = camera.GetNodeMap();
            ConfigureStream(camera);

            var chunkMode = nodeMap.GetNode<IBool>("ChunkModeActive");
            if (chunkMode != null && chunkMode.IsWritable)
            {
                chunkMode.Value = true;
                var chunkSelector = nodeMap.GetNode<IEnum>("ChunkSelector");
                if (chunkSelector != null && chunkSelector.IsReadable)
                {
                    foreach (var chunkName in RequiredChunkNames)
                    {
                        var chunkSelectorEntry = chunkSelector.GetEntryByName(chunkName);
                        if (chunkSelectorEntry == null || !chunkSelectorEntry.IsAvailable || !chunkSelectorEntry.IsReadable)
                            continue;

                        chunkSelector.Value = chunkSelectorEntry.Value;
                        var chunkEnable = nodeMap.GetNode<IBool>("ChunkEnable");
                        if (chunkEnable == null || chunkEnable.Value || !chunkEnable.IsWritable)
                            continue;
                        chunkEnable.Value = true;
                    }
                }
            }

            var acquisitionMode = nodeMap.GetNode<IEnum>("AcquisitionMode");
            if (acquisitionMode == null || !acquisitionMode.IsWritable)
            {
                throw new InvalidOperationException("Unable to set acquisition mode to continuous.");
            }

            var continuousAcquisitionMode = acquisitionMode.GetEntryByName("Continuous");
            if (continuousAcquisitionMode == null || !continuousAcquisitionMode.IsReadable)
            {
                throw new InvalidOperationException("Unable to set acquisition mode to continuous.");
            }

            acquisitionMode.Value = continuousAcquisitionMode.Symbolic;
        }

        void ConfigureStream(IManagedCamera camera)
        {
            var streamNodeMap = camera.GetTLStreamNodeMap();
            if (streamNodeMap == null)
                return;

            var bufferCountMode = streamNodeMap.GetNode<IEnum>("StreamBufferCountMode");
            if (bufferCountMode != null && bufferCountMode.IsWritable)
            {
                var manualMode = bufferCountMode.GetEntryByName("Manual");
                if (manualMode != null && manualMode.IsReadable)
                {
                    bufferCountMode.Value = manualMode.Symbolic;
                }
            }

            var bufferCount = streamNodeMap.GetNode<IInteger>("StreamBufferCountManual");
            if (bufferCount != null && bufferCount.IsWritable)
            {
                var desiredCount = Math.Max(3, StreamBufferCount);
                var minimum = (int)bufferCount.Min;
                var maximum = (int)bufferCount.Max;
                if (desiredCount < minimum) desiredCount = minimum;
                if (desiredCount > maximum) desiredCount = maximum;
                bufferCount.Value = desiredCount;
            }

            var handlingMode = streamNodeMap.GetNode<IEnum>("StreamBufferHandlingMode");
            if (handlingMode != null && handlingMode.IsWritable)
            {
                var oldestFirst = handlingMode.GetEntryByName("OldestFirst");
                if (oldestFirst != null && oldestFirst.IsReadable)
                {
                    handlingMode.Value = oldestFirst.Symbolic;
                }
            }
        }

        static Func<IManagedImage, IplImage> GetConverter(PixelFormatEnums pixelFormat, ColorProcessingAlgorithm colorProcessing)
        {
            int outputChannels;
            IplDepth outputDepth;
            if (pixelFormat < PixelFormatEnums.BayerGR8 || pixelFormat == PixelFormatEnums.BGR8 ||
                pixelFormat <= PixelFormatEnums.BayerBG16 && colorProcessing == ColorProcessingAlgorithm.NONE)
            {
                if (pixelFormat == PixelFormatEnums.BGR8)
                {
                    outputChannels = 3;
                    outputDepth = IplDepth.U8;
                }
                else
                {
                    outputChannels = 1;
                    var depthFactor = (int)pixelFormat;
                    if (pixelFormat > PixelFormatEnums.Mono16) depthFactor = (depthFactor - 3) / 4;
                    outputDepth = (IplDepth)(8 * (depthFactor + 1));
                }

                return image =>
                {
                    var width = (int)image.Width;
                    var height = (int)image.Height;
                    using (var bitmapHeader = new IplImage(new Size(width, height), outputDepth, outputChannels, image.DataPtr))
                    {
                        var output = new IplImage(bitmapHeader.Size, outputDepth, outputChannels);
                        CV.Copy(bitmapHeader, output);
                        return output;
                    }
                };
            }

            PixelFormatEnums outputFormat;
            if (pixelFormat == PixelFormatEnums.Mono10p ||
                pixelFormat == PixelFormatEnums.Mono10Packed ||
                pixelFormat == PixelFormatEnums.Mono12p ||
                pixelFormat == PixelFormatEnums.Mono12Packed)
            {
                outputFormat = PixelFormatEnums.Mono16;
                outputDepth = IplDepth.U16;
                outputChannels = 1;
            }
            else if (pixelFormat >= PixelFormatEnums.BayerGR8 && pixelFormat <= PixelFormatEnums.BayerBG16)
            {
                outputFormat = PixelFormatEnums.BGR8;
                outputDepth = IplDepth.U8;
                outputChannels = 3;
            }
            else throw new InvalidOperationException(string.Format("Unable to convert pixel format {0}.", pixelFormat));

            return image =>
            {
                var width = (int)image.Width;
                var height = (int)image.Height;
                var output = new IplImage(new Size(width, height), outputDepth, outputChannels);
                unsafe
                {
                    using (var destination = new ManagedImage((uint)width, (uint)height, 0, 0, outputFormat, output.ImageData.ToPointer()))
                    {
                        image.ConvertToBitmapSource(outputFormat, destination, (SpinnakerNET.ColorProcessingAlgorithm)colorProcessing);
                        return output;
                    }
                }
            };
        }

        public override IObservable<SpinnakerDataFrame> Generate()
        {
            return Generate(Observable.Return(Unit.Default));
        }

        public IObservable<SpinnakerDataFrame> Generate<TSource>(IObservable<TSource> start)
        {
            var captureInstance = (SpinnakerCapture)MemberwiseClone();
            return Observable.Create<SpinnakerDataFrame>((observer, cancellationToken) =>
            {
                return Task.Factory.StartNew(async () =>
                {
                    IManagedCamera camera;
                    lock (systemLock)
                    {
                        try
                        {
                            using var system = new ManagedSystem();
                            var serialNumber = captureInstance.SerialNumber;
                            var cameraList = system.GetCameras();
                            if (!string.IsNullOrEmpty(serialNumber))
                            {
                                camera = cameraList.GetBySerial(serialNumber);
                                if (camera == null)
                                {
                                    var message = string.Format("Spinnaker camera with serial number {0} was not found.", serialNumber);
                                    throw new InvalidOperationException(message);
                                }
                            }
                            else
                            {
                                var index = captureInstance.Index.GetValueOrDefault(0);
                                if (index < 0 || index >= cameraList.Count)
                                {
                                    var message = string.Format("No Spinnaker camera was found at index {0}.", index);
                                    throw new InvalidOperationException(message);
                                }

                                camera = cameraList.GetByIndex((uint)index);
                            }

                            cameraList.Clear();
                        }
                        catch (Exception ex)
                        {
                            observer.OnError(ex);
                            throw;
                        }
                    }

                    ImageEventListener imageListener = null;
                    try
                    {
                        camera.Init();
                        captureInstance.Configure(camera);

                        imageListener = new ImageEventListener(observer, captureInstance.ColorProcessing);
                        camera.RegisterEventHandler(imageListener);
                        await WaitForStartAsync(start, cancellationToken);

                        camera.BeginAcquisition();
                        cancellationToken.WaitHandle.WaitOne();
                        camera.EndAcquisition();
                    }
                    catch (Exception ex)
                    {
                        observer.OnError(ex);
                        throw;
                    }
                    finally
                    {
                        if (imageListener is not null)
                            camera.UnregisterEventHandler(imageListener);
                        camera.DeInit();
                        camera.Dispose();
                    }
                },
                cancellationToken,
                TaskCreationOptions.LongRunning,
                TaskScheduler.Default).Unwrap();
            });
        }

        static Task WaitForStartAsync<TSource>(IObservable<TSource> start, CancellationToken cancellationToken)
        {
            var tcs = new TaskCompletionSource<object>();
            IDisposable subscription = null;
            subscription = start.Subscribe(
                _ =>
                {
                    tcs.TrySetResult(null);
                },
                ex => tcs.TrySetException(ex),
                () => tcs.TrySetResult(null));

            cancellationToken.Register(() =>
            {
                subscription?.Dispose();
                tcs.TrySetCanceled();
            });

            return tcs.Task.ContinueWith(task =>
            {
                subscription?.Dispose();
                return task;
            }, CancellationToken.None, TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default).Unwrap();
        }

        class ImageEventListener : ManagedImageEventHandler
        {
            readonly IObserver<SpinnakerDataFrame> observer;
            readonly ColorProcessingAlgorithm colorProcessing;
            Func<IManagedImage, IplImage> converter;
            PixelFormatEnums pixelFormat;

            public ImageEventListener(IObserver<SpinnakerDataFrame> observer, ColorProcessingAlgorithm colorProcessing)
            {
                this.observer = observer ?? throw new ArgumentNullException(nameof(observer));
                this.colorProcessing = colorProcessing;
            }

            protected override void OnImageEvent(ManagedImage image)
            {
                try
                {
                    if (image.IsIncomplete)
                        return;

                    if (converter == null || image.PixelFormat != pixelFormat)
                    {
                        converter = GetConverter(image.PixelFormat, colorProcessing);
                        pixelFormat = image.PixelFormat;
                    }

                    var output = converter(image);
                    observer.OnNext(new SpinnakerDataFrame(output, image.ChunkData));
                }
                catch (Exception ex)
                {
                    observer.OnError(ex);
                    throw;
                }
                finally
                {
                    image.Release();
                }
            }
        }
    }
}
