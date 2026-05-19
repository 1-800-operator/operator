// operator-audio-capture.swift — dial-mode dual-stream audio helper.
//
// Captures the macOS global system audio mix AND the user's microphone via
// two independent pipelines:
//
//   System audio: Core Audio Process Tap (macOS 14.4+) — a CATapDescription
//                 + AudioHardwareCreateProcessTap + private aggregate device
//                 + IOProc. Triggers `kTCCServiceAudioCapture` ("System
//                 Audio Recording Only") — distinct from Screen Recording.
//
//   Microphone:   AVCaptureSession + AVCaptureAudioDataOutput. Triggers
//                 `kTCCServiceMicrophone`.
//
// Writes framed PCM chunks to stdout; AttachAdapter parses them per stream.
//
// Why this architecture (Phase 14.32 migration — May 2026):
//
// Pre-14.32 the helper used a single SCStream for both system audio and
// mic (via macOS 15's captureMicrophone). That worked but had two costs:
//
//   1. SCStream is gated behind Screen Recording (`kTCCServiceScreenCapture`),
//      which on macOS 14 demands a "Quit and Reopen" dance the first time
//      the user toggles the grant in System Settings. install.sh accrued
//      ~50 lines of recovery code across v0.1.30-34 to soften this UX —
//      kill leftover helpers, relaunch detached, AppleScript-activate to
//      foreground, poll TCC state. The new Audio Capture service has no
//      such quirk: a single grant click and the helper just works.
//
//   2. SCStream's audio delivery framing was tuned for video — it carries
//      48kHz stereo audio in 10ms callbacks plus paired (unused) video
//      frames, costing wasted CPU on stereo→mono downmix and video-frame
//      decode. The Tap API delivers mono PCM natively (configurable via
//      CATapDescription.monoGlobalTapButExcludeProcesses) — one converter
//      step gone, slightly less per-callback overhead.
//
// Why AVCaptureSession for mic, not SCStream's captureMicrophone:
//
//   Going back to AVCaptureSession decouples the mic path from Screen
//   Recording entirely. Granola (production app, validates by capturing
//   AirPods mic alongside Chrome WebRTC) is the existence proof that
//   AVCaptureSession can coexist with Chrome's mic binding on modern
//   macOS — the Phase 14.21.1 finding that AVAudioEngine.inputNode lost
//   to Chrome's WebRTC over Bluetooth HFP appears specific to
//   AVAudioEngine's IO unit semantics. A silent-buffer watchdog (5 s
//   tripwire) catches any setup where this coexistence fails and surfaces
//   it loudly in /tmp/operator.log rather than letting the user sit
//   through a silent meeting.
//
// Output framing on stdout (binary, unchanged from pre-14.32):
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
//   CATapDescription:                https://developer.apple.com/documentation/coreaudio/catapdescription
//   AudioHardwareCreateProcessTap:   https://developer.apple.com/documentation/coreaudio/audiohardwarecreateprocesstap
//   kAudioAggregateDeviceTapListKey: Apple HeaderDoc, AudioHardware.h
//   AVCaptureAudioDataOutput:        https://developer.apple.com/documentation/avfoundation/avcaptureaudiodataoutput
//   AVCaptureDevice mic auth:        https://developer.apple.com/documentation/avfoundation/avcapturedevice/1624584-authorizationstatus
//   kAudioHardwarePropertyDefaultInputDevice — Core Audio HAL.

import Foundation
import AppKit
import CoreAudio
import AudioToolbox
import CoreMedia
import AVFoundation

setbuf(stdout, nil)

// chdir to root BEFORE any framework call. The helper inherits its
// working directory from whoever spawned it — typically operator running
// from the user's Desktop or Documents folder (e.g. ~/Desktop/operator).
// macOS's Files-and-Folders TCC ("Desktop Folder Access") prompts the
// user the first time a process accesses anything inside one of those
// protected folders, and AppKit / AVFoundation / Core Audio frameworks
// internally touch the current directory during framework init (preference
// lookups, cache paths, default-device enumeration via system files).
// Without this chdir, the user sees a spurious "Operator wants to access
// files on your Desktop" dialog every time the helper starts up — even
// though we never read or write Desktop files ourselves. Root has no TCC
// protection, so chdir'ing here drops the framework calls onto a safe
// path before they have a chance to trip the protection.
_ = FileManager.default.changeCurrentDirectoryPath("/")

// MARK: - TCC private-API probe (audio-capture service)
//
// `TCCAccessPreflight` reads the current grant state for a TCC service
// without surfacing a dialog. Private but stable since macOS 10.14, and
// already a precedent in this codebase (see _disclaimed_spawn.py's use of
// `responsibility_spawnattrs_setdisclaim` — same risk profile, same vendor).
// Used ONLY in --probe mode so the helper can answer "is the audio-capture
// TCC service granted?" without prompting. The runtime capture path still
// surfaces the dialog naturally when the tap is created with no prior grant.
//
// Returns one of:
//   0 (kTCCAccessPreflightGranted)
//   1 (kTCCAccessPreflightDenied)
//   2 (kTCCAccessPreflightUnknown / not yet prompted)
// A non-zero status with a nil bundle is the "unknown" path.

typealias TCCAccessPreflightFn = @convention(c) (CFString, CFDictionary?) -> Int32

let _tccPreflight: TCCAccessPreflightFn? = {
    guard let handle = dlopen("/System/Library/PrivateFrameworks/TCC.framework/TCC", RTLD_LAZY) else {
        return nil
    }
    guard let sym = dlsym(handle, "TCCAccessPreflight") else { return nil }
    return unsafeBitCast(sym, to: TCCAccessPreflightFn.self)
}()

func tccAudioCaptureStatus() -> String {
    guard let fn = _tccPreflight else { return "unknown" }
    let result = fn("kTCCServiceAudioCapture" as CFString, nil)
    switch result {
    case 0: return "ok"
    case 1: return "denied"
    case 2: return "not_determined"
    default: return "unknown"
    }
}

// MARK: - --probe (TCC status, read-only, never prompts)

if CommandLine.arguments.contains("--probe") {
    let audio = tccAudioCaptureStatus()
    let micStr: String
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: micStr = "ok"
    case .denied: micStr = "denied"
    case .restricted: micStr = "restricted"
    case .notDetermined: micStr = "not_determined"
    @unknown default: micStr = "unknown"
    }
    // Schema (S247.32): "system_audio" replaces the pre-14.32 "screen_recording"
    // key — the underlying TCC service is kTCCServiceAudioCapture, not
    // kTCCServiceScreenCapture, and surfacing the old key would mislead
    // callers (doctor / install.sh / __main__) about which Settings pane
    // the user needs to visit if denied. Python-side parsers updated in lockstep.
    print("{\"system_audio\":\"\(audio)\",\"microphone\":\"\(micStr)\"}")
    exit(0)
}

fputs("operator-audio-capture: starting (pid=\(getpid()))\n", stderr)

// Parent-process diagnostics — TCC attribution flows up the responsible-process
// chain (without disclaim). Knowing who spawned us is load-bearing for
// permission debugging.
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
// Both capture paths (system tap IOProc, mic AVCaptureSession delegate)
// call writeFrame. Serialize via a lock so frame headers and payloads can
// never interleave. fwrite is thread-safe at the libc level, but a partial
// frame write from one queue followed by a partial frame write from another
// would corrupt the framing.
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

// MARK: - Per-stream stats (watchdog + periodic stderr lines)
//
// Track callbacks (was the API delivering data at all?) AND non-zero
// callbacks (was the data actually audible?). The zero-buffer mode is a
// real failure shape — e.g. AVCaptureSession returning empty buffers when
// Chrome's WebRTC holds an exclusive Bluetooth HFP SCO link. Counting only
// total callbacks would hide that.

final class StreamStats {
    var callbacks: Int = 0
    var nonZeroCallbacks: Int = 0
    var bytes: Int = 0
}
let systemStats = StreamStats()
let micStats = StreamStats()

// MARK: - TCC preflight (capture path — not probe path)

// Microphone TCC: AVCaptureSession surfaces the same dialog on first use,
// but checking authorizationStatus first lets us emit a helpful stderr
// line and exit with a known code if denied, rather than letting the
// session fail silently downstream.
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
    // Users typically take 5-15s to read + click Allow on the mic prompt.
    // Same poll window as the pre-14.32 SCStream path.
    _ = sema.wait(timeout: .now() + 60)
    if AVCaptureDevice.authorizationStatus(for: .audio) != .authorized {
        fputs("operator-audio-capture: FATAL — Microphone permission denied or 60s timeout\n", stderr)
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

// Audio Capture TCC: there's no public "request audio capture" API. The
// dialog is surfaced by AudioHardwareCreateProcessTap itself on first
// call when permission is not_determined; if denied, the call returns
// noErr but no IOProc callbacks fire. We poll TCCAccessPreflight after
// tap creation (inside buildSystemTap below) so the helper sits idle
// waiting for the user's click — symmetric with how the mic-permission
// path uses sema.wait above.
//
// Why the poll matters: under the install.sh / __main__ warmup flows,
// the helper is invoked via `open -W -n -a` with stdin=/dev/null, which
// means the stdin-EOF lifecycle handler exits the helper in ~100 ms.
// Without an explicit wait, the helper would exit before the user could
// realistically click the dialog — the `open -W` would return early and
// the post-warmup PROBE_AFTER would falsely report not_determined.
//
// Surface the current state to stderr now so logs are debuggable.
let initialAudioStatus = tccAudioCaptureStatus()
fputs("operator-audio-capture: Audio Capture TCC status (probe): \(initialAudioStatus)\n", stderr)

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

// MARK: - Shared resample target

// Match the pre-14.32 wire format: Float32 16kHz mono. Whisper downstream
// expects this; both legs converge to this shape regardless of source rate.
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

// MARK: - System audio: Core Audio Process Tap
//
// CATapDescription(monoGlobalTapButExcludeProcesses: []) → global mono mix
// of every process's audio output. Wrapped in a private aggregate device
// so we can install an IOProc that delivers PCM to us.
//
// Tap stream format on Apple Silicon: 48 kHz Float32 mono, packed (validated
// in debug/14_31_tcc_warmup_spike/ + /tmp/tapspike/tapspike2). We discover
// the actual format from the tap itself rather than hardcoding, so a future
// macOS that changes the global mix rate (e.g. 96 kHz) doesn't silently
// produce wrong-shape audio.

var sysTapID: AudioObjectID = kAudioObjectUnknown
var sysAggregateID: AudioObjectID = kAudioObjectUnknown
var sysIOProcID: AudioDeviceIOProcID?
let sysTapUUID = UUID()

// Lazily-initialized converter — built from the actual source format we
// observe on the first IOProc callback. Same pattern as the pre-14.32
// SCStream path, kept consistent so debugging muscle-memory carries over.
var sysConverter: AVAudioConverter?
var sysSourceFormat: AVAudioFormat?

func getTapStreamFormat(_ tap: AudioObjectID) -> AudioStreamBasicDescription? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyFormat,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    let st = AudioObjectGetPropertyData(tap, &addr, 0, nil, &size, &asbd)
    return st == noErr ? asbd : nil
}

func buildSystemTap() -> Bool {
    // 1. Tap description: global mono mix, no exclusions, unmuted (don't
    //    interfere with the user hearing audio), private (not visible to
    //    other apps).
    let desc = CATapDescription(monoGlobalTapButExcludeProcesses: [])
    desc.uuid = sysTapUUID
    desc.muteBehavior = .unmuted
    desc.name = "OperatorAudioCapture"
    desc.isPrivate = true

    // 2. Create the tap. This is where the kTCCServiceAudioCapture dialog
    //    surfaces on first call when permission is not yet determined.
    //    The call returns ~immediately regardless — the dialog is async.
    let createStatus = AudioHardwareCreateProcessTap(desc, &sysTapID)
    if createStatus != noErr {
        fputs("operator-audio-capture: FATAL — AudioHardwareCreateProcessTap failed: \(createStatus). Audio Capture permission may be denied.\n", stderr)
        return false
    }
    fputs("operator-audio-capture: tap created (tapID=\(sysTapID))\n", stderr)

    // 2.25. If we entered with not_determined, the dialog just fired async.
    //       Poll TCCAccessPreflight up to 60 s to wait for the user's click
    //       BEFORE we proceed — otherwise the install.sh / __main__ warmup
    //       flow (which invokes the helper via `open -W -n -a` and waits
    //       for it to exit) would return before the user has had a
    //       realistic chance to click Allow, and the post-warmup PROBE
    //       would falsely report not_determined.
    //
    //       Same 60 s window as the mic-permission semaphore wait above.
    //       Skipped on the fast path (already ok) and on explicit deny.
    if initialAudioStatus == "not_determined" {
        fputs("operator-audio-capture: waiting for Audio Capture grant (up to 60s)\n", stderr)
        let deadline = Date().addingTimeInterval(60)
        while tccAudioCaptureStatus() == "not_determined" && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.5)
        }
        let resolved = tccAudioCaptureStatus()
        fputs("operator-audio-capture: Audio Capture grant resolved: \(resolved)\n", stderr)
        if resolved == "denied" {
            // Tap will fail to deliver any audio. Tear down and let the
            // mic-only path proceed.
            AudioHardwareDestroyProcessTap(sysTapID)
            sysTapID = kAudioObjectUnknown
            fputs("operator-audio-capture: System Audio Recording denied — system stream disabled\n", stderr)
            return false
        }
    }

    // 2.5. Query the tap's stream format so the IOProc converter can be
    //      built without guesswork. Currently 48kHz Float32 mono on M-series;
    //      hardcoding would be fragile across OS versions.
    if let tapFormat = getTapStreamFormat(sysTapID) {
        fputs("operator-audio-capture: tap stream format rate=\(tapFormat.mSampleRate) ch=\(tapFormat.mChannelsPerFrame) bits=\(tapFormat.mBitsPerChannel)\n", stderr)
    }

    // 3. Aggregate device wrapping the tap. The IOProc gets installed on
    //    the aggregate, not the tap directly — Apple's documented pattern.
    //    isPrivate=true + isStacked=false keeps it out of Settings/AMS UI.
    //    TapAutoStart=true means audio flows as soon as the aggregate is
    //    started; without it the tap stays cold and IOProc fires with empty
    //    buffers (the original v1 spike's "0 bytes" failure mode).
    let aggregateUID = "operator-audio-tap-\(sysTapUUID.uuidString)"
    let aggregateDict: [String: Any] = [
        kAudioAggregateDeviceUIDKey as String: aggregateUID,
        kAudioAggregateDeviceNameKey as String: "OperatorAudioTap",
        kAudioAggregateDeviceIsPrivateKey as String: true,
        kAudioAggregateDeviceIsStackedKey as String: false,
        kAudioAggregateDeviceTapAutoStartKey as String: true,
        kAudioAggregateDeviceTapListKey as String: [
            [
                kAudioSubTapDriftCompensationKey as String: true,
                kAudioSubTapUIDKey as String: sysTapUUID.uuidString,
            ]
        ],
    ]
    let aggStatus = AudioHardwareCreateAggregateDevice(aggregateDict as CFDictionary, &sysAggregateID)
    if aggStatus != noErr {
        fputs("operator-audio-capture: FATAL — AudioHardwareCreateAggregateDevice failed: \(aggStatus)\n", stderr)
        AudioHardwareDestroyProcessTap(sysTapID)
        sysTapID = kAudioObjectUnknown
        return false
    }
    fputs("operator-audio-capture: aggregate device created (aggregateID=\(sysAggregateID))\n", stderr)

    // 4. Install IOProc on the aggregate device. Block fires ~94 times/sec
    //    on M-series (every ~10.7 ms = 512 frames at 48kHz).
    let cbQueue = DispatchQueue(label: "operator.audio.system.cb")
    let ioStatus = AudioDeviceCreateIOProcIDWithBlock(&sysIOProcID, sysAggregateID, cbQueue) {
        (now, inputData, inputTime, outputData, outputTime) -> Void in
        systemStats.callbacks += 1

        // UnsafeMutableAudioBufferListPointer is the correct iterator —
        // the AudioBufferList struct's mBuffers is a tail-allocated array
        // exposed as a single-element tuple in Swift, NOT a contiguous
        // buffer of mNumberBuffers AudioBuffers (the original v1 spike's
        // bug — see /tmp/tapspike/tapspike2 for the working pattern).
        let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer<AudioBufferList>(mutating: inputData))
        guard abl.count > 0 else { return }
        let ab = abl[0]
        guard ab.mDataByteSize > 0, let raw = ab.mData else { return }

        // Lazy converter init from the FIRST observed source format. ASBD
        // comes from the aggregate's IOProc context — channelsPerFrame
        // reflects what the tap actually delivers (1 for mono global tap).
        if sysConverter == nil {
            // Reconstruct an ASBD from the AudioBuffer we have plus the
            // tap's queried format. Tap format query is the source of
            // truth for sample rate; AudioBuffer.mNumberChannels is the
            // source of truth for channel count.
            let tapFmt = getTapStreamFormat(sysTapID) ?? AudioStreamBasicDescription(
                mSampleRate: 48000,
                mFormatID: kAudioFormatLinearPCM,
                mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
                mBytesPerPacket: 4,
                mFramesPerPacket: 1,
                mBytesPerFrame: 4,
                mChannelsPerFrame: ab.mNumberChannels,
                mBitsPerChannel: 32,
                mReserved: 0
            )
            var asbd = tapFmt
            asbd.mChannelsPerFrame = ab.mNumberChannels
            guard let srcFormat = AVAudioFormat(streamDescription: &asbd) else {
                fputs("operator-audio-capture: [S] could not derive AVAudioFormat from tap ASBD\n", stderr)
                return
            }
            sysSourceFormat = srcFormat
            guard let conv = AVAudioConverter(from: srcFormat, to: targetFormat) else {
                fputs("operator-audio-capture: [S] no converter from \(srcFormat) to \(targetFormat)\n", stderr)
                return
            }
            sysConverter = conv
            fputs("operator-audio-capture: [S] source format \(srcFormat.sampleRate)Hz \(srcFormat.channelCount)ch → resampling to 16kHz mono\n", stderr)
        }
        guard let converter = sysConverter, let srcFormat = sysSourceFormat else { return }

        // Wrap the raw AudioBuffer in an AVAudioPCMBuffer (no-copy) for
        // the converter. The source format is non-interleaved single-channel,
        // matching how the tap delivers data.
        let frameCount = AVAudioFrameCount(Int(ab.mDataByteSize) / Int(srcFormat.streamDescription.pointee.mBytesPerFrame))
        guard frameCount > 0 else { return }

        // Build an AudioBufferList that AVAudioPCMBuffer can consume.
        var bufferList = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(
                mNumberChannels: ab.mNumberChannels,
                mDataByteSize: ab.mDataByteSize,
                mData: raw
            )
        )
        guard let inputPCM = AVAudioPCMBuffer(
            pcmFormat: srcFormat,
            bufferListNoCopy: &bufferList,
            deallocator: nil
        ) else { return }
        inputPCM.frameLength = frameCount

        // Track non-zero before resample, so we can distinguish "tap is
        // delivering empty/silent buffers" from "audio is just quiet."
        // Check the raw Float32 data directly — faster than allocating
        // post-resample for the check.
        let sampleCount = Int(frameCount)
        let fp = raw.bindMemory(to: Float32.self, capacity: sampleCount)
        var anyNonZero = false
        for i in 0..<sampleCount {
            if fp[i] != 0 { anyNonZero = true; break }
        }
        if anyNonZero { systemStats.nonZeroCallbacks += 1 }

        let ratio = targetFormat.sampleRate / srcFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(inputPCM.frameLength) * ratio + 16)
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: outCapacity) else { return }

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
            fputs("operator-audio-capture: [S] convert error: \(convError?.localizedDescription ?? "?")\n", stderr)
            return
        }

        let frames = Int(outBuf.frameLength)
        guard frames > 0, let chans = outBuf.floatChannelData else { return }
        let bytes = frames * MemoryLayout<Float32>.size
        writeFrame(tag: TAG_SYSTEM, payload: UnsafeRawPointer(chans[0]), length: bytes)
        systemStats.bytes += bytes
        if systemStats.callbacks <= 3 {
            fputs("operator-audio-capture: [S] callback #\(systemStats.callbacks) — \(bytes) bytes (post-resample)\n", stderr)
        }
    }
    if ioStatus != noErr || sysIOProcID == nil {
        fputs("operator-audio-capture: FATAL — AudioDeviceCreateIOProcIDWithBlock failed: \(ioStatus)\n", stderr)
        AudioHardwareDestroyAggregateDevice(sysAggregateID)
        sysAggregateID = kAudioObjectUnknown
        AudioHardwareDestroyProcessTap(sysTapID)
        sysTapID = kAudioObjectUnknown
        return false
    }
    let startStatus = AudioDeviceStart(sysAggregateID, sysIOProcID)
    if startStatus != noErr {
        fputs("operator-audio-capture: FATAL — AudioDeviceStart failed: \(startStatus)\n", stderr)
        AudioDeviceDestroyIOProcID(sysAggregateID, sysIOProcID!)
        sysIOProcID = nil
        AudioHardwareDestroyAggregateDevice(sysAggregateID)
        sysAggregateID = kAudioObjectUnknown
        AudioHardwareDestroyProcessTap(sysTapID)
        sysTapID = kAudioObjectUnknown
        return false
    }
    fputs("operator-audio-capture: [S] system-audio tap capturing\n", stderr)
    return true
}

func teardownSystemTap() {
    if let proc = sysIOProcID, sysAggregateID != kAudioObjectUnknown {
        AudioDeviceStop(sysAggregateID, proc)
        AudioDeviceDestroyIOProcID(sysAggregateID, proc)
        sysIOProcID = nil
    }
    if sysAggregateID != kAudioObjectUnknown {
        AudioHardwareDestroyAggregateDevice(sysAggregateID)
        sysAggregateID = kAudioObjectUnknown
    }
    if sysTapID != kAudioObjectUnknown {
        AudioHardwareDestroyProcessTap(sysTapID)
        sysTapID = kAudioObjectUnknown
    }
}

// MARK: - Microphone: AVCaptureSession
//
// AVCaptureSession with a single AVCaptureDeviceInput(audio) and a single
// AVCaptureAudioDataOutput. The output's sample-buffer delegate fires on a
// dedicated queue with CMSampleBuffers containing PCM at the device's
// preferred rate.
//
// Source format can CHANGE mid-stream when the user plugs in or removes a
// device (Bluetooth/USB connect/disconnect). The default-input device
// listener stops + rebuilds the session with the new device's input; the
// converter rebuilds itself lazily on format change in the delegate.

var micSession: AVCaptureSession?
var micConverter: AVAudioConverter?
var micSourceFormat: AVAudioFormat?
var currentMicDeviceUID: String?
// Debug instrumentation for the device-swap convert -1 bug.
var micConvertErrorCount: Int = 0
var micCallbacksSinceLastError: Int = 0
var micCallbacksSinceSwap: Int = 0  // reset to 0 in restartMicForCurrentDefaultInput

final class MicAudioDelegate: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        micStats.callbacks += 1
        micCallbacksSinceSwap += 1
        // Trace the first few post-swap callbacks so we can tell if buffers
        // are reaching the delegate at all after a device swap.
        let postSwap = micCallbacksSinceSwap
        if postSwap <= 5 {
            fputs("operator-audio-capture: [M] delegate post_swap=\(postSwap)\n", stderr)
        }

        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            if postSwap <= 5 {
                fputs("operator-audio-capture: [M] early-return post_swap=\(postSwap) reason=no_format_desc\n", stderr)
            }
            return
        }
        var asbd = asbdPtr.pointee
        guard let srcFormat = AVAudioFormat(streamDescription: &asbd) else {
            if postSwap <= 5 {
                fputs("operator-audio-capture: [M] early-return post_swap=\(postSwap) reason=no_srcFormat\n", stderr)
            }
            return
        }

        // Lazy converter init OR rebuild on source-format change. The
        // listener-triggered restart can sometimes deliver a first
        // post-restart buffer with the new format BEFORE its sequence
        // completes — rebuild defensively.
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

        // Pull the raw audio bytes into an AVAudioPCMBuffer for the converter.
        var bufferListSize = 0
        var status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &bufferListSize,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: nil
        )
        if status != noErr || bufferListSize == 0 { return }

        let listPtr = UnsafeMutableRawPointer.allocate(byteCount: bufferListSize, alignment: 16)
        defer { listPtr.deallocate() }
        let bufferList = listPtr.assumingMemoryBound(to: AudioBufferList.self)
        var blockBuffer: CMBlockBuffer?
        status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: bufferList,
            bufferListSize: bufferListSize,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer
        )
        if status != noErr { return }

        guard let inputPCM = AVAudioPCMBuffer(
            pcmFormat: srcFormat,
            bufferListNoCopy: bufferList,
            deallocator: nil
        ) else { return }
        inputPCM.frameLength = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))

        // Non-zero check before resample: probe the first buffer's raw bytes.
        // The mic-leg silent-buffer watchdog uses this to distinguish "mic
        // is delivering zero-filled buffers" (potential BT HFP exclusivity)
        // from "user is just quiet" (normal silence).
        let abl = UnsafeMutableAudioBufferListPointer(bufferList)
        var anyNonZero = false
        for ab in abl {
            guard ab.mDataByteSize > 0, let raw = ab.mData else { continue }
            let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0
            if isFloat && asbd.mBitsPerChannel == 32 {
                let count = Int(ab.mDataByteSize) / MemoryLayout<Float32>.size
                let p = raw.bindMemory(to: Float32.self, capacity: count)
                for i in 0..<count { if p[i] != 0 { anyNonZero = true; break } }
            } else if !isFloat && asbd.mBitsPerChannel == 16 {
                let count = Int(ab.mDataByteSize) / MemoryLayout<Int16>.size
                let p = raw.bindMemory(to: Int16.self, capacity: count)
                for i in 0..<count { if p[i] != 0 { anyNonZero = true; break } }
            } else {
                // Unknown format — assume non-zero so we don't false-trip the watchdog.
                anyNonZero = true
            }
            if anyNonZero { break }
        }
        if anyNonZero { micStats.nonZeroCallbacks += 1 }

        // Identity-format bypass — empirically, AVAudioConverter returns
        // paramErr (-1) with empty userInfo on the first post-swap buffer
        // when src and target formats match exactly (e.g., Bose HFP coming
        // up at 16kHz/1ch Float32 mid-meeting and matching our 16kHz/1ch
        // Float32 target). Boot-path identity converters work; only the
        // swap-path ones fail. No Apple-documented reason; route around
        // it by skipping the converter entirely when there's no work to
        // do. Faster too — no resampling for the identity case.
        if srcFormat.sampleRate == targetFormat.sampleRate
            && srcFormat.channelCount == targetFormat.channelCount
            && srcFormat.commonFormat == targetFormat.commonFormat {
            // Non-interleaved Float32 mono: channel 0 holds all the samples.
            guard let chans = inputPCM.floatChannelData else { return }
            let frames = Int(inputPCM.frameLength)
            guard frames > 0 else { return }
            let bytes = frames * MemoryLayout<Float32>.size
            writeFrame(tag: TAG_MIC, payload: UnsafeRawPointer(chans[0]), length: bytes)
            micStats.bytes += bytes
            if micStats.callbacks <= 3 {
                fputs("operator-audio-capture: [M] callback #\(micStats.callbacks) — \(bytes) bytes (identity bypass)\n", stderr)
            }
            if postSwap <= 5 {
                fputs("operator-audio-capture: [M] identity-bypass post_swap=\(postSwap) frames=\(frames)\n", stderr)
            }
            return
        }

        let ratio = targetFormat.sampleRate / srcFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(inputPCM.frameLength) * ratio + 16)
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: outCapacity) else { return }

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
            micConvertErrorCount += 1
            micCallbacksSinceLastError = 0
            // Verbose dump on first error of a swap cycle — full NSError +
            // converter/buffer state — so we can tell if the converter
            // permanently broke vs got a single bad buffer.
            if micConvertErrorCount <= 3 {
                let e = convError
                fputs("operator-audio-capture: [M] convert error #\(micConvertErrorCount):\n", stderr)
                fputs("  localizedDescription: \(e?.localizedDescription ?? "?")\n", stderr)
                fputs("  domain: \(e?.domain ?? "?")  code: \(e?.code ?? -999)\n", stderr)
                fputs("  userInfo: \(e?.userInfo as Any)\n", stderr)
                fputs("  srcFormat: \(srcFormat) sampleRate=\(srcFormat.sampleRate) channels=\(srcFormat.channelCount) interleaved=\(srcFormat.isInterleaved) commonFormat=\(srcFormat.commonFormat.rawValue)\n", stderr)
                fputs("  inputPCM frameLength=\(inputPCM.frameLength) frameCapacity=\(inputPCM.frameCapacity)\n", stderr)
                fputs("  targetFormat: \(targetFormat) sampleRate=\(targetFormat.sampleRate) channels=\(targetFormat.channelCount)\n", stderr)
                fputs("  outBuf frameCapacity=\(outBuf.frameCapacity) frameLength=\(outBuf.frameLength)\n", stderr)
            }
            return
        }
        // First successful convert after an error tells us the converter
        // recovered on its own — useful to know we don't need to rebuild.
        if micConvertErrorCount > 0 && micCallbacksSinceLastError == 0 {
            fputs("operator-audio-capture: [M] convert RECOVERED after \(micConvertErrorCount) error(s)\n", stderr)
            micConvertErrorCount = 0
        }
        micCallbacksSinceLastError += 1

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

let micDelegate = MicAudioDelegate()
let micQueue = DispatchQueue(label: "operator.audio.mic.cb")
// Serialize stop+rebuild+start so concurrent listener fires can't trample
// each other. Same shape as the pre-14.32 restartQueue.
let restartQueue = DispatchQueue(label: "operator.audio.mic.restart")

func buildMicSession(forDevice device: AVCaptureDevice?) -> AVCaptureSession? {
    let session = AVCaptureSession()
    let chosenDevice: AVCaptureDevice
    if let d = device {
        chosenDevice = d
    } else {
        guard let d = AVCaptureDevice.default(for: .audio) else {
            fputs("operator-audio-capture: [M] no default audio device\n", stderr)
            return nil
        }
        chosenDevice = d
    }
    let input: AVCaptureDeviceInput
    do {
        input = try AVCaptureDeviceInput(device: chosenDevice)
    } catch {
        fputs("operator-audio-capture: [M] AVCaptureDeviceInput init failed: \(error)\n", stderr)
        return nil
    }
    session.beginConfiguration()
    guard session.canAddInput(input) else {
        fputs("operator-audio-capture: [M] cannot add input\n", stderr)
        return nil
    }
    session.addInput(input)
    let output = AVCaptureAudioDataOutput()
    output.setSampleBufferDelegate(micDelegate, queue: micQueue)
    guard session.canAddOutput(output) else {
        fputs("operator-audio-capture: [M] cannot add output\n", stderr)
        return nil
    }
    session.addOutput(output)
    session.commitConfiguration()
    // Observe session-level runtime errors so we know if the session
    // itself blows up (vs just convert() failing on a single buffer).
    // Note: AVCaptureSessionWasInterrupted's reason key is iOS-only on
    // macOS, so we don't bother with that notification.
    NotificationCenter.default.addObserver(
        forName: AVCaptureSession.runtimeErrorNotification,
        object: session,
        queue: nil
    ) { note in
        let err = note.userInfo?[AVCaptureSessionErrorKey] as? NSError
        fputs("operator-audio-capture: [M] AVCaptureSessionRuntimeError: domain=\(err?.domain ?? "?") code=\(err?.code ?? -999) desc=\(err?.localizedDescription ?? "?")\n", stderr)
    }
    return session
}

func startMicSession() -> Bool {
    let device = AVCaptureDevice.default(for: .audio)
    if let d = device {
        fputs("operator-audio-capture: [M] audio device: \(d.localizedName) uid=\(d.uniqueID)\n", stderr)
    }
    guard let session = buildMicSession(forDevice: device) else {
        return false
    }
    micSession = session
    session.startRunning()
    currentMicDeviceUID = deviceUID(currentDefaultInputDevice())
    fputs("operator-audio-capture: [M] AVCaptureSession running\n", stderr)
    return true
}

/// Stop the current mic session, rebuild with the new system-default
/// input device, restart. Serialized via restartQueue. Returns without
/// doing work if the new default's UID matches what we're already on.
func restartMicForCurrentDefaultInput() {
    restartQueue.async {
        let newDefaultID = currentDefaultInputDevice()
        let newUID = deviceUID(newDefaultID)
        let newName = deviceName(newDefaultID)
        if let curUID = currentMicDeviceUID, curUID == newUID {
            return  // listener fired but the device didn't actually change
        }
        fputs("operator-audio-capture: [M] ⟳ swapping mic device → \(newName) uid=\(newUID)\n", stderr)

        if let old = micSession {
            old.stopRunning()
        }
        micSession = nil
        micConverter = nil
        micSourceFormat = nil
        micCallbacksSinceSwap = 0

        // AVCaptureDevice.default(for: .audio) tracks the current system
        // default at call time, so rebuilding with `nil` picks up the new
        // one automatically. We don't need to look up the AVCaptureDevice
        // by uid explicitly.
        guard let newSession = buildMicSession(forDevice: nil) else {
            fputs("operator-audio-capture: [M] restart — buildMicSession returned nil\n", stderr)
            return
        }
        newSession.startRunning()
        micSession = newSession
        currentMicDeviceUID = newUID
        fputs("operator-audio-capture: [M] ✓ restart complete on \(newName)\n", stderr)
    }
}

// MARK: - Default-input listener (mic only — system tap is global)

let defaultInputListener: AudioObjectPropertyListenerProc = { _, _, _, _ in
    restartMicForCurrentDefaultInput()
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

// MARK: - Bring up both streams

let sysOK = buildSystemTap()
let micOK = startMicSession()

if !micOK {
    fputs("operator-audio-capture: FATAL — mic startup failed\n", stderr)
    teardownSystemTap()
    exit(6)
}

if !sysOK {
    // System tap failed — most likely TCC denial. Continue with mic-only
    // (matches pre-14.32 behavior: dial still works for the user's own
    // voice; system audio is "best effort").
    fputs("operator-audio-capture: WARN — system tap unavailable; continuing mic-only\n", stderr)
}

registerDefaultInputListener()

// MARK: - Periodic stats + silent-failure watchdogs

// 2/4/6/8/10/12s breadcrumbs — same cadence as pre-14.32. Surfaces startup
// patterns when intermittent silent-failure modes hit, and gives us
// time-series visibility into the non-zero-callback rate so we can tell
// "tap is silent because nothing's playing" from "BT HFP zero-fill."
for delaySeconds in stride(from: 2, through: 12, by: 2) {
    let d = delaySeconds
    DispatchQueue.global().asyncAfter(deadline: .now() + Double(d)) {
        fputs("operator-audio-capture: stats t=\(d)s [S]=\(systemStats.callbacks)cb/\(systemStats.nonZeroCallbacks)nz/\(systemStats.bytes)B [M]=\(micStats.callbacks)cb/\(micStats.nonZeroCallbacks)nz/\(micStats.bytes)B\n", stderr)
    }
}

// 10 s watchdog. Two failure modes we want to distinguish:
//   - Mic: 0 callbacks → AVCaptureSession never started delivering. Hard
//     fail (exit 5). Most likely cause: TCC denial that slipped past
//     preflight, or input device disconnected before startRunning settled.
//   - Mic: callbacks > 0 but nonZeroCallbacks == 0 → mic is delivering
//     zero-filled buffers. This is the BT HFP exclusivity signature. Don't
//     exit (the user can re-attach a different audio device mid-meeting),
//     but log loudly so the failure is debuggable from /tmp/operator.log.
//   - System: 0 callbacks → tap install succeeded but aggregate isn't
//     producing data. Recoverable from the Python side via teardown +
//     respawn. Log WARN; helper continues mic-only.
DispatchQueue.global().asyncAfter(deadline: .now() + 10) {
    if micStats.callbacks == 0 {
        fputs("operator-audio-capture: FATAL — mic: 0 callbacks in 10s\n", stderr)
        exit(5)
    }
    if micStats.nonZeroCallbacks == 0 {
        fputs("operator-audio-capture: WARN — mic: \(micStats.callbacks) callbacks in 10s but ALL zero-filled (possible BT HFP exclusivity)\n", stderr)
        fputs("operator-audio-capture: this is the failure mode S209 flagged for AVAudioEngine.inputNode; AVCaptureSession may behave the same on some BT devices\n", stderr)
    }
    if systemStats.callbacks == 0 {
        fputs("operator-audio-capture: WARN — system audio: 0 callbacks in 10s (likely TCC stale cache or aggregate not delivering)\n", stderr)
        fputs("operator-audio-capture: helper continues with mic-only; system stream may self-recover\n", stderr)
    }
}

// MARK: - Lifecycle: stop on stdin EOF or SIGINT

DispatchQueue.global().async {
    while readLine() != nil {}
    fputs("operator-audio-capture: stdin EOF — shutting down\n", stderr)
    if let s = micSession {
        s.stopRunning()
    }
    teardownSystemTap()
    fputs("operator-audio-capture: totals [S]=\(systemStats.bytes)B [M]=\(micStats.bytes)B\n", stderr)
    exit(0)
}

signal(SIGINT) { _ in
    fputs("operator-audio-capture: SIGINT — exiting\n", stderr)
    // Note: we don't call teardownSystemTap() here because we're inside a
    // signal handler — most CoreAudio teardown isn't async-signal-safe.
    // Exiting with an orphaned tap/aggregate is acceptable: macOS reaps
    // them on process exit. The Python parent owns the supervisor role.
    exit(0)
}

RunLoop.main.run()
