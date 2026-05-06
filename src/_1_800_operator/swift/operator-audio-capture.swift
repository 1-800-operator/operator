// operator-audio-capture.swift — slip-mode dual-stream audio helper.
//
// Captures system audio via ScreenCaptureKit and the user's microphone via
// AVAudioEngine in a single process; writes framed PCM chunks to stdout.
// AttachAdapter spawns this as a subprocess on join() and feeds the chunks
// into per-stream Whisper instances on the Python side.
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
//   ScreenCaptureKit:           https://developer.apple.com/documentation/screencapturekit
//   SCStreamConfiguration.capturesAudio (macOS 13+)
//   AVAudioEngine.inputNode:    https://developer.apple.com/documentation/avfaudio/avaudioengine
//   AVCaptureDevice mic auth:   https://developer.apple.com/documentation/avfoundation/avcapturedevice/1624584-authorizationstatus

import Foundation
import ScreenCaptureKit
import CoreMedia
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
// Two queues (SCStream's audio queue + AVAudioEngine's tap queue) both call
// writeFrame; serialize via a lock so frame headers and payloads can never
// interleave. fwrite is thread-safe at the libc level, but a partial frame
// write on the system queue followed by a partial frame write on the mic
// queue would corrupt the framing.
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

// Microphone — required for AVAudioEngine.inputNode. Different TCC service
// than Screen Recording; granted independently.
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

// MARK: - System-audio capture (ScreenCaptureKit)

final class StreamDelegate: NSObject, SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("operator-audio-capture: SCStream stopped: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
}

final class SystemAudioOutput: NSObject, SCStreamOutput {
    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        // Count the callback FIRST — SCK fires this at the configured rate
        // regardless of whether audio is actually playing, and the empty-buffer
        // case (no data buffer attached, or zero-length data) is normal during
        // silence. The watchdog wants to distinguish "SCK never fired" (real
        // TCC silent-failure) from "SCK fired but the system was quiet"; if
        // we increment only on non-empty buffers, a quiet system kills the
        // helper at 10s. Voice-preserved counted unconditionally; we match.
        systemStats.callbacks += 1
        guard let bb = CMSampleBufferGetDataBuffer(sb) else { return }
        let n = CMBlockBufferGetDataLength(bb)
        guard n > 0 else { return }
        var buf = [UInt8](repeating: 0, count: n)
        let copyOK = buf.withUnsafeMutableBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return false }
            return CMBlockBufferCopyDataBytes(bb, atOffset: 0, dataLength: n, destination: base) == kCMBlockBufferNoErr
        }
        guard copyOK else { return }
        buf.withUnsafeBytes { raw in
            if let base = raw.baseAddress {
                writeFrame(tag: TAG_SYSTEM, payload: base, length: n)
            }
        }
        systemStats.bytes += n
        if systemStats.callbacks <= 3 {
            fputs("operator-audio-capture: [S] callback #\(systemStats.callbacks) — \(n) bytes\n", stderr)
        }
    }
}

let sysDelegate = StreamDelegate()
let sysOutput = SystemAudioOutput()
var sysStarted = false

SCShareableContent.getWithCompletionHandler { content, error in
    if let error = error {
        fputs("operator-audio-capture: SCK shareable content error: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
    guard let display = content?.displays.first else {
        fputs("operator-audio-capture: SCK no displays\n", stderr)
        exit(2)
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
    cfg.sampleRate = 16000
    cfg.channelCount = 1
    cfg.width = 2; cfg.height = 2
    cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

    let filter = SCContentFilter(display: display, excludingWindows: [])
    let stream = SCStream(filter: filter, configuration: cfg, delegate: sysDelegate)
    do {
        try stream.addStreamOutput(sysOutput, type: .audio,
                                   sampleHandlerQueue: DispatchQueue(label: "operator.audio.system"))
    } catch {
        fputs("operator-audio-capture: SCK addOutput error: \(error)\n", stderr)
        exit(1)
    }
    stream.startCapture { error in
        if let error = error {
            fputs("operator-audio-capture: SCK startCapture error: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
        sysStarted = true
        fputs("operator-audio-capture: SCK capturing 16kHz mono Float32\n", stderr)
    }
}

// MARK: - Microphone capture (AVAudioEngine)

let engine = AVAudioEngine()
let micQueue = DispatchQueue(label: "operator.audio.mic")
// Target format matches the system stream: Float32 mono 16kHz.
guard let target = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                 sampleRate: 16000,
                                 channels: 1,
                                 interleaved: false) else {
    fputs("operator-audio-capture: FATAL — could not build target AVAudioFormat\n", stderr)
    exit(6)
}
let inputNode = engine.inputNode
let hwFormat = inputNode.outputFormat(forBus: 0)
fputs("operator-audio-capture: mic hardware format \(hwFormat.sampleRate)Hz \(hwFormat.channelCount)ch\n", stderr)

guard let converter = AVAudioConverter(from: hwFormat, to: target) else {
    fputs("operator-audio-capture: FATAL — no converter from \(hwFormat) to \(target)\n", stderr)
    exit(6)
}

inputNode.installTap(onBus: 0, bufferSize: 1024, format: hwFormat) { (buffer, _) in
    micQueue.async {
        // Allocate an output buffer sized for the converted frame count.
        let ratio = target.sampleRate / hwFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 16)
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: outCapacity) else { return }
        var error: NSError?
        var supplied = false
        let status = converter.convert(to: outBuf, error: &error) { _, outStatus in
            if supplied {
                outStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            outStatus.pointee = .haveData
            return buffer
        }
        if status == .error {
            fputs("operator-audio-capture: mic convert error: \(error?.localizedDescription ?? "?")\n", stderr)
            return
        }
        let frames = Int(outBuf.frameLength)
        guard frames > 0, let chans = outBuf.floatChannelData else { return }
        let bytes = frames * MemoryLayout<Float32>.size
        writeFrame(tag: TAG_MIC, payload: UnsafeRawPointer(chans[0]), length: bytes)
        micStats.callbacks += 1
        micStats.bytes += bytes
        if micStats.callbacks <= 3 {
            fputs("operator-audio-capture: [M] callback #\(micStats.callbacks) — \(bytes) bytes\n", stderr)
        }
    }
}

do {
    try engine.start()
    fputs("operator-audio-capture: AVAudioEngine started\n", stderr)
} catch {
    fputs("operator-audio-capture: FATAL — engine.start: \(error.localizedDescription)\n", stderr)
    exit(6)
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
// `tccutil reset ScreenCapture com.operator.audio-capture` + respawn (see
// AttachAdapter._audio_reader_loop). Exit code 4 = system silent-failure;
// the parent retries once. Voice-preserved's runner used exactly this
// recipe (pipeline/runner.py:342). If the system stays silent after the
// retry, parent falls back to mic-only — slip still works for the user's
// own voice, system audio is "best effort."
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
    engine.stop()
    fputs("operator-audio-capture: totals [S]=\(systemStats.bytes)B [M]=\(micStats.bytes)B\n", stderr)
    exit(0)
}

signal(SIGINT) { _ in
    fputs("operator-audio-capture: SIGINT — exiting\n", stderr)
    exit(0)
}

RunLoop.main.run()
_ = sysStarted  // keep referenced; suppresses "never used" warning
