"""Record synthetic counting WAVs and run production VAD, without live-service writes.

PowerShell: .\\.venv\\Scripts\\python.exe tests\\captioner_counting_harness.py generate
PowerShell: .\\.venv\\Scripts\\python.exe tests\\captioner_counting_harness.py vad
PowerShell: .\\.venv\\Scripts\\python.exe tests\\captioner_counting_harness.py native
PowerShell: .\\.venv\\Scripts\\python.exe tests\\captioner_counting_harness.py summarize
PowerShell: .\\.venv\\Scripts\\python.exe tests\\captioner_counting_harness.py compare
PowerShell: .\\.venv\\Scripts\\python.exe tests\\captioner_counting_harness.py replay
PowerShell: .\\.venv\\Scripts\\python.exe tests\\captioner_counting_harness.py position
All generated files stay under collab_state\\captioner_counting_tests.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
ARTIFACTS = ROOT / "collab_state" / "captioner_counting_tests"
RUNTIME = ROOT / "src" / "ws_collab" / "captioner" / "captioner_runtime.js"
TRANSCRIPT = ROOT / "src" / "ws_collab" / "admin" / "transcript_runtime.js"
RATE = 16000
WORDS = ["one", "two", "three", "four", "five", "six"]
CASES = [
    ("one_to_five_2s", WORDS[:5], [2000] * 4),
    ("one_to_six_mixed", WORDS, [80, 240, 1000, 2000, 4000]),
    ("phrases_4s", ["first_phrase", "second_phrase"], [4000]),
    ("continuous_control", ["continuous"], []),
]


def save_json(file: Path, data) -> None:
    file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


def read_pcm(file: Path) -> array:
    with wave.open(str(file), "rb") as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"Expected {RATE}Hz mono PCM16: {file}")
        values = array("h", reader.readframes(reader.getnframes()))
    if sys.byteorder != "little":
        values.byteswap()
    return values


def write_pcm(file: Path, values: array) -> None:
    payload = array("h", values)
    if sys.byteorder != "little":
        payload.byteswap()
    with wave.open(str(file), "wb") as writer:
        writer.setparams((1, 2, RATE, len(values), "NONE", "not compressed"))
        writer.writeframes(payload.tobytes())


def quiet_bounds(values: array) -> tuple[int, int]:
    """Independent fixed-threshold 10ms RMS proxy, not word timestamps."""
    bins = []
    width = RATE // 100
    for start in range(0, len(values), width):
        block = values[start:start + width]
        rms = math.sqrt(sum(v * v for v in block) / len(block)) / 32768
        bins.append((start, min(start + width, len(values)), rms))
    first = next(start for start, _, rms in bins if rms >= 0.008)
    last = next(end for _, end, rms in reversed(bins) if rms >= 0.0056)
    return first, last


def generate(voice: str) -> dict:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    source_dir = ARTIFACTS / "sources"
    source_dir.mkdir(exist_ok=True)
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(TESTS / "captioner_counting_tts.ps1"),
                    "-OutputDirectory", str(source_dir), "-Voice", voice], check=True, timeout=120)
    synthesis = json.loads((source_dir / "synthesis.json").read_text(encoding="utf-8-sig"))
    sources, trimmed = {}, {}
    for spec in synthesis["sources"]:
        file = source_dir / (spec["id"] + ".wav")
        raw = read_pcm(file)
        active = [i for i, sample in enumerate(raw) if abs(sample) > 64]
        start, end = active[0], active[-1] + 1
        values = raw[start:end]
        first, last = quiet_bounds(values)
        trimmed[spec["id"]] = values
        sources[spec["id"]] = {
            "text": spec["text"], "wav": str(file.relative_to(ARTIFACTS)),
            "sha256": sha256(file), "raw_samples": len(raw),
            "crop_start_sample": start, "crop_end_sample_exclusive": end,
            "removed_leading_samples": start, "removed_trailing_samples": len(raw) - end,
            "retained_samples": len(values),
            "quiet_proxy_first_active_sample": first, "quiet_proxy_last_active_end_sample": last,
        }
    cases = []
    for case_id, ids, gaps_ms in CASES:
        pcm = array("h", [0]) * (RATE // 2)
        spans, gaps = [], []
        for index, source_id in enumerate(ids):
            source = sources[source_id]
            start = len(pcm)
            pcm.extend(trimmed[source_id])
            spans.append({"source_id": source_id, "text": source["text"],
                          "start_sample": start, "end_sample_exclusive": len(pcm),
                          "source_crop_start_sample": source["crop_start_sample"],
                          "source_crop_end_sample_exclusive": source["crop_end_sample_exclusive"]})
            if index < len(gaps_ms):
                gap_samples = gaps_ms[index] * RATE // 1000
                gap_start = len(pcm)
                pcm.extend(array("h", [0]) * gap_samples)
                next_source = sources[ids[index + 1]]
                quiet_start = start + source["quiet_proxy_last_active_end_sample"]
                quiet_end = len(pcm) + next_source["quiet_proxy_first_active_sample"]
                gaps.append({"after": source_id, "before": ids[index + 1],
                             "start_sample": gap_start, "end_sample_exclusive": len(pcm),
                             "sample_count": gap_samples, "duration_ms": gaps_ms[index],
                             "start_ms": gap_start * 1000 / RATE, "end_ms": len(pcm) * 1000 / RATE,
                             "quiet_proxy": {
                                 "start_ms": quiet_start * 1000 / RATE,
                                 "end_ms": quiet_end * 1000 / RATE,
                                 "duration_ms": (quiet_end - quiet_start) * 1000 / RATE,
                                 "left_tail_ms": (gap_start - quiet_start) * 1000 / RATE,
                                 "right_head_ms": (quiet_end - len(pcm)) * 1000 / RATE,
                             }})
        pcm.extend(array("h", [0]) * RATE)
        for gap in gaps:
            left, right = gap["start_sample"], gap["end_sample_exclusive"]
            while left > 0 and pcm[left - 1] == 0:
                left -= 1
            while right < len(pcm) and pcm[right] == 0:
                right += 1
            gap["contiguous_digital_zero_span"] = {
                "start_sample": left, "end_sample_exclusive": right,
                "duration_ms": (right - left) * 1000 / RATE,
            }
        file = ARTIFACTS / (case_id + ".wav")
        write_pcm(file, pcm)
        cases.append({"id": case_id, "wav": file.name, "sha256": sha256(file),
                      "sample_count": len(pcm), "duration_seconds": len(pcm) / RATE,
                      "leading_padding_samples": RATE // 2, "trailing_padding_samples": RATE,
                      "speech_spans": spans, "gaps": gaps})
    manifest = {
        "schema_version": 1, "sample_rate": RATE, "channels": 1, "sample_width_bytes": 2,
        "synthesis": synthesis,
        "trimming_policy": "Keep first through last PCM sample with abs(value)>64; no fade or normalization. Preserve raw source WAVs.",
        "gap_policy": "Exact integer PCM zeros inserted AFTER cropping. Half-open sample indices. TTS quiet tails are separate.",
        "quiet_proxy_policy": "10ms RMS bins relative to each cropped source; first >=0.008, last >=0.0056. Acoustic quiet proxy only, not a speech/word timestamp or exact inserted gap.",
        "sources": sources, "cases": cases,
    }
    save_json(ARTIFACTS / "manifest.json", manifest)
    return manifest


def run_vad(runtime: Path) -> dict:
    destination = ARTIFACTS / "vad_results.json"
    subprocess.run(["node", str(TESTS / "captioner_counting_vad.js"),
                    str(ARTIFACTS / "manifest.json"), str(runtime), str(destination)],
                   check=True, timeout=30)
    report = json.loads(destination.read_text(encoding="utf-8"))
    for case in report["cases"]:
        compare = case["comparison"]
        print(case["id"], "expected", [m["expected_ms"] for m in compare["matches"]],
              "detected", [m.get("detected_ms") for m in compare["matches"]],
              "missed", compare["missed_count"], "merged", len(compare["merged"]),
              "out_of_tolerance", compare["out_of_tolerance_count"],
              "extra", len(compare["extra_pauses"]))
    return report


def run_comparison(runtime: Path, manifest: Path, output: Path, config: Path | None) -> dict:
    command = ["node", str(TESTS / "captioner_counting_compare.js"), str(manifest), str(runtime), str(output)]
    if config:
        command.append(str(config))
    subprocess.run(command, check=True, timeout=120)
    for flag, name in [("--schema", "comparison_schema.json"), ("--config-template", "comparison_default_config.json")]:
        completed = subprocess.run(["node", str(TESTS / "captioner_counting_compare.js"), flag],
                                   capture_output=True, text=True, check=True, timeout=10)
        save_json(ARTIFACTS / name, json.loads(completed.stdout))
    return json.loads(output.read_text(encoding="utf-8"))


def run_replay(runtime: Path, transcript: Path, baseline: Path | None, native_report: Path | None) -> dict:
    from datetime import datetime, timezone
    import shutil
    if baseline is None:
        baseline = sorted(ARTIFACTS.glob("baseline_*"))[0]
    if native_report is None:
        native_report = Path(json.loads((ARTIFACTS / "native_latest.json").read_text(encoding="utf-8"))["report"])
    destination = ARTIFACTS / ("replay_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ"))
    destination.mkdir()
    shutil.copyfile(runtime, destination / "captioner_runtime.js")
    shutil.copyfile(transcript, destination / "transcript_runtime.js")
    report = destination / "replay_results.json"
    subprocess.run(["node", str(TESTS / "captioner_counting_replay.js"), str(native_report),
                    str(baseline / "captioner_runtime.js"), str(baseline / "transcript_runtime.js"),
                    str(destination / "captioner_runtime.js"), str(destination / "transcript_runtime.js"),
                    str(report)], check=True, timeout=30)
    save_json(ARTIFACTS / "replay_latest.json", {"report": str(report)})
    return json.loads(report.read_text(encoding="utf-8"))


def run_position(runtime: Path, transcript: Path, baseline: Path | None, native_report: Path | None,
                 manifest: Path) -> dict:
    from datetime import datetime, timezone
    import shutil
    if baseline is None:
        baseline = sorted(ARTIFACTS.glob("baseline_*"))[0]
    if native_report is None:
        native_report = Path(json.loads((ARTIFACTS / "native_latest.json").read_text(encoding="utf-8"))["report"])
    destination = ARTIFACTS / ("position_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ"))
    destination.mkdir()
    shutil.copyfile(runtime, destination / "captioner_runtime.js")
    shutil.copyfile(transcript, destination / "transcript_runtime.js")
    report = destination / "position_results.json"
    subprocess.run(["node", str(TESTS / "captioner_counting_position.js"), str(manifest), str(native_report),
                    str(baseline / "captioner_runtime.js"), str(baseline / "transcript_runtime.js"),
                    str(destination / "captioner_runtime.js"), str(destination / "transcript_runtime.js"),
                    str(report)], check=True, timeout=30)
    save_json(ARTIFACTS / "position_latest.json", {"report": str(report)})
    return json.loads(report.read_text(encoding="utf-8"))


def summarize() -> dict:
    manifest = json.loads((ARTIFACTS / "manifest.json").read_text(encoding="utf-8"))
    vad = json.loads((ARTIFACTS / "vad_results.json").read_text(encoding="utf-8"))
    result = {
        "voice": manifest["synthesis"]["voice"],
        "sample_rate": manifest["sample_rate"],
        "runtime_sha256": vad["runtime_sha256"],
        "baseline_snapshots": [{
            "directory": str(directory),
            "files": {file.name: sha256(file) for file in directory.glob("*.js")},
        } for directory in sorted(ARTIFACTS.glob("baseline_*")) if directory.is_dir()],
        "cases": [{
            "id": case["id"],
            "exact_inserted_ms": [gap["duration_ms"] for gap in case["gaps"]],
            "acoustic_quiet_proxy_ms": [gap["quiet_proxy"]["duration_ms"] for gap in case["gaps"]],
            "vad": next(item["comparison"] for item in vad["cases"] if item["id"] == case["id"]),
        } for case in manifest["cases"]],
        "interpretation": {
            "exactness": "Only inserted zero samples are exact gap ground truth. VAD also includes TTS acoustic tails and 20ms frame quantization.",
            "tolerance": "60ms per independently measured 10ms RMS quiet-proxy endpoint; raw insertion errors and extra intra-speech pauses are retained.",
            "native_scope": "Real Chrome ASR and production BrowserVadCapture/AudioWorklet, injected shared synthetic track. A local handler mirrors per-result association/rendering; not a full captioner-page/backend test.",
            "no_word_timestamps": "Native ASR result times are receipt times. No synthesized word-to-ASR timestamp alignment is asserted.",
        },
        "rerun": [
            r".\.venv\Scripts\python.exe tests\captioner_counting_harness.py generate",
            r".\.venv\Scripts\python.exe tests\captioner_counting_harness.py vad",
            r".\.venv\Scripts\python.exe tests\captioner_counting_harness.py native",
            r".\.venv\Scripts\python.exe tests\captioner_counting_harness.py compare",
            r".\.venv\Scripts\python.exe tests\captioner_counting_harness.py replay",
            r".\.venv\Scripts\python.exe tests\captioner_counting_harness.py position",
            r".\.venv\Scripts\python.exe tests\captioner_counting_harness.py summarize",
            r".\.venv\Scripts\python.exe -m pytest tests\test_captioner_counting_audio.py -q -p no:cacheprovider",
        ],
    }
    comparison_file = ARTIFACTS / "comparison_results.json"
    if comparison_file.exists():
        comparison = json.loads(comparison_file.read_text(encoding="utf-8"))
        result["bounded_vad_comparison"] = {
            "report": str(comparison_file),
            "runtime_sha256": comparison["runtime_sha256"],
            "config": comparison["config"],
            "by_gain": comparison["by_gain"],
            "caveats": comparison["caveats"],
            "reusable_cli": r"node tests\captioner_counting_compare.js MANIFEST RUNTIME OUTPUT [CONFIG_JSON]",
            "schema_file": str(ARTIFACTS / "comparison_schema.json"),
            "config_template_file": str(ARTIFACTS / "comparison_default_config.json"),
        }
    natural_comparison_file = ARTIFACTS / "natural_comparison_results.json"
    if natural_comparison_file.exists():
        natural = json.loads(natural_comparison_file.read_text(encoding="utf-8"))
        result["recorded_human_vad_comparison"] = {
            "report": str(natural_comparison_file),
            "manifest": natural["manifest_file"], "manifest_sha256": natural["manifest_sha256"],
            "runtime_sha256": natural["runtime_sha256"], "by_gain": natural["by_gain"],
            "case_distributions": [{ "variant_id": experiment["variant_id"], "gain": experiment["gain"],
                "cases": [{"id": case["id"], "distribution": case.get("pause_distribution")}
                          for case in experiment["cases"]]} for experiment in natural["experiments"]],
            "warning": "Silence is unlabelled in these originals. Counts/distributions are detector sensitivity observations, not precision, recall, timing accuracy or a best-configuration ranking.",
        }
    natural_inserted_file = ARTIFACTS / "natural_inserted_comparison_results.json"
    if natural_inserted_file.exists():
        natural_inserted = json.loads(natural_inserted_file.read_text(encoding="utf-8"))
        result["recorded_human_exact_insertion_comparison"] = {
            "report": str(natural_inserted_file),
            "manifest": natural_inserted["manifest_file"],
            "runtime_sha256": natural_inserted["runtime_sha256"],
            "fixtures": natural_inserted["fixtures"], "by_gain": natural_inserted["by_gain"],
            "warning": "Only inserted zero spans are exact. Surrounding quiet envelopes are independent energy proxies; original within-utterance pauses remain unlabelled.",
        }
    replay_latest = ARTIFACTS / "replay_latest.json"
    if replay_latest.exists():
        replay_path = Path(json.loads(replay_latest.read_text(encoding="utf-8"))["report"])
        replay = json.loads(replay_path.read_text(encoding="utf-8"))
        result["offline_native_replay"] = {
            "report": str(replay_path), "summary": replay["summary"], "inputs": replay["inputs"],
            "limitations": replay["limitations"],
        }
    position_latest = ARTIFACTS / "position_latest.json"
    if position_latest.exists():
        position_path = Path(json.loads(position_latest.read_text(encoding="utf-8"))["report"])
        position = json.loads(position_path.read_text(encoding="utf-8"))
        result["retention_vs_word_placement"] = {
            "report": str(position_path),
            "strategies": [{"strategy": experiment["strategy"], "metrics": experiment["metrics"]}
                           for experiment in position["experiments"]],
            "caveats": position["caveats"],
        }
    latest = ARTIFACTS / "native_latest.json"
    if latest.exists():
        native_path = Path(json.loads(latest.read_text(encoding="utf-8"))["report"])
        native = json.loads(native_path.read_text(encoding="utf-8"))
        result["native"] = {
            "report": str(native_path), "browser": native.get("browser"),
            "runtime_sha256": native["runtime_sha256"],
            "cleanup": native["cleanup"], "comparisons": native.get("comparisons"),
            "cases": [{key: case.get(key) for key in ["case_id", "outcome", "summary", "errors", "audio_graph"]}
                      for case in native["cases"]],
        }
        evidence = []
        for case in native["cases"]:
            observations = case["observations"]
            for index, observation in enumerate(observations):
                if observation["type"] != "asr_full_result_event":
                    continue
                rows = observation["results"]
                interims = [row for row in rows if not row["is_final"]]
                if len(interims) < 2:
                    continue
                revisions = []
                for item in observations[index + 1:]:
                    if item["type"] == "asr_full_result_event":
                        break
                    if item["type"] == "asr_revision":
                        revisions.append(item)
                if revisions:
                    evidence.append({
                        "case": case["case_id"], "elapsed_ms": observation["elapsed_ms"],
                        "google_interim_array": [row["alternatives"][0]["transcript"] for row in interims],
                        "last_single_tail_text": revisions[-1]["text"],
                        "last_rendered_text": revisions[-1]["rendered_text"],
                        "metadata_durations_ms": [p["duration_ms"] for p in revisions[-1]["metadata"]["pauses"]],
                    })
        result["native_interim_split_evidence"] = evidence
        result["interpretation"]["observed_handler_behavior"] = (
            "Google repeatedly repartitions one interim into multiple results. The baseline per-result single-tail handler shows only the last suffix, "
            "and moves/merges markers onto it; this can appear as disappearing history. Check each interval's reached_final and duration_changed values for preservation. "
            "A removal from one reused result index is not global pause loss. Initial ASR latency also left some pauses without a word prefix."
        ) if evidence else "No multi-interim split was captured in this run."
    save_json(ARTIFACTS / "summary.json", result)
    print("Saved", ARTIFACTS / "summary.json")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["generate", "vad", "native", "summarize", "compare", "replay", "position"])
    parser.add_argument("--voice", default="Microsoft David Desktop")
    parser.add_argument("--runtime", type=Path, default=RUNTIME)
    parser.add_argument("--transcript", type=Path, default=TRANSCRIPT)
    parser.add_argument("--cdp-port", type=int, default=9224)
    parser.add_argument("--manifest", type=Path, default=ARTIFACTS / "manifest.json")
    parser.add_argument("--comparison-output", type=Path, default=ARTIFACTS / "comparison_results.json")
    parser.add_argument("--comparison-config", type=Path)
    parser.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--native-report", type=Path)
    parser.add_argument("--artifact-dir", type=Path, help="Native-only output directory inside this repository")
    args = parser.parse_args()
    if args.command == "generate":
        result = generate(args.voice)
        print("Saved", len(result["cases"]), "real TTS WAV cases to", ARTIFACTS)
    elif args.command == "vad":
        run_vad(args.runtime.resolve())
    elif args.command == "summarize":
        summarize()
    elif args.command == "compare":
        run_comparison(args.runtime.resolve(), args.manifest.resolve(), args.comparison_output.resolve(),
                       args.comparison_config.resolve() if args.comparison_config else None)
    elif args.command == "replay":
        run_replay(args.runtime.resolve(), args.transcript.resolve(), args.baseline_dir, args.native_report)
    elif args.command == "position":
        run_position(args.runtime.resolve(), args.transcript.resolve(), args.baseline_dir, args.native_report,
                     args.manifest.resolve())
    else:
        from captioner_counting_native import run_native
        run_native(args.runtime.resolve(), args.transcript.resolve(), args.cdp_port, args.manifest.resolve(),
                   args.artifact_dir.resolve() if args.artifact_dir else None)


if __name__ == "__main__":
    main()
