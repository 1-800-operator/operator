// spike_mic_via_sckit.swift — Phase 14.21.1 hypothesis test
//
// Question: when AirPods are connected and Chrome's WebRTC has the BT mic open
// (slip Chrome unmuted in a Meet), can a SECOND process capture the same mic
// via SCStream's macOS 15 captureMicrophone API, while AVAudioEngine.inputNode
// returns silence in that scenario?
//
// Granola's empirical behavior says yes (see USER_NOTE.md). This spike either
// reproduces that result or disproves the hypothesis.
//
// What this spike does:
//   - Opens an SCStream with capturesAudio + captureMicrophone both true.
//   - Captures system audio (.audio) AND mic audio (.microphone) in parallel.
//   - Dumps each stream to a 16-bit PCM WAV file you can open in QuickTime.
//   - Logs per-callback RMS so silence-vs-signal is obvious in the log.
//   - Auto-stops after 30 seconds (or earlier on Ctrl-C).
//
// Output:
//   /tmp/spike_mic.wav    — mic stream (AirPods or whatever the system default is)
//   /tmp/spike_sys.wav    — system audio (whatever's playing through speakers)
//   /tmp/spike_mic.log    — per-callback RMS + summary
//
// Build:
//   cd debug/14_21_mic_capture_spike
//   swiftc -O spike_mic_via_sckit.swift -o spike_mic_via_sckit
//   codesign --force --sign - spike_mic_via_sckit
//
// Run:
//   ./spike_mic_via_sckit 2> /tmp/spike_mic.log
//
// Test procedure (mirror Granola validation):
//   1. AirPods connected. macOS Sound Settings > Input set to AirPods mic.
//   2. Open a Meet (or any voice call) in Chrome, unmute mic.
//   3. Walk into another room (laptop mic CANNOT hear you).
//   4. Run the spike. Speak something distinctive.
//   5. After 30s, open /tmp/spike_mic.wav in QuickTime — does it have your voice?
//
// If yes: hypothesis confirmed; replace the AVAudioEngine path in the
//   production helper with this approach.
// If no: hypothesis disproved; SCStream's mic path has the same exclusivity
//   limitation as AVAudioEngine, and Granola is doing something else.
//
// Refs:
//   captureMicrophone (macOS 15+): https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/capturemicrophone
//   microphoneCaptureDeviceID:     https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/microphonecapturedeviceid
//   SCStreamOutputType.microphone: https://developer.apple.com/documentation/screencapturekit/scstreamoutputtype/microphone
//   AVCaptureDevice mic auth:      https://developer.apple.com/documentation/avfoundation/avcapturedevice/1624584-authorizationstatus
import Foundation
import ScreenCaptureKit
import CoreMedia
import CoreAudio
import AVFoundation

setbuf(stdout, nil)

// MARK: - Configuration

let CAPTURE_SECONDS: TimeInterval = 30
let TARGET_SAMPLE_RATE: Double = 16000  // matches production helper
let TARGET_CHANNELS: UInt32 = 1
let MIC_WAV_PATH = "/tmp/spike_mic.wav"
let SYS_WAV_PATH = "/tmp/spike_sys.wav"

// MARK: - Minimal WAV writer (16-bit PCM, mono, 16kHz)
//
// QuickTime / Finder Preview / `afplay` all open this format. Writing
// Int16 instead of Float32 because Float32 WAV (IEEE 4-byte) playback
// support is patchier across system tools.

final class WavWriter {
    private let handle: FileHandle
    private var byteCount: UInt32 = 0
    private let sampleRate: UInt32
    private let channels: UInt16

    init?(path: String, sampleRate: Double, channels: UInt32) {
        FileManager.default.createFile(atPath: path, contents: nil, attributes: nil)
        guard let fh = FileHandle(forWritingAtPath: path) else { return nil }
        self.handle = fh
        self.sampleRate = UInt32(sampleRate)
        self.channels = UInt16(channels)
        // Reserve 44 bytes for the header; we'll rewrite it on close.
        try? fh.write(contentsOf: Data(count: 44))
    }

    /// Append Float32 mono samples; clamps to [-1, 1] and converts to Int16.
    func append(float32: [Float32]) {
        var buf = Data(capacity: float32.count * 2)
        for f in float32 {
            let clamped = max(-1.0, min(1.0, f))
            let s = Int16(clamped * 32767.0)
            var le = s.littleEndian
            withUnsafeBytes(of: &le) { buf.append(contentsOf: $0) }
        }
        try? handle.write(contentsOf: buf)
        byteCount += UInt32(buf.count)
    }

    func close() {
        // Rewrite the 44-byte WAV header with the final byte count.
        let bitsPerSample: UInt16 = 16
        let byteRate = sampleRate * UInt32(channels) * UInt32(bitsPerSample / 8)
        let blockAlign = channels * (bitsPerSample / 8)
        var header = Data()
        header.append("RIFF".data(using: .ascii)!)
        var fileSize = UInt32(36 + byteCount).littleEndian
        withUnsafeBytes(of: &fileSize) { header.append(contentsOf: $0) }
        header.append("WAVE".data(using: .ascii)!)
        header.append("fmt ".data(using: .ascii)!)
        var fmtChunkSize: UInt32 = (16 as UInt32).littleEndian
        withUnsafeBytes(of: &fmtChunkSize) { header.append(contentsOf: $0) }
        var audioFormat: UInt16 = (1 as UInt16).littleEndian  // PCM
        withUnsafeBytes(of: &audioFormat) { header.append(contentsOf: $0) }
        var ch = channels.littleEndian
        withUnsafeBytes(of: &ch) { header.append(contentsOf: $0) }
        var sr = sampleRate.littleEndian
        withUnsafeBytes(of: &sr) { header.append(contentsOf: $0) }
        var br = byteRate.littleEndian
        withUnsafeBytes(of: &br) { header.append(contentsOf: $0) }
        var ba = blockAlign.littleEndian
        withUnsafeBytes(of: &ba) { header.append(contentsOf: $0) }
        var bps = bitsPerSample.littleEndian
        withUnsafeBytes(of: &bps) { header.append(contentsOf: $0) }
        header.append("data".data(using: .ascii)!)
        var dataSize = byteCount.littleEndian
        withUnsafeBytes(of: &dataSize) { header.append(contentsOf: $0) }
        try? handle.seek(toOffset: 0)
        try? handle.write(contentsOf: header)
        try? handle.close()
    }
}

// MARK: - Sink — handles both .audio (system) and .microphone (mic)

final class Sink: NSObject, SCStreamOutput, SCStreamDelegate {
    let micWav: WavWriter
    let sysWav: WavWriter
    var micCalls = 0, sysCalls = 0
    var micBytes = 0, sysBytes = 0
    var micRmsAccum: Double = 0, sysRmsAccum: Double = 0

    // Resampler for the mic path. macOS's SCStream delivers .microphone at the
    // device's preferred rate (24 kHz on AirPods HFP; 48 kHz on built-in;
    // varies). cfg.sampleRate does NOT apply to .microphone (verified by the
    // format probe). We always resample to 16 kHz mono Float32 for whisper.
    // Rebuilt on the first callback and again whenever the source format
    // changes (e.g., user puts on AirPods mid-stream — SCStream auto-follows
    // the system default mic but the buffer format flips).
    let micTargetFormat: AVAudioFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: 16000,
        channels: 1,
        interleaved: false
    )!
    var micConverter: AVAudioConverter?
    var micSourceFormat: AVAudioFormat?
    var micFormatChanges = 0  // count source-format transitions for the summary

    init(micWav: WavWriter, sysWav: WavWriter) {
        self.micWav = micWav
        self.sysWav = sysWav
    }

    func stream(_ s: SCStream, didOutputSampleBuffer b: CMSampleBuffer, of t: SCStreamOutputType) {
        guard let bb = CMSampleBufferGetDataBuffer(b) else { return }
        // Probe the actual stream format on the FIRST callback of each type — verifies
        // whether SCStream is honoring cfg.sampleRate for .microphone or delivering
        // at the device's native rate.
        if (t == .microphone && micCalls == 0) || (t == .audio && sysCalls == 0) {
            if let fd = CMSampleBufferGetFormatDescription(b),
               let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fd) {
                let label = (t == .microphone) ? "[M] mic" : "[S] sys"
                fputs("spike: \(label) actual format: sampleRate=\(asbd.pointee.mSampleRate) channels=\(asbd.pointee.mChannelsPerFrame) bytesPerFrame=\(asbd.pointee.mBytesPerFrame) formatID=\(asbd.pointee.mFormatID)\n", stderr)
            }
        }

        if t == .microphone {
            handleMicCallback(buffer: b, blockBuffer: bb)
        } else if t == .audio {
            handleSysCallback(blockBuffer: bb)
        }
    }

    /// Resample 24/48/whatever-kHz mono Float32 mic input down to 16 kHz, write WAV.
    /// Detects mid-stream format changes (device swap) and rebuilds the converter.
    private func handleMicCallback(buffer b: CMSampleBuffer, blockBuffer bb: CMBlockBuffer) {
        // Read source format from this buffer.
        guard let fd = CMSampleBufferGetFormatDescription(b),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fd) else { return }
        var asbd = asbdPtr.pointee
        guard let srcFormat = AVAudioFormat(streamDescription: &asbd) else { return }

        // Rebuild converter on first callback OR when source format changes
        // (e.g. user plugs in AirPods mid-meeting, default input flips).
        if micSourceFormat == nil
            || micSourceFormat!.sampleRate != srcFormat.sampleRate
            || micSourceFormat!.channelCount != srcFormat.channelCount {
            let from = micSourceFormat
            micSourceFormat = srcFormat
            micConverter = AVAudioConverter(from: srcFormat, to: micTargetFormat)
            micFormatChanges += 1
            if let from = from {
                fputs("spike: [M] source format CHANGED: \(from.sampleRate)Hz \(from.channelCount)ch → \(srcFormat.sampleRate)Hz \(srcFormat.channelCount)ch (rebuilt converter, change #\(micFormatChanges))\n", stderr)
            } else {
                fputs("spike: [M] source format set to \(srcFormat.sampleRate)Hz \(srcFormat.channelCount)ch (built converter)\n", stderr)
            }
        }
        guard let converter = micConverter else { return }

        // Wrap the CMSampleBuffer's audio data in an AVAudioPCMBuffer
        // (no-copy initializer reads directly from the CMBlockBuffer).
        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(b))
        var audioBufferList = AudioBufferList()
        var blockBufferOut: CMBlockBuffer?
        let st = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            b,
            bufferListSizeNeededOut: nil,
            bufferListOut: &audioBufferList,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBufferOut
        )
        guard st == noErr,
              let inputPCM = AVAudioPCMBuffer(pcmFormat: srcFormat, bufferListNoCopy: &audioBufferList) else {
            return
        }
        inputPCM.frameLength = frameCount

        // Allocate output buffer with capacity for the resampled frames.
        let ratio = micTargetFormat.sampleRate / srcFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(frameCount) * ratio + 16)
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: micTargetFormat, frameCapacity: outCapacity) else { return }

        var supplied = false
        var error: NSError?
        let status = converter.convert(to: outBuf, error: &error) { _, outStatus in
            if supplied { outStatus.pointee = .noDataNow; return nil }
            supplied = true
            outStatus.pointee = .haveData
            return inputPCM
        }
        if status == .error {
            fputs("spike: [M] convert error: \(error?.localizedDescription ?? "?")\n", stderr)
            return
        }

        // Pull resampled Float32 samples out, append to WAV, log RMS.
        let outFrames = Int(outBuf.frameLength)
        guard outFrames > 0, let chans = outBuf.floatChannelData else { return }
        let ptr = chans[0]
        var samples = [Float32](repeating: 0, count: outFrames)
        samples.withUnsafeMutableBufferPointer { dst in
            dst.baseAddress!.update(from: ptr, count: outFrames)
        }
        let rms = sqrt(samples.reduce(0) { $0 + Double($1 * $1) } / Double(max(samples.count, 1)))

        micWav.append(float32: samples)
        micCalls += 1
        micBytes += outFrames * MemoryLayout<Float32>.size
        micRmsAccum += rms
        if micCalls <= 3 || micCalls % 25 == 0 {
            fputs("spike: [M] cb=\(micCalls) outFrames=\(outFrames) rms=\(String(format: "%.4f", rms)) total=\(micBytes)B\n", stderr)
        }
    }

    /// System path: cfg.sampleRate=16000 already gives us 16 kHz mono Float32 here,
    /// so no conversion is needed in the spike (production helper has its own
    /// converter for the 48 kHz stereo case).
    private func handleSysCallback(blockBuffer bb: CMBlockBuffer) {
        let n = CMBlockBufferGetDataLength(bb)
        var data = Data(count: n)
        _ = data.withUnsafeMutableBytes { ptr in
            CMBlockBufferCopyDataBytes(bb, atOffset: 0, dataLength: n, destination: ptr.baseAddress!)
        }
        let samples = data.withUnsafeBytes { Array($0.bindMemory(to: Float32.self)) }
        let rms = sqrt(samples.reduce(0) { $0 + Double($1 * $1) } / Double(max(samples.count, 1)))
        sysWav.append(float32: samples)
        sysCalls += 1
        sysBytes += n
        sysRmsAccum += rms
        if sysCalls <= 3 || sysCalls % 100 == 0 {
            fputs("spike: [S] cb=\(sysCalls) samples=\(samples.count) rms=\(String(format: "%.4f", rms)) total=\(sysBytes)B\n", stderr)
        }
    }

    func stream(_ s: SCStream, didStopWithError e: Error) {
        fputs("spike: stopped: \(e.localizedDescription)\n", stderr)
        exit(1)
    }

    func summary() -> String {
        let micAvg = micCalls > 0 ? micRmsAccum / Double(micCalls) : 0
        let sysAvg = sysCalls > 0 ? sysRmsAccum / Double(sysCalls) : 0
        let micSrcDesc = micSourceFormat.map { "\($0.sampleRate)Hz \($0.channelCount)ch" } ?? "(none)"
        return """
        ── summary ──
        [M] mic:    \(micCalls) callbacks, \(micBytes) bytes (16kHz output), avg RMS = \(String(format: "%.4f", micAvg))
                    final source format: \(micSrcDesc), format changes seen: \(micFormatChanges)
        [S] system: \(sysCalls) callbacks, \(sysBytes) bytes, avg RMS = \(String(format: "%.4f", sysAvg))
        verdict: mic stream \(micAvg > 0.005 ? "HAS signal" : "is silent (RMS near zero)")
        files:   \(MIC_WAV_PATH)  \(SYS_WAV_PATH)
        """
    }
}

// MARK: - Core Audio device listeners (Phase 14.21.1 design probe)
//
// We register two property listeners on the system audio object:
//   - kAudioHardwarePropertyDevices — fires when ANY audio device is added
//     or removed (AirPods connect/disconnect, USB plug, etc.)
//   - kAudioHardwarePropertyDefaultInputDevice — fires when the macOS system
//     default INPUT device changes (manual switch in Sound Settings, or
//     auto-fallback when the active device disappears)
//
// Goal: see which one fires when the user puts on / takes off AirPods
// mid-stream. If kAudioHardwarePropertyDefaultInputDevice fires on connect,
// we don't need any "policy" in production — just follow the default.
// If it doesn't fire (only the device list change does), we'd need to
// actively pick devices ourselves to mimic Meet's behavior.

func deviceName(_ id: AudioDeviceID) -> String {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioObjectPropertyName,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var name: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let st = AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &name)
    guard st == noErr, let n = name?.takeRetainedValue() else { return "(unknown)" }
    return n as String
}

func deviceUID(_ id: AudioDeviceID) -> String {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceUID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var uid: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let st = AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &uid)
    guard st == noErr, let u = uid?.takeRetainedValue() else { return "(unknown)" }
    return u as String
}

func deviceTransport(_ id: AudioDeviceID) -> String {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyTransportType,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var transport: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    let st = AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &transport)
    if st != noErr { return "?" }
    switch transport {
    case kAudioDeviceTransportTypeBuiltIn: return "built-in"
    case kAudioDeviceTransportTypeBluetooth: return "bluetooth"
    case kAudioDeviceTransportTypeBluetoothLE: return "bluetooth-le"
    case kAudioDeviceTransportTypeUSB: return "usb"
    case kAudioDeviceTransportTypeAggregate: return "aggregate"
    case kAudioDeviceTransportTypeVirtual: return "virtual"
    case kAudioDeviceTransportTypeAirPlay: return "airplay"
    case kAudioDeviceTransportTypeContinuityCaptureWired: return "continuity-wired"
    case kAudioDeviceTransportTypeContinuityCaptureWireless: return "continuity-wireless"
    default: return "other(\(transport))"
    }
}

func hasInputStreams(_ id: AudioDeviceID) -> Bool {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreams,
        mScope: kAudioDevicePropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain
    )
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size)
    return size > 0
}

func currentDefaultInputDevice() -> AudioDeviceID {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dev: AudioDeviceID = 0
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &dev)
    return dev
}

func enumerateInputDevices() -> [(AudioDeviceID, String, String, String)] {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    let count = Int(size) / MemoryLayout<AudioDeviceID>.size
    var devices = [AudioDeviceID](repeating: 0, count: count)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &devices)
    var result: [(AudioDeviceID, String, String, String)] = []
    for d in devices where hasInputStreams(d) {
        result.append((d, deviceName(d), deviceUID(d), deviceTransport(d)))
    }
    return result
}

func logCurrentAudioState(_ tag: String) {
    let def = currentDefaultInputDevice()
    let inputs = enumerateInputDevices()
    fputs("spike: \(tag) default-input=\(def) \(deviceName(def)) [\(deviceTransport(def))]\n", stderr)
    fputs("spike: \(tag) input devices (\(inputs.count)):\n", stderr)
    for (id, name, _, transport) in inputs {
        let marker = (id == def) ? " ★" : ""
        fputs("spike:   - id=\(id) name=\"\(name)\" transport=\(transport)\(marker)\n", stderr)
    }
}

// Listener callbacks. C function pointers; no Swift capture allowed,
// so they call top-level helpers.
let devicesListener: AudioObjectPropertyListenerProc = { _, _, _, _ in
    fputs("\nspike: ⚡ kAudioHardwarePropertyDevices FIRED\n", stderr)
    logCurrentAudioState("    (devices listener)")
    return kAudioHardwareNoError
}

let defaultInputListener: AudioObjectPropertyListenerProc = { _, _, _, _ in
    fputs("\nspike: ⚡ kAudioHardwarePropertyDefaultInputDevice FIRED\n", stderr)
    logCurrentAudioState("    (default-input listener)")
    // Act on the change: stop SCStream, restart on the new default device.
    restartStreamForCurrentDefaultInput()
    return kAudioHardwareNoError
}

func registerAudioListeners() {
    var devicesAddr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    let s1 = AudioObjectAddPropertyListener(
        AudioObjectID(kAudioObjectSystemObject), &devicesAddr, devicesListener, nil)
    if s1 != noErr {
        fputs("spike: WARNING — could not register devices listener (status=\(s1))\n", stderr)
    }

    var defaultInputAddr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    let s2 = AudioObjectAddPropertyListener(
        AudioObjectID(kAudioObjectSystemObject), &defaultInputAddr, defaultInputListener, nil)
    if s2 != noErr {
        fputs("spike: WARNING — could not register default-input listener (status=\(s2))\n", stderr)
    }

    fputs("spike: registered audio property listeners (devices + default-input)\n", stderr)
}

// MARK: - Permission preflight

func ensureScreenRecording() {
    if !CGPreflightScreenCaptureAccess() {
        CGRequestScreenCaptureAccess()
        Thread.sleep(forTimeInterval: 3)
        if !CGPreflightScreenCaptureAccess() {
            fputs("spike: TCC denied — grant Screen Recording in System Settings\n", stderr)
            exit(3)
        }
    }
}

func ensureMicrophone() {
    let status = AVCaptureDevice.authorizationStatus(for: .audio)
    switch status {
    case .authorized:
        return
    case .notDetermined:
        let sema = DispatchSemaphore(value: 0)
        AVCaptureDevice.requestAccess(for: .audio) { _ in sema.signal() }
        _ = sema.wait(timeout: .now() + 5)
        if AVCaptureDevice.authorizationStatus(for: .audio) != .authorized {
            fputs("spike: mic permission denied\n", stderr)
            exit(4)
        }
    default:
        fputs("spike: mic permission denied — grant Microphone in System Settings\n", stderr)
        exit(4)
    }
}

// MARK: - Main

ensureScreenRecording()
ensureMicrophone()

// Snapshot the audio device landscape and start listening for changes.
logCurrentAudioState("startup snapshot")
registerAudioListeners()

guard let micWav = WavWriter(path: MIC_WAV_PATH, sampleRate: TARGET_SAMPLE_RATE, channels: UInt32(TARGET_CHANNELS)),
      let sysWav = WavWriter(path: SYS_WAV_PATH, sampleRate: TARGET_SAMPLE_RATE, channels: UInt32(TARGET_CHANNELS)) else {
    fputs("spike: could not open WAV files for write\n", stderr)
    exit(5)
}

let sink = Sink(micWav: micWav, sysWav: sysWav)

// State shared between the initial-build path and the device-swap restart
// path. Mutated only on the restart-control queue below.
var globalDisplay: SCDisplay?
var globalStream: SCStream?
var currentMicDeviceUID: String?  // what microphoneCaptureDeviceID is set to right now (nil = system default at start time)
let restartQueue = DispatchQueue(label: "spike.restart")  // serializes restart attempts
var restartInFlight = false

func buildStream(forMicDeviceUID uid: String?) -> SCStream? {
    guard let display = globalDisplay else {
        fputs("spike: buildStream — no display cached\n", stderr)
        return nil
    }
    let cfg = SCStreamConfiguration()
    cfg.capturesAudio = true
    cfg.sampleRate = Int(TARGET_SAMPLE_RATE)
    cfg.channelCount = Int(TARGET_CHANNELS)
    cfg.excludesCurrentProcessAudio = true
    cfg.captureMicrophone = true
    cfg.microphoneCaptureDeviceID = uid  // explicit on restart; nil at first build = system default
    cfg.width = 2; cfg.height = 2
    cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

    let stream = SCStream(
        filter: SCContentFilter(display: display, excludingWindows: []),
        configuration: cfg,
        delegate: sink
    )
    do {
        try stream.addStreamOutput(sink, type: .audio,
                                    sampleHandlerQueue: DispatchQueue(label: "spike.audio"))
        try stream.addStreamOutput(sink, type: .microphone,
                                    sampleHandlerQueue: DispatchQueue(label: "spike.mic"))
    } catch {
        fputs("spike: addStreamOutput err: \(error)\n", stderr)
        return nil
    }
    return stream
}

/// Called from the default-input listener (or initial setup) — checks
/// whether the system default input device has changed and, if so, stops
/// the current SCStream and starts a new one bound to the new device's UID.
/// Serialized via restartQueue. Re-entrancy gated by restartInFlight.
func restartStreamForCurrentDefaultInput() {
    restartQueue.async {
        if restartInFlight {
            fputs("spike: restart already in flight — skipping re-entrant call\n", stderr)
            return
        }
        let newDefaultID = currentDefaultInputDevice()
        let newUID = deviceUID(newDefaultID)
        let newName = deviceName(newDefaultID)
        if let curUID = currentMicDeviceUID, curUID == newUID {
            fputs("spike: default-input event but UID unchanged (\(newUID)) — no restart\n", stderr)
            return
        }
        fputs("spike: ⟳ swapping mic device → \(newName) [\(deviceTransport(newDefaultID))] uid=\(newUID)\n", stderr)
        restartInFlight = true

        let oldStream = globalStream
        let cleanup: () -> Void = {
            guard let newStream = buildStream(forMicDeviceUID: newUID) else {
                fputs("spike: restart — buildStream returned nil; aborting\n", stderr)
                restartInFlight = false
                return
            }
            globalStream = newStream
            currentMicDeviceUID = newUID
            newStream.startCapture { e in
                if let e = e {
                    fputs("spike: restart — startCapture error: \(e.localizedDescription)\n", stderr)
                } else {
                    fputs("spike: ✓ restart complete on \(newName)\n", stderr)
                }
                restartInFlight = false
            }
        }
        if let s = oldStream {
            s.stopCapture { e in
                if let e = e {
                    fputs("spike: restart — stopCapture error: \(e.localizedDescription)\n", stderr)
                }
                cleanup()
            }
        } else {
            cleanup()
        }
    }
}

SCShareableContent.getWithCompletionHandler { content, err in
    guard let display = content?.displays.first else {
        fputs("spike: no display\n", stderr)
        exit(2)
    }
    globalDisplay = display

    guard let stream = buildStream(forMicDeviceUID: nil) else {
        fputs("spike: initial buildStream failed\n", stderr)
        exit(1)
    }
    globalStream = stream
    // Capture which device that nil resolved to, so the listener can detect changes.
    currentMicDeviceUID = deviceUID(currentDefaultInputDevice())

    stream.startCapture { e in
        if let e = e {
            fputs("spike: startCapture err: \(e)\n", stderr)
            exit(1)
        }
        fputs("spike: capturing system + microphone (\(Int(CAPTURE_SECONDS))s) on initial device uid=\(currentMicDeviceUID ?? "(nil)") — Ctrl-C to stop early\n", stderr)
    }

    DispatchQueue.global().asyncAfter(deadline: .now() + CAPTURE_SECONDS) {
        globalStream?.stopCapture { _ in
            sink.micWav.close()
            sink.sysWav.close()
            fputs("\n\(sink.summary())\n", stderr)
            exit(0)
        }
    }
}

// Use DispatchSourceSignal instead of a raw signal handler so we can call
// non-async-signal-safe Swift APIs (WAV close + summary).
let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
signal(SIGINT, SIG_IGN)  // disable default behavior; the source handles it
sigintSource.setEventHandler {
    fputs("\nspike: SIGINT — closing WAVs and exiting\n", stderr)
    sink.micWav.close()
    sink.sysWav.close()
    fputs("\n\(sink.summary())\n", stderr)
    exit(0)
}
sigintSource.resume()

RunLoop.main.run()
