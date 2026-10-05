# WhisperX

WhisperX (`kubernetes/apps/media/whisperx`) is a second instance of the `whisper-asr-webservice`
image that `whisper` runs, with `ASR_ENGINE=whisperx`. It serves the same `/asr` and
`/detect-language` API at `http://whisperx.media.svc.cluster.local:9000`, so Bazarr's whisperai
provider can be pointed at either one to compare subtitles. WhisperX runs the same `large-v3`
transcription and then a forced-alignment pass for word timing.

## The VAD checkpoint workaround

Image v1.10.0 pairs lightning 2.6.5 with pyannote-audio 3.4.0. Lightning 2.6 stopped forcing
`weights_only=False` when it loads a checkpoint, and torch has defaulted to `True` since 2.6, so
the pyannote VAD checkpoint that WhisperX loads at start fails with `UnpicklingError` and the pod
crash-loops (upstream issue ahmetoner/whisper-asr-webservice#383). This was reproduced against the
pinned digest.

`TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1` is the workaround documented in that issue. It applies to
every `torch.load` in the process, not only the VAD checkpoint. The process loads only models from
fixed upstream sources. Remove it once an image ships pyannote-audio 4.0.3 or later, which fixes the
load (pyannote/pyannote-audio#1962).

## No idle unload

`whisper` sets `MODEL_IDLE_TIMEOUT` to free its VRAM between jobs. This instance cannot: in v1.10.0
the whisperx engine keeps its models in a dict, the idle unload sets that dict to `None`, and the
next request then fails assigning into it. The instance holds its VRAM for the life of the pod.

## Known limitations

- **Translation timing.** On `task=translate` the engine still force-aligns the English output
  against the source language's alignment model. WhisperX's own pipeline skips alignment for
  translation for this reason, so translated cue timing is suspect.
- **Alignment models accumulate.** The engine loads one alignment model per source language onto
  the GPU and never evicts them. A pod restart clears them.
- **Subtitle text.** Upstream issue #309 reports WhisperX SRT output with spaces removed, seen on
  Japanese. Check English and other spaced languages before relying on it.
