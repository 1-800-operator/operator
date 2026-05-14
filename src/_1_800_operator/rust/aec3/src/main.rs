// aec3 — operator's speaker-bleed cleaner. Wraps WebRTC AEC3 with operator's
// framing protocol. Two modes:
//
//   batch  (default): --mic <wav> --ref <wav> --out <wav>
//                     Reads two 16 kHz mono WAVs, runs AEC3, writes a cleaned WAV.
//                     Used for offline A/B verification against recorded sessions.
//
//   stream (--stream): Long-running process. Reads framed PCM on stdin,
//                     writes framed cleaned mic frames on stdout. Used by the
//                     AecCleaner subprocess manager in pipeline/aec_cleaner.py
//                     that wires the audio helper into the live AEC path.
//
// Stream-mode framing (input and output — identical layout, mic-only on output):
//   [1-byte tag: 'S' (0x53) = system/reference, 'M' (0x4D) = mic]
//   [4-byte big-endian uint32: payload length in bytes]
//   [N bytes: Float32 PCM, little-endian, 16 kHz mono]
//
// Stream-mode algorithm:
//   - Reference ('S') frames feed AEC3's render side immediately on arrival.
//   - Mic ('M') frames enter a 150 ms delay queue (2400 samples) before
//     feeding AEC3's capture side. This places render ~150 ms ahead of
//     capture inside AEC3, which lands inside its causal echo-search
//     window (63-500 ms) and compensates for SCStream's anti-causal
//     63 ms mic-leads-ref skew.
//   - Both sides are accumulated and drained in 10 ms (160-sample) frames,
//     matching AEC3's required frame size. The helper emits ~40 ms chunks.
//   - One cleaned mic frame is emitted per 10 ms AEC step (tag 'M').
//   - EOF on stdin → exit cleanly. Any leftover mic samples still inside
//     the delay queue are dropped (≤150 ms of trailing audio).

use std::collections::VecDeque;
use std::env;
use std::io::{self, BufReader, BufWriter, Read, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use hound::{SampleFormat, WavReader, WavSpec, WavWriter};
use webrtc_audio_processing::{
    Config, Processor,
    config::{EchoCanceller, Pipeline},
};

const SAMPLE_RATE_HZ: u32 = 16_000;
const FRAME_LEN: usize = (SAMPLE_RATE_HZ as usize) / 100; // 10 ms = 160 samples
const MIC_DELAY_MS: usize = 150;
const MIC_DELAY_SAMPLES: usize = (SAMPLE_RATE_HZ as usize) * MIC_DELAY_MS / 1000; // 2400

const TAG_SYSTEM: u8 = b'S';
const TAG_MIC: u8 = b'M';

// Sanity cap on per-frame payload. The helper emits ~5 KB chunks; anything
// past 1 MiB means the stream is corrupted (same cap as the Python reader).
const MAX_FRAME_BYTES: usize = 1 << 20;

#[derive(Debug)]
enum Mode {
    Batch { mic: PathBuf, reference: PathBuf, out: PathBuf },
    Stream,
}

fn parse_args() -> Result<Mode, String> {
    let mut mic = None;
    let mut reference = None;
    let mut out = None;
    let mut stream = false;

    let mut iter = env::args().skip(1);
    while let Some(a) = iter.next() {
        match a.as_str() {
            "--stream" => stream = true,
            "--mic" => mic = Some(PathBuf::from(iter.next().ok_or("missing value for --mic")?)),
            "--ref" => {
                reference = Some(PathBuf::from(iter.next().ok_or("missing value for --ref")?))
            }
            "--out" => out = Some(PathBuf::from(iter.next().ok_or("missing value for --out")?)),
            "-h" | "--help" => {
                return Err(usage());
            }
            other => return Err(format!("unknown arg: {other}\n\n{}", usage())),
        }
    }

    if stream {
        if mic.is_some() || reference.is_some() || out.is_some() {
            return Err(format!(
                "--stream takes no path args (uses stdin/stdout)\n\n{}",
                usage()
            ));
        }
        return Ok(Mode::Stream);
    }

    Ok(Mode::Batch {
        mic: mic.ok_or_else(|| format!("--mic required\n\n{}", usage()))?,
        reference: reference.ok_or_else(|| format!("--ref required\n\n{}", usage()))?,
        out: out.ok_or_else(|| format!("--out required\n\n{}", usage()))?,
    })
}

fn usage() -> String {
    "usage:\n  \
     aec3 --mic <mic.wav> --ref <ref.wav> --out <cleaned.wav>   (batch)\n  \
     aec3 --stream                                              (streaming via stdin/stdout)"
        .into()
}

/// Reads a 16 kHz mono WAV (PCM i16/i32 or float32) and returns samples as f32 in [-1,1].
fn read_wav_mono_16k(path: &PathBuf) -> Result<Vec<f32>, String> {
    let mut reader = WavReader::open(path).map_err(|e| format!("open {path:?}: {e}"))?;
    let spec = reader.spec();
    if spec.channels != 1 {
        return Err(format!("{path:?}: expected mono, got {} channels", spec.channels));
    }
    if spec.sample_rate != SAMPLE_RATE_HZ {
        return Err(format!(
            "{path:?}: expected {} Hz, got {} Hz",
            SAMPLE_RATE_HZ, spec.sample_rate
        ));
    }

    let samples: Vec<f32> = match (spec.sample_format, spec.bits_per_sample) {
        (SampleFormat::Int, 16) => reader
            .samples::<i16>()
            .map(|s| s.map(|v| v as f32 / 32768.0))
            .collect::<Result<_, _>>()
            .map_err(|e| format!("decode i16 {path:?}: {e}"))?,
        (SampleFormat::Int, 32) => reader
            .samples::<i32>()
            .map(|s| s.map(|v| v as f32 / 2_147_483_648.0))
            .collect::<Result<_, _>>()
            .map_err(|e| format!("decode i32 {path:?}: {e}"))?,
        (SampleFormat::Float, 32) => reader
            .samples::<f32>()
            .collect::<Result<_, _>>()
            .map_err(|e| format!("decode f32 {path:?}: {e}"))?,
        (fmt, bits) => {
            return Err(format!("{path:?}: unsupported format {fmt:?} {bits} bits",));
        }
    };

    Ok(samples)
}

fn write_wav_f32_16k(path: &PathBuf, samples: &[f32]) -> Result<(), String> {
    let spec = WavSpec {
        channels: 1,
        sample_rate: SAMPLE_RATE_HZ,
        bits_per_sample: 32,
        sample_format: SampleFormat::Float,
    };
    let mut w = WavWriter::create(path, spec).map_err(|e| format!("create {path:?}: {e}"))?;
    for &s in samples {
        w.write_sample(s).map_err(|e| format!("write {path:?}: {e}"))?;
    }
    w.finalize().map_err(|e| format!("finalize {path:?}: {e}"))?;
    Ok(())
}

fn build_processor() -> Result<Processor, String> {
    let processor =
        Processor::new(SAMPLE_RATE_HZ).map_err(|e| format!("processor init: {e}"))?;
    processor.set_config(Config {
        pipeline: Pipeline::default(),
        echo_canceller: Some(EchoCanceller::Full { stream_delay_ms: None }),
        ..Default::default()
    });
    Ok(processor)
}

fn run_batch(mic_path: &PathBuf, ref_path: &PathBuf, out_path: &PathBuf) -> Result<(), String> {
    eprintln!("reading mic: {mic_path:?}");
    let mic = read_wav_mono_16k(mic_path)?;
    eprintln!("reading ref: {ref_path:?}");
    let mut reference = read_wav_mono_16k(ref_path)?;

    let mic_len = mic.len();
    eprintln!(
        "mic samples: {} ({:.2} s); ref samples: {} ({:.2} s)",
        mic_len,
        mic_len as f32 / SAMPLE_RATE_HZ as f32,
        reference.len(),
        reference.len() as f32 / SAMPLE_RATE_HZ as f32,
    );

    if reference.len() < mic_len {
        reference.resize(mic_len, 0.0);
    } else if reference.len() > mic_len {
        reference.truncate(mic_len);
    }

    let processor = build_processor()?;

    let num_frames = mic_len / FRAME_LEN;
    let mut cleaned = Vec::with_capacity(mic_len);

    for f_idx in 0..num_frames {
        let start = f_idx * FRAME_LEN;
        let end = start + FRAME_LEN;

        let mut render: Vec<Vec<f32>> = vec![reference[start..end].to_vec()];
        processor
            .process_render_frame(&mut render)
            .map_err(|e| format!("process_render_frame frame {f_idx}: {e}"))?;

        let mut capture: Vec<Vec<f32>> = vec![mic[start..end].to_vec()];
        processor
            .process_capture_frame(&mut capture)
            .map_err(|e| format!("process_capture_frame frame {f_idx}: {e}"))?;

        cleaned.extend_from_slice(&capture[0]);
    }

    let leftover = mic_len - num_frames * FRAME_LEN;
    if leftover > 0 {
        cleaned.extend_from_slice(&mic[num_frames * FRAME_LEN..]);
    }

    assert_eq!(cleaned.len(), mic_len, "cleaned must match mic length");

    let stats = processor.get_stats();
    eprintln!("processed {} frames ({} samples)", num_frames, num_frames * FRAME_LEN);
    eprintln!("AEC stats: {stats:?}");

    write_wav_f32_16k(out_path, &cleaned)?;
    eprintln!("wrote {out_path:?}");

    Ok(())
}

/// Read exactly `buf.len()` bytes from `r`, or return Ok(false) on clean EOF
/// at a frame boundary. Partial reads (EOF mid-frame) return Ok(false) with
/// a diagnostic — the stream is over.
fn read_exact_or_eof<R: Read>(r: &mut R, buf: &mut [u8]) -> io::Result<bool> {
    let mut read = 0;
    while read < buf.len() {
        match r.read(&mut buf[read..]) {
            Ok(0) => return Ok(read == 0 && buf.is_empty()),
            Ok(n) => read += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
        if read == 0 {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Best-effort read of a header: returns Ok(None) on clean EOF before any
/// byte is read, Ok(Some(header)) on full 5-byte header, Err on partial or
/// I/O error.
fn read_header<R: Read>(r: &mut R) -> io::Result<Option<[u8; 5]>> {
    let mut header = [0u8; 5];
    let mut read = 0;
    while read < header.len() {
        match r.read(&mut header[read..]) {
            Ok(0) => {
                if read == 0 {
                    return Ok(None); // clean EOF between frames
                }
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    format!("EOF mid-header after {read} bytes"),
                ));
            }
            Ok(n) => read += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(Some(header))
}

/// Convert a slice of Float32 LE bytes into samples. Returns an error if the
/// byte count is not a multiple of 4.
fn decode_f32_le(bytes: &[u8]) -> Result<Vec<f32>, String> {
    if !bytes.len().is_multiple_of(4) {
        return Err(format!(
            "payload length {} is not a multiple of 4 (Float32)",
            bytes.len()
        ));
    }
    let mut out = Vec::with_capacity(bytes.len() / 4);
    for chunk in bytes.chunks_exact(4) {
        out.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
    }
    Ok(out)
}

/// Encode a frame `['M'][4-byte BE length][Float32 LE PCM]` to `w`. The
/// caller must flush.
fn write_mic_frame<W: Write>(w: &mut W, samples: &[f32]) -> io::Result<()> {
    let byte_len = samples.len().checked_mul(4).expect("frame too large");
    let mut header = [0u8; 5];
    header[0] = TAG_MIC;
    header[1..5].copy_from_slice(&(byte_len as u32).to_be_bytes());
    w.write_all(&header)?;
    // Encode Float32 LE inline to avoid an extra allocation per frame.
    let mut buf = [0u8; 4];
    for &s in samples {
        buf.copy_from_slice(&s.to_le_bytes());
        w.write_all(&buf)?;
    }
    Ok(())
}

fn run_stream() -> Result<(), String> {
    eprintln!(
        "aec3: stream mode (mic delay {MIC_DELAY_MS} ms = {MIC_DELAY_SAMPLES} samples)"
    );

    let processor = build_processor()?;

    let stdin = io::stdin();
    let mut stdin = BufReader::new(stdin.lock());
    let stdout = io::stdout();
    let mut stdout = BufWriter::new(stdout.lock());

    // Per-side sample accumulators. Ref drains immediately in 10 ms frames;
    // mic uses the delay queue described above.
    let mut ref_acc: Vec<f32> = Vec::with_capacity(FRAME_LEN * 8);
    let mut mic_delay: VecDeque<f32> = VecDeque::with_capacity(MIC_DELAY_SAMPLES + FRAME_LEN * 8);

    let mut frames_render: u64 = 0;
    let mut frames_capture: u64 = 0;
    let mut saw_first_ref = false;
    let mut saw_first_mic = false;

    loop {
        let header = match read_header(&mut stdin) {
            Ok(Some(h)) => h,
            Ok(None) => break, // clean EOF
            Err(e) => {
                eprintln!("aec3: read header failed: {e}");
                break;
            }
        };
        let tag = header[0];
        let length = u32::from_be_bytes([header[1], header[2], header[3], header[4]]) as usize;
        if length == 0 || length > MAX_FRAME_BYTES {
            return Err(format!("bogus frame length {length} (tag {tag:#x})"));
        }

        let mut payload = vec![0u8; length];
        if !read_exact_or_eof(&mut stdin, &mut payload).map_err(|e| format!("read payload: {e}"))?
        {
            eprintln!("aec3: EOF mid-payload — stream truncated");
            break;
        }
        let samples = decode_f32_le(&payload)?;

        match tag {
            TAG_SYSTEM => {
                if !saw_first_ref {
                    eprintln!("aec3: first ref frame ({} samples)", samples.len());
                    saw_first_ref = true;
                }
                ref_acc.extend_from_slice(&samples);
                // Drain ref in 10 ms increments → render side.
                while ref_acc.len() >= FRAME_LEN {
                    let frame: Vec<f32> = ref_acc.drain(..FRAME_LEN).collect();
                    let mut render: Vec<Vec<f32>> = vec![frame];
                    processor
                        .process_render_frame(&mut render)
                        .map_err(|e| format!("process_render_frame: {e}"))?;
                    frames_render += 1;
                }
            }
            TAG_MIC => {
                if !saw_first_mic {
                    eprintln!("aec3: first mic frame ({} samples)", samples.len());
                    saw_first_mic = true;
                }
                mic_delay.extend(samples.iter().copied());
                // Hold MIC_DELAY_SAMPLES back; drain anything older than that
                // in 10 ms increments → capture side. We require at least
                // MIC_DELAY_SAMPLES + FRAME_LEN buffered before emitting the
                // first capture frame, so the oldest frame we pop sits exactly
                // ~150 ms after it entered the buffer.
                while mic_delay.len() >= MIC_DELAY_SAMPLES + FRAME_LEN {
                    let mut frame: Vec<f32> = Vec::with_capacity(FRAME_LEN);
                    for _ in 0..FRAME_LEN {
                        // SAFETY: length checked above
                        frame.push(mic_delay.pop_front().unwrap());
                    }
                    let mut capture: Vec<Vec<f32>> = vec![frame];
                    processor
                        .process_capture_frame(&mut capture)
                        .map_err(|e| format!("process_capture_frame: {e}"))?;
                    frames_capture += 1;
                    write_mic_frame(&mut stdout, &capture[0])
                        .map_err(|e| format!("write mic frame: {e}"))?;
                }
                // Flush at the natural ~40 ms helper-chunk boundary so the
                // downstream consumer sees frames promptly rather than
                // waiting on BufWriter's default buffer fill.
                stdout.flush().map_err(|e| format!("flush stdout: {e}"))?;
            }
            other => {
                eprintln!("aec3: unknown tag {other:#x} — dropping {length}B");
                continue;
            }
        }
    }

    let _ = stdout.flush();
    let stats = processor.get_stats();
    eprintln!(
        "aec3: EOF — render frames {frames_render}, capture frames {frames_capture}, mic_delay leftover {} samples",
        mic_delay.len()
    );
    eprintln!("aec3: AEC stats: {stats:?}");

    Ok(())
}

fn run() -> Result<(), String> {
    match parse_args()? {
        Mode::Batch { mic, reference, out } => run_batch(&mic, &reference, &out),
        Mode::Stream => run_stream(),
    }
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}
