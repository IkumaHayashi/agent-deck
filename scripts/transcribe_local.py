#!/usr/bin/env python3
"""Agent Deckから呼び出すmlx-whisperアダプター。"""
import json
import sys

import mlx_whisper


def transcribe(audio, model, language, initial_prompt):
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=model,
        language=language or None,
        initial_prompt=initial_prompt or None,
        # 短い録音の末尾にある無音から、字幕の定型文を補完しないようにする。
        condition_on_previous_text=False,
        word_timestamps=True,
        hallucination_silence_threshold=1.0,
        verbose=False,
    )
    return {"text": result.get("text", "")}


def worker(model, language):
    """モデルをメモリに保持したまま、JSON Linesで文字起こしを繰り返す。"""
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = transcribe(
                request["audio"], model, language, request.get("initial_prompt", "")
            )
        except Exception as exc:  # ワーカー境界なので呼び出し元へ文字列で返す
            response = {"error": str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker(sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: transcribe_local.py AUDIO MODEL LANGUAGE INITIAL_PROMPT\n"
            "   or: transcribe_local.py --worker MODEL LANGUAGE"
        )
    audio, model, language, initial_prompt = sys.argv[1:]
    print(json.dumps(transcribe(audio, model, language, initial_prompt), ensure_ascii=False))


if __name__ == "__main__":
    main()
