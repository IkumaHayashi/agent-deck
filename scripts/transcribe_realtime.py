#!/usr/bin/env python3
"""Agent Deck用の低遅延日本語ASRワーカー（sherpa-onnx）。"""
import json
import os
import sys
import wave

import numpy as np
import sherpa_onnx


def create_recognizer(model_dir):
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=os.path.join(model_dir, "encoder-epoch-99-avg-1.int8.onnx"),
        decoder=os.path.join(model_dir, "decoder-epoch-99-avg-1.onnx"),
        joiner=os.path.join(model_dir, "joiner-epoch-99-avg-1.int8.onnx"),
        tokens=os.path.join(model_dir, "tokens.txt"),
        num_threads=4,
        provider="cpu",
    )


def transcribe(recognizer, audio_path):
    with wave.open(audio_path, "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("PCM 16-bitモノラルWAVのみ対応しています")
        sample_rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples.astype(np.float32) / 32768.0)
    # 短い途中音声でもZipformerの畳み込み入力長を満たし、語尾を確定させる。
    stream.accept_waveform(sample_rate, np.zeros(sample_rate, dtype=np.float32))
    recognizer.decode_stream(stream)
    return {"text": stream.result.text}


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: transcribe_realtime.py MODEL_DIR")
    recognizer = create_recognizer(sys.argv[1])
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = transcribe(recognizer, request["audio"])
        except Exception as exc:  # ワーカー境界なので呼び出し元へ文字列で返す
            response = {"error": str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
