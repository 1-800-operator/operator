// operator-audio-capture.swift — dial-mode dual-stream audio helper.
//
// Captures system audio AND the user's microphone via ScreenCaptureKit's
// captureMicrophone API (macOS 15+) in a single SCStream; writes framed PCM
// chunks to stdout. AttachAdapter spawns this as a subprocess on join() and
// feeds the chunks into per-stream Whisper instances on the Python side.
//
// Why SCStream for mic instead of AVAudioEngine.inputNode (Phase 14.21.1):
// AVAudioEngine.inputNode loses to Chrome's WebRTC over Bluetooth HFP — when
// AirPods are connected and Chrome is unmuted in Meet, both processes try
// to bind the same SCO link and the second binder gets zero-filled buffers
// (the BT HFP single-channel limitation). SCStream's captureMicrophone goes
// through Apple's screen-recording subsystem which fans out the mic stream
// to all consumers without the per-process exclusivity. Validated against
// AirPods + Bose in debug/14_21_mic_capture_spike/.
//
// Mid-meeting device tracking: macOS Sequoia auto-flips the system default
// input device when a Bluetooth headset connects/disconnects. SCStream does
// NOT auto-follow the default after startCapture — it's sticky to the device
// at start time. We close that gap with a `kAudioHardwarePropertyDefaultInputDevice`
// listener that stops + restarts SCStream with the new device's `uniqueID`
// as `cfg.microphoneCaptureDeviceID` whenever the default changes.
//
// Output framing on stdout (binary):
//   [1-byte tag: 'S' (0x53) = system, 'M' (0x4D) = mic]
//   [4-byte big-endian uint32: payload length in bytes]
//   [N bytes: Float32 PCM, little-endian, 16 kHz mono]
//
// Diagnostics + errors go to stderr.
//
// Build: swiftc operator-audio-capture.swift -O -o operator-audio-capture
// Stop:  Ctrl-C, or close stdin (parent process exits).
//
// Refs:
//   ScreenCaptureKit:               https://developer.apple.com/documentation/screencapturekit
//   captureMicrophone (macOS 15+):  https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/capturemicrophone
//   microphoneCaptureDeviceID:      https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/microphonecapturedeviceid
//   kAudioHardwarePropertyDefaultInputDevice property listener — Core Audio HAL.
//   AVCaptureDevice mic auth:       https://developer.apple.com/documentation/avfoundation/avcapturedevice/1624584-authorizationstatus

import Foundation
import ScreenCaptureKit
import CoreMedia
import CoreAudio
import AVFoundation

setbuf(stdout, nil)

// --probe: read-only TCC status report for `operator doctor`. Prints one
// line of JSON to stdout and exits 0 without ever prompting the user.
// TCC.db is SIP-protected, so the only supported way to query our own
// grants is to ask the system from inside this process (DECISION.md §2).
if CommandLine.arguments.contains("--probe") {
    let sck = CGPreflightScreenCaptureAccess() ? "ok" : "denied"
    let micStr: String
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: micStr = "ok"
    case .denied: micStr = "denied"
    case .restricted: micStr = "restricted"
    case .notDetermined: micStr = "not_determined"
    @unknown default: micStr = "unknown"
    }
    print("{\"screen_recording\":\"\(sck)\",\"microphone\":\"\(micStr)\"}")
    exit(0)
}

fputs("operator-audio-capture: starting (pid=\(getpid()))\n", stderr)

// Parent-process diagnostics — TCC attribution flows up the responsible-process
// chain, so knowing who spawned us is load-bearing for permission debugging.
let parentPID = getppid()
if let parent = NSRunningApplication(processIdentifier: parentPID) {
    let name = parent.localizedName ?? "unknown"
    let bundle = parent.bundleIdentifier ?? "no-bundle-id"
    fputs("operator-audio-capture: parent: \(name) (\(bundle), pid=\(parentPID))\n", stderr)
} else {
    fputs("operator-audio-capture: parent pid=\(parentPID) (likely a shell)\n", stderr)
}

// MARK: - Framed stdout writer
//
// Two SCStream output handlers (system audio + mic) both call writeFrame.
// Serialize via a lock so frame headers and payloads can never interleave.
// fwrite is thread-safe at the libc level, but a partial frame write on the
// system queue followed by a partial frame write on the mic queue would
// corrupt the framing.
let writeLock = NSLock()
let TAG_SYSTEM: UInt8 = 0x53  // 'S'
let TAG_MIC: UInt8 = 0x4D     // 'M'

func writeFrame(tag: UInt8, payload: UnsafeRawPointer, length: Int) {
    guard length > 0, length <= Int(UInt32.max) else { return }
    writeLock.lock()
    defer { writeLock.unlock() }
    var header = [UInt8](repeating: 0, count: 5)
    header[0] = tag
    let len = UInt32(length).bigEndian
    withUnsafeBytes(of: len) { bytes in
        for i in 0..<4 { header[1 + i] = bytes[i] }
    }
    _ = header.withUnsafeBufferPointer { buf in
        fwrite(buf.baseAddress, 1, 5, stdout)
    }
    _ = fwrite(payload, 1, length, stdout)
}

// MARK: - Per-stream callback counters (for watchdog + periodic stderr stats)

final class StreamStats {
    var callbacks: Int = 0
    var bytes: Int = 0
}
let systemStats = StreamStats()
let micStats = StreamStats()

// MARK: - TCC preflight

// Screen Recording — required for SCStream audio (Apple gates audio behind the
// same TCC service as video). Without it, startCapture can succeed yet zero
// callbacks fire (the silent-failure mode reproduced in the 14.20.1 spike).
if !CGPreflightScreenCaptureAccess() {
    fputs("operator-audio-capture: Screen Recording permission not granted — requesting\n", stderr)
    CGRequestScreenCaptureAccess()
    Thread.sleep(forTimeInterval: 3)
    if !CGPreflightScreenCaptureAccess() {
        fputs("operator-audio-capture: FATAL — Screen Recording permission denied\n", stderr)
        fputs("operator-audio-capture: System Settings > Privacy & Security > Screen Recording\n", stderr)
        exit(3)
    }
}
fputs("operator-audio-capture: Screen Recording permission OK\n", stderr)

// Microphone — required for SCStream's captureMicrophone the same way it was
// required for AVAudioEngine.inputNode. Different TCC service than Screen
// Recording; granted independently.
let micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
switch micStatus {
case .authorized:
    fputs("operator-audio-capture: Microphone permission OK\n", stderr)
case .notDetermined:
    fputs("operator-audio-capture: Microphone permission not determined — requesting\n", stderr)
    let sema = DispatchSemaphore(value: 0)
    AVCaptureDevice.requestAccess(for: .audio) { granted in
        fputs("operator-audio-capture: Microphone access granted=\(granted)\n", stderr)
        sema.signal()
    }
    _ = sema.wait(timeout: .now() + 10)
    if AVCaptureDevice.authorizationStatus(for: .audio) != .authorized {
        fputs("operator-audio-capture: FATAL — Microphone permission denied\n", stderr)
        fputs("operator-audio-capture: System Settings > Privacy & Security > Microphone\n", stderr)
        exit(5)
    }
case .denied, .restricted:
    fputs("operator-audio-capture: FATAL — Microphone permission denied\n", stderr)
    fputs("operator-audio-capture: System Settings > Privacy & Security > Microphone\n", stderr)
    exit(5)
@unknown default:
    fputs("operator-audio-capture: FATAL — unknown mic auth status\n", stderr)
    exit(5)
}

// MARK: - Core Audio device helpers (for default-input listener + restart)

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

// MARK: - System-audio output handler

final class StreamDelegate: NSObject, SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("operator-audio-capture: SCStream stopped: \(error.localizedDescription)\n", stderr)
        // Don't exit — restartStreamForCurrentDefaultInput may have stopped
        // this stream intentionally to swap devices. The new stream is
        // already being built and will resume capture.
    }
}

// Target output format: matches the mic path — Float32 mono 16kHz. Whisper
// downstream expects this, and homogenizing both streams keeps Python's
// AudioProcessor format-agnostic.
let targetFormat: AVAudioFormat = {
    guard let f = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                sampleRate: 16000,
                                channels: 1,
                                interleaved: false) else {
        fputs("operator-audio-capture: FATAL — could not build target format\n", stderr)
        exit(7)
    }
    return f
}()

// Lazily-initialized converter for the system stream. SCK delivers 48kHz
// stereo Float32 (per cfg below), but we don't hardcode the source format —
// we discover it from the first sample buffer and build the converter to
// match. That way config drift on the SCK side doesn't silently produce
// wrong-shape audio.
var sysConverter: AVAudioConverter?
var sysSourceFormat: AVAudioFormat?

final class SystemAudioOutput: NSObject, SCStreamOutput {
    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        // Count the callback FIRST — SCK fires this at the configured rate
        // regardless of whether audio is actually playing, and the empty-buffer
        // case (no data buffer attached, or zero-length data) is normal during
        // silence. The watchdog wants to distinguish "SCK never fired" (real
        // TCC silent-failure) from "SCK fired but the system was quiet"; if
        // we increment only on non-empty buffers, a quiet system kills the
        // helper at 10s.
        systemStats.callbacks += 1

        guard let formatDesc = CMSampleBufferGetFormatDescription(sb),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            return
        }

        // Lazy converter init on first callback. SCK's delivered format may
        // differ slightly from what we configured (channel layout, interleaved
        // vs not). Build the converter from the actual source format we see.
        if sysConverter == nil {
            var asbd = asbdPtr.pointee
            guard let srcFormat = AVAudioFormat(streamDescription: &asbd) else {
                fputs("operator-audio-capture: SCK could not derive AVAudioFormat from CMSampleBuffer\n", stderr)
                return
            }
            sysSourceFormat = srcFormat
            guard let conv = AVAudioConverter(from: srcFormat, to: targetFormat) else {
                fputs("operator-audio-capture: SCK no converter from \(srcFormat) to \(targetFormat)\n", stderr)
                return
            }
            sysConverter = conv
            fputs("operator-audio-capture: [S] source format \(srcFormat.sampleRate)Hz \(srcFormat.channelCount)ch → resampling to 16kHz mono\n", stderr)
        }
        guard let converter = sysConverter, let srcFormat = sysSourceFormat else { return }

        guard let outBuf = convertSampleBuffer(sb, srcFormat: srcFormat, converter: converter) else { return }
        let frames = Int(outBuf.frameLength)
        guard frames > 0, let chans = outBuf.floatChannelData else { return }
        let bytes = frames * MemoryLayout<Float32>.size
        writeFrame(tag: TAG_SYSTEM, payload: UnsafeRawPointer(chans[0]), length: bytes)
        systemStats.bytes += bytes
        if systemStats.callbacks <= 3 {
            fputs("operator-audio-capture: [S] callback #\(systemStats.callbacks) — \(bytes) bytes (post-resample)\n", stderr)
        }
    }
}

// MARK: - Microphone output handler
//
// Mirrors the system-audio path but with two differences:
//   1. The source format can CHANGE mid-stream when the user plugs in or
//      removes a Bluetooth/USB device (default-input listener triggers a
//      stream restart, but the in-flight format flip happens before the
//      restart completes). We rebuild micConverter on detected change.
//   2. SCStream's captureMicrophone ignores cfg.sampleRate and delivers at
//      the device's preferred rate (24 kHz HFP for AirPods, 16 kHz HFP for
//      Bose, 48 kHz mono for built-in MacBook mic, etc.). The dynamic
//      converter resamples whatever-it-is to 16 kHz Float32 mono.

var micConverter: AVAudioConverter?
var micSourceFormat: AVAudioFormat?

final class MicAudioOutput: NSObject, SCStreamOutput {
    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .microphone else { return }
        micStats.callbacks += 1

        guard let formatDesc = CMSampleBufferGetFormatDescription(sb),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            return
        }
        var asbd = asbdPtr.pointee
        guard let srcFormat = AVAudioFormat(streamDescription: &asbd) else { return }

        // Rebuild converter on first callback OR when the source format
        // changes mid-stream (e.g., default-input listener fired but the
        // first post-restart buffer arrives with the new format before the
        // listener-triggered restart completes its sequence).
        if micSourceFormat == nil
            || micSourceFormat!.sampleRate != srcFormat.sampleRate
            || micSourceFormat!.channelCount != srcFormat.channelCount {
            let from = micSourceFormat
            micSourceFormat = srcFormat
            micConverter = AVAudioConverter(from: srcFormat, to: targetFormat)
            if let from = from {
                fputs("operator-audio-capture: [M] source format CHANGED: \(from.sampleRate)Hz \(from.channelCount)ch → \(srcFormat.sampleRate)Hz \(srcFormat.channelCount)ch (rebuilt converter)\n", stderr)
            } else {
                fputs("operator-audio-capture: [M] source format \(srcFormat.sampleRate)Hz \(srcFormat.channelCount)ch → resampling to 16kHz mono\n", stderr)
            }
        }
        guard let converter = micConverter else { return }

        guard let outBuf = convertSampleBuffer(sb, srcFormat: srcFormat, converter: converter) else { return }
        let frames = Int(outBuf.frameLength)
        guard frames > 0, let chans = outBuf.floatChannelData else { return }
        let bytes = frames * MemoryLayout<Float32>.size
        writeFrame(tag: TAG_MIC, payload: UnsafeRawPointer(chans[0]), length: bytes)
        micStats.bytes += bytes
        if micStats.callbacks <= 3 {
            fputs("operator-audio-capture: [M] callback #\(micStats.callbacks) — \(bytes) bytes (post-resample)\n", stderr)
        }
    }
}

// MARK: - Shared sample-buffer → resampled AVAudioPCMBuffer helper

func convertSampleBuffer(_ sb: CMSampleBuffer, srcFormat: AVAudioFormat, converter: AVAudioConverter) -> AVAudioPCMBuffer? {
    var bufferListSize = 0
    var status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sb,
        bufferListSizeNeededOut: &bufferListSize,
        bufferListOut: nil,
        bufferListSize: 0,
        blockBufferAllocator: nil,
        blockBufferMemoryAllocator: nil,
        flags: 0,
        blockBufferOut: nil
    )
    if status != noErr || bufferListSize == 0 { return nil }

    let listPtr = UnsafeMutableRawPointer.allocate(byteCount: bufferListSize, alignment: 16)
    defer { listPtr.deallocate() }
    let bufferList = listPtr.assumingMemoryBound(to: AudioBufferList.self)
    var blockBuffer: CMBlockBuffer?
    status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sb,
        bufferListSizeNeededOut: nil,
        bufferListOut: bufferList,
        bufferListSize: bufferListSize,
        blockBufferAllocator: nil,
        blockBufferMemoryAllocator: nil,
        flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
        blockBufferOut: &blockBuffer
    )
    if status != noErr { return nil }

    guard let inputPCM = AVAudioPCMBuffer(
        pcmFormat: srcFormat,
        bufferListNoCopy: bufferList,
        deallocator: nil
    ) else { return nil }
    inputPCM.frameLength = AVAudioFrameCount(CMSampleBufferGetNumSamples(sb))

    let ratio = targetFormat.sampleRate / srcFormat.sampleRate
    let outCapacity = AVAudioFrameCount(Double(inputPCM.frameLength) * ratio + 16)
    guard let outBuf = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: outCapacity) else { return nil }

    var convError: NSError?
    var supplied = false
    let convStatus = converter.convert(to: outBuf, error: &convError) { _, outStatus in
        if supplied {
            outStatus.pointee = .noDataNow
            return nil
        }
        supplied = true
        outStatus.pointee = .haveData
        return inputPCM
    }
    if convStatus == .error {
        fputs("operator-audio-capture: convert error: \(convError?.localizedDescription ?? "?")\n", stderr)
        return nil
    }
    return outBuf
}

// MARK: - Stream lifecycle (initial build + restart on default-input change)
//
// State accessed by the listener AND the initial-build path. Mutated only
// inside restartQueue closures or the SCShareableContent completion handler
// (which runs once before the listener is registered).
//
// Strong reference at module scope (sysStream / globalStream): without it,
// ARC deallocates the SCStream when the SCShareableContent closure returns
// — startCapture's completion handler still fires (so we log "SCK
// capturing"), but the stream is gone before any audio callbacks land.
// macOS 14 internally retained the stream during capture; macOS 15 doesn't,
// so a local `let stream` inside the closure compiles fine but silently
// produces zero callbacks.
//
// Full debug trail: docs/agent-context.md — Hard-Won Knowledge entry
// "macOS 15 SCStream silently drops audio callbacks unless the stream
// object is held in a strong reference …" (session 206).

let sysDelegate = StreamDelegate()
let sysOutput = SystemAudioOutput()
let micOutput = MicAudioOutput()
var globalDisplay: SCDisplay?
var globalStream: SCStream?
var currentMicDeviceUID: String?

// Serialize stop+rebuild+start so concurrent listener fires can't trample
// each other. SCStream's start/stop completion handlers run on internal
// SCStream queues, so blocking on a semaphore inside a restartQueue
// closure does not deadlock.
let restartQueue = DispatchQueue(label: "operator.audio.restart")

func buildStream(forMicDeviceUID uid: String?) -> SCStream? {
    guard let display = globalDisplay else {
        fputs("operator-audio-capture: buildStream — no display cached\n", stderr)
        return nil
    }
    let cfg = SCStreamConfiguration()
    cfg.capturesAudio = true
    // Match voice-preserved's working config: false. With responsibility
    // disclaim active (see _disclaimed_spawn.py), our process IS the
    // current-process from SCK's POV; setting this to true filtered out
    // audio in some intermittent SCK startup races (callbacks would
    // never fire). Voice-preserved ran for hundreds of meetings with
    // false and never echoed because the helper has no audio output of
    // its own to be excluded.
    cfg.excludesCurrentProcessAudio = false
    // macOS 15 (Sequoia) SCStream silently denies audio callbacks when
    // sampleRate/channelCount don't match the system's preferred audio
    // format. Apple's docs note 48000/2 as the working config; the Azayaka
    // open-source recorder (active on macOS 15) uses the same. We resample
    // to 16k mono Float32 client-side before forwarding bytes.
    cfg.sampleRate = 48000
    cfg.channelCount = 2
    cfg.queueDepth = 5
    // Mic capture (macOS 15+). nil means "system default at start time" —
    // sticky once the stream starts, which is why we do device tracking
    // via the kAudioHardwarePropertyDefaultInputDevice listener and rebuild
    // the stream on change. cfg.sampleRate does NOT apply to .microphone
    // output; the mic stream is delivered at the device's preferred rate
    // (24 kHz AirPods HFP, 16 kHz Bose HFP, 48 kHz built-in MacBook mic),
    // which is why MicAudioOutput resamples dynamically per source format.
    cfg.captureMicrophone = true
    cfg.microphoneCaptureDeviceID = uid
    cfg.width = 2; cfg.height = 2
    cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

    let filter = SCContentFilter(display: display, excludingWindows: [])
    let stream = SCStream(filter: filter, configuration: cfg, delegate: sysDelegate)
    do {
        try stream.addStreamOutput(sysOutput, type: .audio,
                                   sampleHandlerQueue: DispatchQueue(label: "operator.audio.system"))
        try stream.addStreamOutput(micOutput, type: .microphone,
                                   sampleHandlerQueue: DispatchQueue(label: "operator.audio.mic"))
    } catch {
        fputs("operator-audio-capture: addStreamOutput err: \(error)\n", stderr)
        return nil
    }
    return stream
}

/// Stop the current SCStream, rebuild with the new system-default input
/// device's `uniqueID`, restart. Serialized via restartQueue. Returns
/// without doing work if the new default's UID matches what we're already
/// capturing on.
func restartStreamForCurrentDefaultInput() {
    restartQueue.async {
        let newDefaultID = currentDefaultInputDevice()
        let newUID = deviceUID(newDefaultID)
        let newName = deviceName(newDefaultID)
        if let curUID = currentMicDeviceUID, curUID == newUID {
            return  // listener fired but the device didn't actually change
        }
        fputs("operator-audio-capture: ⟳ swapping mic device → \(newName) uid=\(newUID)\n", stderr)

        // Stop the old stream, wait for completion before starting the new
        // one. Without the wait we can't serialize cleanly (concurrent
        // streams could compete for the same SCK resources during the
        // overlap window).
        let oldStream = globalStream
        if let old = oldStream {
            let stopSema = DispatchSemaphore(value: 0)
            old.stopCapture { _ in stopSema.signal() }
            _ = stopSema.wait(timeout: .now() + 3)
        }
        globalStream = nil

        guard let newStream = buildStream(forMicDeviceUID: newUID) else {
            fputs("operator-audio-capture: restart — buildStream returned nil; helper continues without active capture\n", stderr)
            return
        }
        let startSema = DispatchSemaphore(value: 0)
        var startError: Error?
        newStream.startCapture { e in
            startError = e
            startSema.signal()
        }
        _ = startSema.wait(timeout: .now() + 3)
        if let e = startError {
            fputs("operator-audio-capture: restart — startCapture error: \(e.localizedDescription)\n", stderr)
            return
        }
        globalStream = newStream
        currentMicDeviceUID = newUID
        fputs("operator-audio-capture: ✓ restart complete on \(newName)\n", stderr)
    }
}

// MARK: - Default-input listener

let defaultInputListener: AudioObjectPropertyListenerProc = { _, _, _, _ in
    restartStreamForCurrentDefaultInput()
    return kAudioHardwareNoError
}

func registerDefaultInputListener() {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    let st = AudioObjectAddPropertyListener(
        AudioObjectID(kAudioObjectSystemObject), &addr, defaultInputListener, nil)
    if st != noErr {
        fputs("operator-audio-capture: WARN — could not register default-input listener (status=\(st))\n", stderr)
        return
    }
    fputs("operator-audio-capture: registered default-input device listener\n", stderr)
}

// MARK: - Initial setup

SCShareableContent.getWithCompletionHandler { content, error in
    if let error = error {
        fputs("operator-audio-capture: SCK shareable content error: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
    guard let display = content?.displays.first else {
        fputs("operator-audio-capture: SCK no displays\n", stderr)
        exit(2)
    }
    globalDisplay = display

    // Initial build: nil microphoneCaptureDeviceID = use system default at
    // start time. Cache the resolved UID immediately so the listener can
    // detect changes against it.
    guard let stream = buildStream(forMicDeviceUID: nil) else {
        fputs("operator-audio-capture: initial buildStream failed\n", stderr)
        exit(1)
    }
    globalStream = stream
    currentMicDeviceUID = deviceUID(currentDefaultInputDevice())

    stream.startCapture { error in
        if let error = error {
            fputs("operator-audio-capture: SCK startCapture error: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
        let defID = currentDefaultInputDevice()
        fputs("operator-audio-capture: SCK capturing system + microphone (mic device: \(deviceName(defID)) uid=\(currentMicDeviceUID ?? "(nil)"))\n", stderr)
        // Register listener AFTER initial start so we have a valid currentMicDeviceUID
        // to compare against. A device-change event firing during the SCK
        // startup window would have nothing to compare to.
        registerDefaultInputListener()
    }
}

// MARK: - Periodic stats + silent-failure detection
//
// Time-series visibility every 2s for the first 12s — surfaces SCK startup
// patterns (some Macs fire [S] callbacks immediately, some take 4-6s, some
// stay silent forever in tccd-cache-stale mode). Without periodic logs we
// only saw the binary endpoints (start, FATAL, EOF), making intermittent
// silent-failure indistinguishable from "system was just quiet."
for delaySeconds in stride(from: 2, through: 12, by: 2) {
    let d = delaySeconds
    DispatchQueue.global().asyncAfter(deadline: .now() + Double(d)) {
        fputs("operator-audio-capture: stats t=\(d)s [S]=\(systemStats.callbacks)cb/\(systemStats.bytes)B [M]=\(micStats.callbacks)cb/\(micStats.bytes)B\n", stderr)
    }
}

// Watchdog: only FATAL if mic is silent (real bug we always want to surface).
// System-stream silence at 10s is recoverable from the Python side via
// `tccutil reset ScreenCapture com.1-800-operator.audio-capture` + respawn (see
// AttachAdapter._audio_reader_loop). Exit code 4 = system silent-failure;
// the parent retries once. If the system stays silent after the retry,
// parent falls back to mic-only — dial still works for the user's own
// voice, system audio is "best effort."
DispatchQueue.global().asyncAfter(deadline: .now() + 10) {
    // Mic silent at 10s is unrecoverable — exit so parent fails fast.
    if micStats.callbacks == 0 {
        fputs("operator-audio-capture: FATAL — mic: 0 callbacks in 10s\n", stderr)
        exit(5)
    }
    // System silent at 10s is recoverable (tccd stale cache) BUT we don't
    // tear down the helper — that would also kill mic, which is working.
    // Just log loudly. The first stats line (t=2s) plus this 10s warning
    // gives the parent enough to decide whether to attempt a tccutil
    // reset + respawn out-of-band, or accept mic-only operation. If SCK
    // later starts firing (it sometimes self-recovers in Meet contexts
    // where remote audio is steady), [S] picks up live with no restart.
    if systemStats.callbacks == 0 {
        fputs("operator-audio-capture: WARN — system audio: 0 callbacks in 10s (likely tccd cache stale)\n", stderr)
        fputs("operator-audio-capture: helper continues with mic-only; system stream may self-recover\n", stderr)
    }
}

// MARK: - Lifecycle: stop on stdin EOF or SIGINT

DispatchQueue.global().async {
    while readLine() != nil {}
    fputs("operator-audio-capture: stdin EOF — shutting down\n", stderr)
    if let s = globalStream {
        let sema = DispatchSemaphore(value: 0)
        s.stopCapture { _ in sema.signal() }
        _ = sema.wait(timeout: .now() + 2)
    }
    fputs("operator-audio-capture: totals [S]=\(systemStats.bytes)B [M]=\(micStats.bytes)B\n", stderr)
    exit(0)
}

signal(SIGINT) { _ in
    fputs("operator-audio-capture: SIGINT — exiting\n", stderr)
    exit(0)
}

RunLoop.main.run()
