"use strict";

window.runCountingCase = async function(test) {
  const R = window.WsCaptionerRuntime;
  const T = window.WsCollabTranscript;
  const clone = value => JSON.parse(JSON.stringify(value));
  const observations = [];
  const association = new R.PauseAssociation(
    typeof T.silencePosition === "function" ? {positionMapper: T.silencePosition} : {}
  );
  const finals = [];
  const finalized = new Set();
  const previous = new Map();
  let previousFull = "";
  const render = document.createElement("div");
  const tail = new T.LiveTranscriptTail(document, render);
  const origin = performance.now();
  let playbackOrigin = null;
  const log = (type, data = {}) => {
    const item = {type, elapsed_ms: performance.now() - origin, wall_ms: Date.now(), ...data};
    observations.push(item);
    return item;
  };
  const result = window.countingCurrent = {
    case_id: test.id, observations, frames: [], errors: [],
    source_kind: test.source_kind || "unspecified",
    expected_text: test.expected_text || test.reference_text || null,
    positioning: {mapper_injected: typeof T.silencePosition === "function",
      revision_retention_available: typeof T.reviseCaption === "function",
      recognition_updates_injected: typeof R.recognitionUpdates === "function"},
    safeguards: {synthetic_only: test.source_kind === "synthetic_tts",
      prerecorded_audio_only: true, microphone_api_called: false, backend_posts: 0,
      recognition_continuous: true, sentence_segmentation_forced: false},
  };
  // Any accidental native microphone fallback is an explicit test failure.
  Object.defineProperty(navigator.mediaDevices, "getUserMedia", {
    configurable: true, value: async () => {
      result.safeguards.microphone_api_called = true;
      throw new Error("Real microphone forbidden in this test");
    },
  });
  const producer = new AudioContext({sampleRate: 48000});
  await producer.resume();
  const sink = producer.createMediaStreamDestination();
  const track = sink.stream.getAudioTracks()[0];
  const source = producer.createBufferSource();
  source.buffer = await producer.decodeAudioData(await (await fetch(test.audio_url || `/audio/${test.wav}`)).arrayBuffer());
  source.connect(sink);
  const status = {value: null};
  let lastState = null;
  const capture = new R.BrowserVadCapture({
    mediaDevices: {getUserMedia: async () => sink.stream},
    AudioContextClass: AudioContext,
    workletUrl: "/captioner_runtime.js",
    onEvents(events) {
      for (const event of events) {
        if (event.type === "speech_end") association.silenceStarted(event.at);
        if (event.type === "pause") association.record(event);
        log("vad_event", {event: clone(event)});
      }
    },
    onStatus(value) {
      status.value = value;
      const key = `${value.state}|${value.available}|${value.error}`;
      if (key !== lastState) {
        log("vad_status", {status: clone(value)});
        lastState = key;
      }
    },
  });
  const originalSample = capture.sampleFrame.bind(capture);
  capture.sampleFrame = function(frame, epoch) {
    const receipt = performance.now();
    originalSample(frame, epoch);
    result.frames.push({...frame, receipt_performance_ms: receipt,
      mapped_sample_performance_ms: capture.lastSampleMs});
  };
  let recognizer = null, ended = false, sourceStarted = false;
  try {
    if (!await capture.start("synthetic-native")) throw new Error(capture.error || "Capture failed");
    result.audio_graph = {
      producer_sample_rate: producer.sampleRate, capture_sample_rate: capture.context.sampleRate,
      decoded_sample_rate: source.buffer.sampleRate,
      decoded_duration_seconds: source.buffer.duration,
      shared_track_id: track.id,
      identical_recognition_vad_track: track === capture.stream.getAudioTracks()[0],
      capture_output_gain: capture.output.gain.value,
      source_connected_only_to_media_stream_destination: true,
    };
    if (!result.audio_graph.identical_recognition_vad_track || capture.output.gain.value !== 0) {
      throw new Error("Synthetic shared-track/muted-output invariant failed");
    }
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (Recognition) {
      recognizer = new Recognition();
      recognizer.continuous = true;
      recognizer.interimResults = true;
      recognizer.lang = "en-US";
      recognizer.maxAlternatives = 1;
      for (const name of ["start", "audiostart", "soundstart", "speechstart", "speechend",
        "soundend", "audioend", "nomatch"]) recognizer[`on${name}`] = () => log(`asr_${name}`);
      recognizer.onerror = event => {
        result.errors.push({error: event.error, message: event.message});
        log("asr_error", {error: event.error, message: event.message});
      };
      recognizer.onend = () => { ended = true; log("asr_end"); };
      recognizer.onresult = event => {
        const rows = Array.from(event.results, (row, index) => ({
          index, is_final: row.isFinal,
          alternatives: Array.from(row, alternative => ({
            transcript: alternative.transcript, confidence: alternative.confidence,
          })),
        }));
        const joined = rows.map(row => row.alternatives[0]?.transcript.trim() || "").join(" ");
        log("asr_full_result_event", {result_index: event.resultIndex, results: rows,
          joined_transcript: joined, previous_joined_transcript: previousFull,
          entire_text_replacement: Boolean(previousFull && joined !== previousFull && !joined.startsWith(previousFull)),
          interim_result_count: rows.filter(row => !row.is_final).length});
        previousFull = joined;
        const updates = typeof R.recognitionUpdates === "function" ? R.recognitionUpdates(event)
          : rows.filter(row => row.index >= event.resultIndex).map(row => ({
            index: row.index, isFinal: row.is_final, text: row.alternatives[0]?.transcript || "",
          }));
        for (const update of updates) {
          const i = update.index;
          if (finalized.has(i)) continue;
          const row = {isFinal: update.isFinal}, text = String(update.text || "").trim();
          if (!text) continue;
          const utterance = `${test.id}:${i}`;
          const before = previous.get(i);
          association.updateTranscript(text, utterance);
          const snapshot = association.snapshot(text, {reserve: row.isFinal});
          const data = {engine: "browser_captioner", raw_text: text, is_final: row.isFinal,
            session_id: "isolated-synthetic-test", utterance_id: utterance, ...snapshot.metadata};
          const rowEvent = {type: row.isFinal ? "STT_FINAL_RESULT" : "STT_PARTIAL_RESULT", data,
            ts: new Date().toISOString(), source_id: "browser_captioner"};
          if (row.isFinal) finals.push(tail.finish(rowEvent) || T.finalCaption(rowEvent));
          T.renderTranscript(document, render, finals);
          if (!row.isFinal) {
            tail.setInterim(rowEvent);
          }
          tail.observe(status.value);
          tail.update(finals.at(-1) || null);
          const metadata = clone(snapshot.metadata);
          const key = pause => `${pause.start_at}|${pause.end_at}`;
          const nowKeys = new Map(metadata.pauses.map(pause => [key(pause), pause]));
          log("asr_revision", {
            result_index: i, text, is_final: row.isFinal,
            replacement: Boolean(before && before.text !== text && !text.startsWith(before.text)),
            previous_text: before?.text || null, metadata,
            removed_pause_keys: before ? before.metadata.pauses.filter(p => !nowKeys.has(key(p))).map(key) : [],
            changed_pause_durations: before ? before.metadata.pauses.filter(p =>
              nowKeys.has(key(p)) && nowKeys.get(key(p)).duration_ms !== p.duration_ms).map(key) : [],
            moved_pause_positions: before ? before.metadata.pauses.filter(p =>
              nowKeys.has(key(p)) && nowKeys.get(key(p)).after_char !== p.after_char).map(p => ({
                key: key(p), before: p.after_char, after: nowKeys.get(key(p)).after_char,
                alignment: nowKeys.get(key(p)).alignment,
              })) : [],
            rendered_text: render.textContent,
            displayed_caption: clone(row.isFinal ? finals.at(-1) : tail.interim),
            rendered_markers: [...render.querySelectorAll(".silence-marker")].filter(n => !n.hidden)
              .map(node => ({text: node.textContent, label: node.getAttribute("aria-label")})),
          });
          previous.set(i, {text, metadata});
          if (row.isFinal) {
            association.commit(snapshot);
            finalized.add(i);
          }
        }
      };
      log("asr_start_requested", {track_id: track.id, ready_state: track.readyState});
      // Never call start() without the explicitly shared synthetic track.
      recognizer.start(track);
      await new Promise(resolve => setTimeout(resolve, 650));
    } else {
      result.errors.push({error: "SpeechRecognition unavailable"});
    }
    const endedPromise = new Promise(resolve => { source.onended = resolve; });
    const timestamp = producer.getOutputTimestamp();
    const startAt = producer.currentTime + 0.1;
    playbackOrigin = timestamp.performanceTime > 0
      ? timestamp.performanceTime + (startAt - timestamp.contextTime) * 1000
      : performance.now() + (startAt - producer.currentTime) * 1000;
    result.playback = {
      source_start_audio_seconds: startAt, source_start_performance_ms_estimate: playbackOrigin,
      wall_minus_performance_ms: Date.now() - performance.now(),
      output_timestamp: timestamp, timestamp_is_not_word_alignment: true,
    };
    log("playback_start_scheduled", result.playback);
    source.start(startAt);
    sourceStarted = true;
    await endedPromise;
    log("playback_end");
    // Give native continuous recognition its own silence/finalization opportunity.
    await new Promise(resolve => setTimeout(resolve, 6000));
    if (recognizer && !ended) {
      log("asr_stop_after_entire_case", {reason: "Only after completed WAV plus 6s drain; no sentence splitting"});
      recognizer.stop();
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
    result.pending_after_case = clone(association.finalize("", {consume: false}));
    result.final_status = clone(capture.status());
    result.outcome = observations.some(o => o.type === "asr_full_result_event")
      ? "native_results_received" : "native_recognition_unavailable_or_no_results";
  } catch (error) {
    result.errors.push({error: error.name, message: String(error.message || error)});
    log("test_error", {message: String(error.stack || error)});
    result.outcome = "test_error";
  } finally {
    if (recognizer && !ended) {
      try { recognizer.abort(); } catch (_) {}
    }
    if (sourceStarted) {
      try { source.stop(); } catch (_) {}
    }
    source.disconnect();
    await capture.stop();
    await producer.close();
    sink.stream.getTracks().forEach(item => item.stop());
  }
  result.summary = {
    asr_events: observations.filter(o => o.type === "asr_full_result_event").length,
    revisions: observations.filter(o => o.type === "asr_revision").length,
    full_replacements: observations.filter(o => o.type === "asr_revision" && o.replacement).length,
    finals: observations.filter(o => o.type === "asr_revision" && o.is_final).map(o => o.text),
    removed_from_same_result_index_count: observations.filter(o => o.type === "asr_revision")
      .reduce((n, o) => n + o.removed_pause_keys.length, 0),
    changed_duration_count: observations.filter(o => o.type === "asr_revision")
      .reduce((n, o) => n + o.changed_pause_durations.length, 0),
    moved_position_count: observations.filter(o => o.type === "asr_revision")
      .reduce((n, o) => n + o.moved_pause_positions.length, 0),
    count_note: "Removal from one reused interim index is not global pause loss. Compare interval identities across all finals and revisions.",
  };
  return result;
};
